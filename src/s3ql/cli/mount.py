'''
mount.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import
from s3ql import fs, CURRENT_FS_REV, inode_cache
from s3ql.backends.common import get_bucket_factory, BucketPool
from s3ql.block_cache import BlockCache
from s3ql.common import (setup_logging, get_bucket_cachedir, get_seq_no, 
    QuietError, cycle_metadata, dump_metadata, 
    restore_metadata)
from s3ql.daemonize import daemonize
from s3ql.database import Connection
from s3ql.parse_args import ArgumentParser
from threading import Thread
import cPickle as pickle
import llfuse
import logging
import os
import shutil
import signal
import stat
import sys
import tempfile
import textwrap
import thread
import threading
import time

log = logging.getLogger("mount")

def install_thread_excepthook():
    """work around sys.excepthook thread bug
    
    See http://bugs.python.org/issue1230540.

    Call once from __main__ before creating any threads. If using
    psyco, call psyco.cannotcompile(threading.Thread.run) since this
    replaces a new-style class method.
    """

    init_old = threading.Thread.__init__
    def init(self, *args, **kwargs):
        init_old(self, *args, **kwargs)
        run_old = self.run
        def run_with_except_hook(*args, **kw):
            try:
                run_old(*args, **kw)
            except SystemExit:
                raise
            except:
                sys.excepthook(*sys.exc_info())
        self.run = run_with_except_hook
        
    threading.Thread.__init__ = init   
install_thread_excepthook()    
    
def main(args=None):
    '''Mount S3QL file system'''

    if args is None:
        args = sys.argv[1:]

    options = parse_args(args)
    
    # Save handler so that we can remove it when daemonizing
    stdout_log_handler = setup_logging(options)
    
    if options.threads is None:
        options.threads = determine_threads(options)

    if not os.path.exists(options.mountpoint):
        raise QuietError('Mountpoint does not exist.')
        
    if options.profile:
        import cProfile
        import pstats
        prof = cProfile.Profile()

    bucket_factory = get_bucket_factory(options)
    bucket_pool = BucketPool(bucket_factory)
    
    # Get paths
    cachepath = get_bucket_cachedir(options.storage_url, options.cachedir)

    # Retrieve metadata
    with bucket_pool() as bucket:
        (param, db) = get_metadata(bucket, cachepath)
            
    if options.nfs:
        log.info('Creating NFS indices...')
        # NFS may try to look up '..', so we have to speed up this kind of query
        db.execute('CREATE INDEX IF NOT EXISTS ix_contents_inode ON contents(inode)')
        
        # Since we do not support generation numbers, we have to keep the
        # likelihood of reusing a just-deleted inode low
        inode_cache.RANDOMIZE_INODES = True
    else:
        db.execute('DROP INDEX IF EXISTS ix_contents_inode')
                       
    metadata_upload_thread = MetadataUploadThread(bucket_pool, param, db,
                                                  options.metadata_upload_interval)
    block_cache = BlockCache(bucket_pool, db, cachepath + '-cache',
                             options.cachesize * 1024, options.max_cache_entries)
    commit_thread = CommitThread(block_cache)
    operations = fs.Operations(block_cache, db, blocksize=param['blocksize'],
                               upload_event=metadata_upload_thread.event)
    
    log.info('Mounting filesystem...')
    llfuse.init(operations, options.mountpoint, get_fuse_opts(options))

    if not options.fg:
        if stdout_log_handler:
            logging.getLogger().removeHandler(stdout_log_handler)
        daemonize(options.cachedir)

    exc_info = setup_exchook()

    # After we start threads, we must be sure to terminate them
    # or the process will hang 
    try:
        block_cache.init(options.threads)
        metadata_upload_thread.start()
        commit_thread.start()
        
        if options.upstart:
            os.kill(os.getpid(), signal.SIGSTOP)
        if options.profile:
            prof.runcall(llfuse.main, options.single)
        else:
            llfuse.main(options.single)
        
        log.info("FUSE main loop terminated.")
        
    except:
        log.info("Caught exception in main loop, unmounting file system...")  
          
        # Tell finally handler that there already is an exception
        if not exc_info:
            exc_info = sys.exc_info()
        
        # We do *not* unmount on exception. Why? E.g. if someone is mirroring the
        # mountpoint, and it suddenly becomes empty, all the mirrored data will be
        # deleted. However, it's crucial to still call llfuse.close, so that
        # Operations.destroy() can flush the inode cache.
        with llfuse.lock:
            llfuse.close(unmount=False)

        raise
            
    # Terminate threads
    finally:
        log.debug("Waiting for background threads...")
        for (op, with_lock) in ((metadata_upload_thread.stop, False),
                                (commit_thread.stop, False),
                                (block_cache.destroy, True),
                                (metadata_upload_thread.join, False),
                                (commit_thread.join, False)):
            try:
                if with_lock:
                    with llfuse.lock:
                        op()
                else:
                    op()
            except:
                # We just live with the race cond here
                if not exc_info: 
                    exc_info = sys.exc_info()
                else:
                    log.exception("Exception during cleanup:")

        log.debug("All background threads terminated.")
 
    # Re-raise if main loop terminated due to exception in other thread
    # or during cleanup
    if exc_info:
        raise exc_info[0], exc_info[1], exc_info[2]
        
    # At this point, there should be no other threads left

    # Unmount
    log.info("Unmounting file system.")
    with llfuse.lock:
        llfuse.close()
    
    # Do not update .params yet, dump_metadata() may fail if the database is
    # corrupted, in which case we want to force an fsck.
       
    with bucket_pool() as bucket:   
        seq_no = get_seq_no(bucket)
        if metadata_upload_thread.db_mtime == os.stat(cachepath + '.db').st_mtime:
            log.info('File system unchanged, not uploading metadata.')
            del bucket['s3ql_seq_no_%d' % param['seq_no']]         
            param['seq_no'] -= 1
            pickle.dump(param, open(cachepath + '.params', 'wb'), 2)         
        elif seq_no == param['seq_no']:
            log.info('Uploading metadata...')     
            cycle_metadata(bucket)
            param['last-modified'] = time.time() - time.timezone
            with tempfile.TemporaryFile() as tmp:
                dump_metadata(tmp, db)
                tmp.seek(0)
                with bucket.open_write('s3ql_metadata', param) as fh:
                    shutil.copyfileobj(tmp, fh)
            pickle.dump(param, open(cachepath + '.params', 'wb'), 2)
        else:
            log.error('Remote metadata is newer than local (%d vs %d), '
                      'refusing to overwrite!', seq_no, param['seq_no'])
            log.error('The locally cached metadata will be *lost* the next time the file system '
                      'is mounted or checked and has therefore been backed up.')
            for name in (cachepath + '.params', cachepath + '.db'):
                for i in reversed(range(4)):
                    if os.path.exists(name + '.%d' % i):
                        os.rename(name + '.%d' % i, name + '.%d' % (i+1))     
                os.rename(name, name + '.0')
   
    db.execute('ANALYZE')
    db.execute('VACUUM')
    db.close() 

    if options.profile:
        tmp = tempfile.NamedTemporaryFile()
        prof.dump_stats(tmp.name)
        fh = open('s3ql_profile.txt', 'w')
        p = pstats.Stats(tmp.name, stream=fh)
        tmp.close()
        p.strip_dirs()
        p.sort_stats('cumulative')
        p.print_stats(50)
        p.sort_stats('time')
        p.print_stats(50)
        fh.close()
    
def determine_threads(options):
    '''Return optimum number of upload threads'''
            
    cores = os.sysconf('SC_NPROCESSORS_ONLN')
    memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGESIZE')
        
    if options.compress == 'lzma':
        # Keep this in sync with compression level in backends/common.py
        # Memory usage according to man xz(1)
        mem_per_thread = 186 * 1024**2
    else:
        # Only check LZMA memory usage
        mem_per_thread = 0
                
    if cores == -1 or memory == -1:
        log.warn("Can't determine number of cores, using 2 upload threads.")
        return 1
    elif 2*cores * mem_per_thread > (memory/2):
        threads = int((memory/2) // mem_per_thread)
        log.info('Using %d upload threads (memory limited).', threads)
        return threads
    else:
        log.info("Using %d upload threads.", 2*cores)
        return 2*cores
    
        
def get_metadata(bucket, cachepath):
    '''Retrieve metadata
    
    Checks:
    - Revision
    - Unclean mounts
    
    Locally cached metadata is used if up-to-date.
    '''
                
    seq_no = get_seq_no(bucket)

    # Check for cached metadata
    db = None
    if os.path.exists(cachepath + '.params'):
        param = pickle.load(open(cachepath + '.params', 'rb'))
        if param['seq_no'] < seq_no:
            log.info('Ignoring locally cached metadata (outdated).')
            param = bucket.lookup('s3ql_metadata')
        else:
            log.info('Using cached metadata.')
            db = Connection(cachepath + '.db')
    else:
        param = bucket.lookup('s3ql_metadata')
 
    # Check for unclean shutdown
    if param['seq_no'] < seq_no:
        if bucket.is_get_consistent():
            raise QuietError(textwrap.fill(textwrap.dedent('''\
                It appears that the file system is still mounted somewhere else. If this is not
                the case, the file system may have not been unmounted cleanly and you should try
                to run fsck on the computer where the file system has been mounted most recently.
                ''')))
        else:                
            raise QuietError(textwrap.fill(textwrap.dedent('''\
                It appears that the file system is still mounted somewhere else. If this is not the
                case, the file system may have not been unmounted cleanly or the data from the 
                most-recent mount may have not yet propagated through the backend. In the later case,
                waiting for a while should fix the problem, in the former case you should try to run
                fsck on the computer where the file system has been mounted most recently.
                ''')))
       
    # Check revision
    if param['revision'] < CURRENT_FS_REV:
        raise QuietError('File system revision too old, please run `s3qladm upgrade` first.')
    elif param['revision'] > CURRENT_FS_REV:
        raise QuietError('File system revision too new, please update your '
                         'S3QL installation.')
        
    # Check that the fs itself is clean
    if param['needs_fsck']:
        raise QuietError("File system damaged or not unmounted cleanly, run fsck!")        
    if (time.time() - time.timezone) - param['last_fsck'] > 60 * 60 * 24 * 31:
        log.warn('Last file system check was more than 1 month ago, '
                 'running fsck.s3ql is recommended.')
    
    # Download metadata
    if not db:
        log.info("Downloading & uncompressing metadata...")
        os.close(os.open(cachepath + '.db.tmp', os.O_RDWR | os.O_CREAT | os.O_TRUNC,
                         stat.S_IRUSR | stat.S_IWUSR)) 
        db = Connection(cachepath + '.db.tmp', fast_mode=True)
        with bucket.open_read("s3ql_metadata") as fh:
            restore_metadata(fh, db)
        db.close()
        os.rename(cachepath + '.db.tmp', cachepath + '.db')
        db = Connection(cachepath + '.db')
 
    # Increase metadata sequence no 
    param['seq_no'] += 1
    param['needs_fsck'] = True
    bucket['s3ql_seq_no_%d' % param['seq_no']] = 'Empty'
    pickle.dump(param, open(cachepath + '.params', 'wb'), 2)
    param['needs_fsck'] = False
    
    return (param, db)

def get_fuse_opts(options):
    '''Return fuse options for given command line options'''

    fuse_opts = [ b"nonempty", b'fsname=%s' % options.storage_url,
                  'subtype=s3ql' ]

    if options.allow_other:
        fuse_opts.append(b'allow_other')
    if options.allow_root:
        fuse_opts.append(b'allow_root')
    if options.allow_other or options.allow_root:
        fuse_opts.append(b'default_permissions')

    return fuse_opts


def parse_args(args):
    '''Parse command line'''

    # Parse fstab-style -o options
    if '--' in args:
        max_idx = args.index('--')
    else:
        max_idx = len(args)
    if '-o' in args[:max_idx]:
        pos = args.index('-o')
        val = args[pos + 1]
        del args[pos]
        del args[pos]
        for opt in reversed(val.split(',')):
            if '=' in opt:
                (key, val) = opt.split('=')
                args.insert(pos, val)
                args.insert(pos, '--' + key)
            else:
                if opt in ('rw', 'defaults', 'auto', 'noauto', 'user', 'nouser', 'dev', 'nodev',
                           'suid', 'nosuid', 'atime', 'diratime', 'exec', 'noexec', 'group',
                           'mand', 'nomand', '_netdev', 'nofail', 'norelatime', 'strictatime',
                           'owner', 'users', 'nobootwait'):
                    continue
                elif opt == 'ro':
                    raise QuietError('Read-only mounting not supported.')
                args.insert(pos, '--' + opt)

    parser = ArgumentParser(
        description="Mount an S3QL file system.")

    parser.add_log('~/.s3ql/mount.log')
    parser.add_cachedir()
    parser.add_authfile()
    parser.add_debug_modules()
    parser.add_quiet()
    parser.add_version()
    parser.add_storage_url()
    
    parser.add_argument("mountpoint", metavar='<mountpoint>',
                        type=(lambda x: x.rstrip('/')),
                        help='Where to mount the file system')
        
    parser.add_argument("--cachesize", type=int, default=102400, metavar='<size>', 
                      help="Cache size in kb (default: 102400 (100 MB)). Should be at least 10 times "
                      "the blocksize of the filesystem, otherwise an object may be retrieved and "
                      "written several times during a single write() or read() operation.")
    parser.add_argument("--max-cache-entries", type=int, default=768, metavar='<num>',
                      help="Maximum number of entries in cache (default: %(default)d). "
                      'Each cache entry requires one file descriptor, so if you increase '
                      'this number you have to make sure that your process file descriptor '
                      'limit (as set with `ulimit -n`) is high enough (at least the number ' 
                      'of cache entries + 100).')
    parser.add_argument("--allow-other", action="store_true", default=False, help=
                      'Normally, only the user who called `mount.s3ql` can access the mount '
                      'point. This user then also has full access to it, independent of '
                      'individual file permissions. If the `--allow-other` option is '
                      'specified, other users can access the mount point as well and '
                      'individual file permissions are taken into account for all users.')
    parser.add_argument("--allow-root", action="store_true", default=False,
                      help='Like `--allow-other`, but restrict access to the mounting '
                           'user and the root user.')
    parser.add_argument("--fg", action="store_true", default=False,
                      help="Do not daemonize, stay in foreground")
    parser.add_argument("--single", action="store_true", default=False,
                      help="Run in single threaded mode. If you don't understand this, "
                           "then you don't need it.")
    parser.add_argument("--upstart", action="store_true", default=False,
                      help="Stay in foreground and raise SIGSTOP once mountpoint "
                           "is up.")
    parser.add_argument("--profile", action="store_true", default=False,
                      help="Create profiling information. If you don't understand this, "
                           "then you don't need it.")
    parser.add_argument("--compress", action="store", default='lzma', metavar='<name>',
                      choices=('lzma', 'bzip2', 'zlib', 'none'),
                      help="Compression algorithm to use when storing new data. Allowed "
                           "values: `lzma`, `bzip2`, `zlib`, none. (default: `%(default)s`)")
    parser.add_argument("--metadata-upload-interval", action="store", type=int,
                      default=24*60*60, metavar='<seconds>',
                      help='Interval in seconds between complete metadata uploads. '
                           'Set to 0 to disable. Default: 24h.')
    parser.add_argument("--threads", action="store", type=int,
                      default=None, metavar='<no>',
                      help='Number of parallel upload threads to use (default: auto).')
    parser.add_argument("--nfs", action="store_true", default=False,
                      help='Support export of S3QL file systems over NFS ' 
                           '(default: %(default)s)')
        
    options = parser.parse_args(args)

    if options.allow_other and options.allow_root:
        parser.error("--allow-other and --allow-root are mutually exclusive.")

    if not options.log and not options.fg:
        parser.error("Please activate logging to a file or syslog, or use the --fg option.")
        
    if options.profile:
        options.single = True

    if options.upstart:
        options.fg = True
        
    if options.metadata_upload_interval == 0:
        options.metadata_upload_interval = None
        
    if options.compress == 'none':
        options.compress = None

    return options

class MetadataUploadThread(Thread):
    '''
    Periodically upload metadata. Upload is done every `interval`
    seconds, and whenever `event` is set. To terminate thread,
    set `quit` attribute as well as `event` event.
    
    This class uses the llfuse global lock. When calling objects
    passed in the constructor, the global lock is acquired first.    
    '''    
    
    def __init__(self, bucket_pool, param, db, interval):
        super(MetadataUploadThread, self).__init__()
        self.bucket_pool = bucket_pool
        self.param = param
        self.db = db
        self.interval = interval
        self.daemon = True
        self.db_mtime = os.stat(db.file).st_mtime 
        self.event = threading.Event()
        self.quit = False
        self.name = 'Metadata-Upload-Thread'
           
    def run(self):
        log.debug('MetadataUploadThread: start')
        
        while not self.quit:
            self.event.wait(self.interval)
            self.event.clear()
            
            if self.quit:
                break
            
            with llfuse.lock:
                if self.quit:
                    break
                new_mtime = os.stat(self.db.file).st_mtime 
                if self.db_mtime == new_mtime:
                    log.info('File system unchanged, not uploading metadata.')
                    continue
                
                # We dump to a file first, so that we don't hold the
                # lock for quite so long.
                log.info('Saving metadata...')
                fh = tempfile.TemporaryFile()
                dump_metadata(fh, self.db) 
              
            with self.bucket_pool() as bucket:
                seq_no = get_seq_no(bucket)
                if seq_no != self.param['seq_no']:
                    log.error('Remote metadata is newer than local (%d vs %d), '
                              'refusing to overwrite!', seq_no, self.param['seq_no'])
                    fh.close()
                    continue
                              
                log.info("Compressing & uploading metadata..")
                cycle_metadata(bucket)
                fh.seek(0)
                self.param['last-modified'] = time.time() - time.timezone
                
                # Temporarily decrease sequence no, this is not the final upload
                self.param['seq_no'] -= 1
                with bucket.open_write("s3ql_metadata", self.param) as obj_fh:
                    shutil.copyfileobj(fh, obj_fh)
                self.param['seq_no'] += 1
                
                fh.close()
                self.db_mtime = new_mtime    

        log.debug('MetadataUploadThread: end')    
        
    def stop(self):
        '''Signal thread to terminate'''
        
        self.quit = True
        self.event.set()     
    
def setup_exchook():
    '''Send SIGTERM if any other thread terminates with an exception
    
    The exc_info will be saved in the list object returned
    by this function.
    '''
    
    this_thread = thread.get_ident()
    old_exchook = sys.excepthook
    exc_info = []
    
    def exchook(type_, val, tb):
        if (thread.get_ident() != this_thread
            and not exc_info):
            os.kill(os.getpid(), signal.SIGTERM)
            exc_info.append(type_)
            exc_info.append(val)
            exc_info.append(tb)
        
            old_exchook(type_, val, tb)
            
        # If the main thread re-raised exception, there is no need to call
        # excepthook again
        elif not (thread.get_ident() == this_thread
                  and exc_info == [type_, val, tb]):
            old_exchook(type_, val, tb)
        
    sys.excepthook = exchook
    
    return exc_info
 

   
class CommitThread(Thread):
    '''
    Periodically upload dirty blocks.
    
    This class uses the llfuse global lock. When calling objects
    passed in the constructor, the global lock is acquired first.
    '''    
    

    def __init__(self, block_cache):
        super(CommitThread, self).__init__()
        self.block_cache = block_cache 
        self.stop_event = threading.Event()
        self.name = 'CommitThread'
                    
    def run(self):
        log.debug('CommitThread: start')
 
        while not self.stop_event.is_set():
            did_sth = False
            stamp = time.time()
            for el in self.block_cache.entries.values_rev():
                if stamp - el.last_access < 10:
                    break
                if not (el.dirty and (el.inode, el.blockno) not in self.block_cache.in_transit):
                    continue
                        
                # Acquire global lock to access UploadManager instance
                with llfuse.lock:
                    if self.stop_event.is_set():
                        break
                    # Object may have been accessed while waiting for lock
                    if not (el.dirty and (el.inode, el.blockno) not in self.block_cache.in_transit):
                        continue
                    self.block_cache.upload(el)
                did_sth = True

                if self.stop_event.is_set():
                    break
            
            if not did_sth:
                self.stop_event.wait(5)

        log.debug('CommitThread: end')    
        
    def stop(self):
        '''Signal thread to terminate'''
        
        self.stop_event.set()
        

if __name__ == '__main__':
    main(sys.argv[1:])
