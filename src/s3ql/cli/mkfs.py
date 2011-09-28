'''
mkfs.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import
from getpass import getpass
from s3ql import CURRENT_FS_REV
from s3ql.backends.common import get_bucket, BetterBucket
from s3ql.common import (get_bucket_cachedir, setup_logging, QuietError, 
    dump_metadata, create_tables, init_tables)
from s3ql.database import Connection
from s3ql.parse_args import ArgumentParser
import cPickle as pickle
import logging
import os
import shutil
import sys
import time


log = logging.getLogger("mkfs")

def parse_args(args):

    parser = ArgumentParser(
        description="Initializes an S3QL file system")

    parser.add_cachedir()
    parser.add_authfile()
    parser.add_debug_modules()
    parser.add_quiet()
    parser.add_version()
    parser.add_storage_url()
    
    parser.add_argument("-L", default='', help="Filesystem label",
                      dest="label", metavar='<name>',)
    parser.add_argument("--blocksize", type=int, default=10240, metavar='<size>',
                      help="Maximum block size in KB (default: %(default)d)")
    parser.add_argument("--plain", action="store_true", default=False,
                      help="Create unencrypted file system.")
    parser.add_argument("--force", action="store_true", default=False,
                        help="Overwrite any existing data.")

    options = parser.parse_args(args)
        
    return options

def main(args=None):

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    setup_logging(options)
    
    plain_bucket = get_bucket(options, plain=True)
    
    if 's3ql_metadata' in plain_bucket:
        if not options.force:
            raise QuietError("Found existing file system! Use --force to overwrite")
            
        log.info('Purging existing file system data..')
        plain_bucket.clear()
        if not plain_bucket.is_get_consistent():
            log.info('Please note that the new file system may appear inconsistent\n'
                     'for a while until the removals have propagated through the backend.')
            
    if not options.plain:
        if sys.stdin.isatty():
            wrap_pw = getpass("Enter encryption password: ")
            if not wrap_pw == getpass("Confirm encryption password: "):
                raise QuietError("Passwords don't match.")
        else:
            wrap_pw = sys.stdin.readline().rstrip()

        # Generate data encryption passphrase
        log.info('Generating random encryption key...')
        fh = open('/dev/urandom', "rb", 0) # No buffering
        data_pw = fh.read(32)
        fh.close()
        
        bucket = BetterBucket(wrap_pw, 'bzip2', plain_bucket)
        bucket['s3ql_passphrase'] = data_pw
    else:    
        data_pw = None
        
    bucket = BetterBucket(data_pw, 'bzip2', plain_bucket)

    # Setup database
    cachepath = get_bucket_cachedir(options.storage_url, options.cachedir)

    # There can't be a corresponding bucket, so we can safely delete
    # these files.
    if os.path.exists(cachepath + '.db'):
        os.unlink(cachepath + '.db')
    if os.path.exists(cachepath + '-cache'):
        shutil.rmtree(cachepath + '-cache')

    log.info('Creating metadata tables...')
    db = Connection(cachepath + '.db')
    create_tables(db)
    init_tables(db)

    param = dict()
    param['revision'] = CURRENT_FS_REV
    param['seq_no'] = 1
    param['label'] = options.label
    param['blocksize'] = options.blocksize * 1024
    param['needs_fsck'] = False
    param['last_fsck'] = time.time() - time.timezone
    param['last-modified'] = time.time() - time.timezone
    
    # This indicates that the convert_legacy_metadata() stuff
    # in BetterBucket is not required for this file system.
    param['bucket_revision'] = 1
    
    bucket.store('s3ql_seq_no_%d' % param['seq_no'], 'Empty')

    log.info('Uploading metadata...')
    with bucket.open_write('s3ql_metadata', param) as fh:
        dump_metadata(fh, db)  
    pickle.dump(param, open(cachepath + '.params', 'wb'), 2)


if __name__ == '__main__':
    main(sys.argv[1:])
