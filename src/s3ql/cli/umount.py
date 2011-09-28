'''
umount.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import

import llfuse
import sys
import os
import logging
from s3ql.common import (CTRL_NAME, QuietError, setup_logging)
from s3ql.parse_args import ArgumentParser
import posixpath
import subprocess
import time
import textwrap

log = logging.getLogger("umount")

def parse_args(args):
    '''Parse command line
     
    This function writes to stdout/stderr and may call `system.exit()` instead 
    of throwing an exception if it encounters errors.
    '''

    parser = ArgumentParser(
        description=textwrap.dedent('''\ 
        Unmounts an S3QL file system. The command returns only after all data
        has been uploaded to the backend.'''))

    parser.add_debug()
    parser.add_quiet()
    parser.add_version()
    parser.add_argument("mountpoint", metavar='<mountpoint>',
                        type=(lambda x: x.rstrip('/')),
                        help='Mount point to un-mount')
    
    parser.add_argument('--lazy', "-z", action="store_true", default=False,
                      help="Lazy umount. Detaches the file system immediately, even if there "
                      'are still open files. The data will be uploaded in the background '
                      'once all open files have been closed.')

    return parser.parse_args(args)


def main(args=None):
    '''Umount S3QL file system
    
    This function writes to stdout/stderr and calls `system.exit()` instead
    of returning.
    '''

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    setup_logging(options)
    mountpoint = options.mountpoint
        
    # Check if it's a mount point
    if not posixpath.ismount(mountpoint):
        print('Not a mount point.', file=sys.stderr)
        sys.exit(1)

    # Check if it's an S3QL mountpoint
    ctrlfile = os.path.join(mountpoint, CTRL_NAME)
    if not (CTRL_NAME not in llfuse.listdir(mountpoint)
            and os.path.exists(ctrlfile)):
        print('Not an S3QL file system.', file=sys.stderr)
        sys.exit(1)

    if options.lazy:
        lazy_umount(mountpoint)
    else:
        blocking_umount(mountpoint)


def lazy_umount(mountpoint):
    '''Invoke fusermount -u -z for mountpoint
    
    This function writes to stdout/stderr and calls `system.exit()`.
    '''

    if os.getuid() == 0:
        umount_cmd = ('umount', '-l', mountpoint)
    else:
        umount_cmd = ('fusermount', '-u', '-z', mountpoint)
    
    if not subprocess.call(umount_cmd) == 0:
        sys.exit(1)

def blocking_umount(mountpoint):
    '''Invoke fusermount and wait for daemon to terminate.
    
    This function writes to stdout/stderr and calls `system.exit()`.
    '''

    devnull = open('/dev/null', 'wb')
    if subprocess.call(['fuser', '-m', mountpoint], stdout=devnull,
                       stderr=devnull) == 0:
        print('Cannot unmount, the following processes still access the mountpoint:')
        subprocess.call(['fuser', '-v', '-m', mountpoint], stdout=sys.stdout,
                        stderr=sys.stdout)
        raise QuietError()

    ctrlfile = os.path.join(mountpoint, CTRL_NAME)
    
    log.debug('Flushing cache...')
    llfuse.setxattr(ctrlfile, b's3ql_flushcache!', b'dummy')

    # Get pid
    log.debug('Trying to get pid')
    pid = int(llfuse.getxattr(ctrlfile, b's3ql_pid?'))
    log.debug('PID is %d', pid)

    # Get command line to make race conditions less-likely
    with open('/proc/%d/cmdline' % pid, 'r') as fh:
        cmdline = fh.readline()
    log.debug('cmdline is %r', cmdline)

    # Unmount
    log.debug('Unmounting...')
    # This seems to be necessary to prevent weird busy errors
    time.sleep(3)
    
    if os.getuid() == 0:
        umount_cmd = ['umount', mountpoint]
    else:
        umount_cmd = ['fusermount', '-u', mountpoint]
            
    if subprocess.call(umount_cmd) != 0:
        sys.exit(1)

    # Wait for daemon
    log.debug('Uploading metadata...')
    step = 0.5
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            log.debug('Kill failed, assuming daemon has quit.')
            break

        # Check that the process did not terminate and the PID
        # was reused by a different process
        try:
            with open('/proc/%d/cmdline' % pid, 'r') as fh:
                if fh.readline() != cmdline:
                    log.debug('PID still alive, but cmdline changed')
                    # PID must have been reused, original process terminated
                    break
                else:
                    log.debug('PID still alive and commandline unchanged.')
        except OSError:
            # Process must have exited by now
            log.debug('Reading cmdline failed, assuming daemon has quit.')
            break

        # Process still exists, we wait
        log.debug('Daemon seems to be alive, waiting...')
        time.sleep(step)
        if step < 10:
            step *= 2

if __name__ == '__main__':
    main(sys.argv[1:])
