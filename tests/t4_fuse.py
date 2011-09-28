'''
t4_fuse.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function
from _common import TestCase
from cStringIO import StringIO
from os.path import basename
from s3ql.common import AsyncFn
import filecmp
import os.path
import s3ql.cli.fsck
import s3ql.cli.ctrl
import s3ql.cli.mkfs
import s3ql.cli.mount
import s3ql.cli.umount
import shutil
import stat
import llfuse
import subprocess
import sys
import tempfile
import time
import unittest2 as unittest

# For debugging
USE_VALGRIND = False

def retry(timeout, fn, *a, **kw):
    """Wait for fn(*a, **kw) to return True.
    
    If the return value of fn() returns something True, this value
    is returned. Otherwise, the function is called repeatedly for
    `timeout` seconds. If the timeout is reached, `TimeoutError` is
    raised.
    """

    step = 0.2
    waited = 0
    while waited < timeout:
        ret = fn(*a, **kw)
        if ret:
            return ret
        time.sleep(step)
        waited += step
        if step < waited / 30:
            step *= 2

    raise TimeoutError()

class TimeoutError(Exception):
    '''Raised by `retry()` when a timeout is reached.'''

    pass

if __name__ == '__main__':
    mypath = sys.argv[0]
else:
    mypath = __file__
BASEDIR = os.path.abspath(os.path.join(os.path.dirname(mypath), '..'))
            
class fuse_tests(TestCase):
    
    def setUp(self):
        # We need this to test multi block operations
        self.src = __file__
        if os.path.getsize(self.src) < 1048:
            raise RuntimeError("test file %s should be bigger than 1 kb" % self.src)

        self.mnt_dir = tempfile.mkdtemp()
        self.cache_dir = tempfile.mkdtemp()
        self.bucket_dir = tempfile.mkdtemp()

        self.bucketname = 'local://' + self.bucket_dir
        self.passphrase = 'oeut3d'

        self.mount_process = None
        self.name_cnt = 0
                    
    def mkfs(self):
        proc = subprocess.Popen([os.path.join(BASEDIR, 'bin', 'mkfs.s3ql'), 
                                 '-L', 'test fs', '--blocksize', '500',
                                 '--cachedir', self.cache_dir, '--quiet',
                                 self.bucketname ], stdin=subprocess.PIPE)
        
        print(self.passphrase, file=proc.stdin)
        print(self.passphrase, file=proc.stdin)
        proc.stdin.close()
        
        self.assertEqual(proc.wait(), 0)
        
    def mount(self):  
        self.mount_process = subprocess.Popen([os.path.join(BASEDIR, 'bin', 'mount.s3ql'), 
                                                "--fg", '--cachedir', self.cache_dir,
                                                '--log', 'none', '--quiet',
                                                  self.bucketname, self.mnt_dir],
                                                  stdin=subprocess.PIPE)
        print(self.passphrase, file=self.mount_process.stdin)
        self.mount_process.stdin.close()
        retry(30, os.path.ismount, self.mnt_dir)                              

    def umount(self):
        devnull = open('/dev/null', 'wb')
        retry(5, lambda: subprocess.call(['fuser', '-m', self.mnt_dir],
                                         stdout=devnull, stderr=devnull) == 1)
        
        subprocess.check_call([os.path.join(BASEDIR, 'bin', 'umount.s3ql'), 
                                '--quiet', self.mnt_dir])
        self.assertEqual(self.mount_process.wait(), 0)
        self.assertFalse(os.path.ismount(self.mnt_dir))

    def fsck(self):
        proc = subprocess.Popen([os.path.join(BASEDIR, 'bin', 'fsck.s3ql'), 
                                 '--force', '--quiet', '--log', 'none',
                                 '--cachedir', self.cache_dir, 
                                 self.bucketname ], stdin=subprocess.PIPE)
        print(self.passphrase, file=proc.stdin)
        proc.stdin.close()
        self.assertEqual(proc.wait(), 0)                

    def tearDown(self):
        subprocess.call(['fusermount', '-z', '-u', self.mnt_dir],
                        stderr=open('/dev/null', 'wb'))
        os.rmdir(self.mnt_dir)
        shutil.rmtree(self.cache_dir)
        shutil.rmtree(self.bucket_dir)


    def runTest(self):
        # Run all tests in same environment, mounting and umounting
        # just takes too long otherwise

        self.mkfs()
        self.mount()
        self.tst_chown()
        self.tst_link()
        self.tst_mkdir()
        self.tst_mknod()
        self.tst_readdir()
        self.tst_statvfs()
        self.tst_symlink()
        self.tst_truncate()
        self.tst_truncate_nocache()
        self.tst_write()
        self.umount()
        self.fsck()
        
        # Empty cache
        shutil.rmtree(self.cache_dir)
        self.cache_dir = tempfile.mkdtemp()
        
        self.mount()
        self.umount()
        
        # Empty cache
        shutil.rmtree(self.cache_dir)
        self.cache_dir = tempfile.mkdtemp()
        self.fsck()
        
    def newname(self):
        self.name_cnt += 1
        return "s3ql_%d" % self.name_cnt

    def tst_mkdir(self):
        dirname = self.newname()
        fullname = self.mnt_dir + "/" + dirname
        os.mkdir(fullname)
        fstat = os.stat(fullname)
        self.assertTrue(stat.S_ISDIR(fstat.st_mode))
        self.assertEquals(llfuse.listdir(fullname), [])
        self.assertEquals(fstat.st_nlink, 1)
        self.assertTrue(dirname in llfuse.listdir(self.mnt_dir))
        os.rmdir(fullname)
        self.assertRaises(OSError, os.stat, fullname)
        self.assertTrue(dirname not in llfuse.listdir(self.mnt_dir))

    def tst_symlink(self):
        linkname = self.newname()
        fullname = self.mnt_dir + "/" + linkname
        os.symlink("/imaginary/dest", fullname)
        fstat = os.lstat(fullname)
        self.assertTrue(stat.S_ISLNK(fstat.st_mode))
        self.assertEquals(os.readlink(fullname), "/imaginary/dest")
        self.assertEquals(fstat.st_nlink, 1)
        self.assertTrue(linkname in llfuse.listdir(self.mnt_dir))
        os.unlink(fullname)
        self.assertRaises(OSError, os.lstat, fullname)
        self.assertTrue(linkname not in llfuse.listdir(self.mnt_dir))

    def tst_mknod(self):
        filename = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, filename)
        fstat = os.lstat(filename)
        self.assertTrue(stat.S_ISREG(fstat.st_mode))
        self.assertEquals(fstat.st_nlink, 1)
        self.assertTrue(basename(filename) in llfuse.listdir(self.mnt_dir))
        self.assertTrue(filecmp.cmp(src, filename, False))
        os.unlink(filename)
        self.assertRaises(OSError, os.stat, filename)
        self.assertTrue(basename(filename) not in llfuse.listdir(self.mnt_dir))

    def tst_chown(self):
        filename = os.path.join(self.mnt_dir, self.newname())
        os.mkdir(filename)
        fstat = os.lstat(filename)
        uid = fstat.st_uid
        gid = fstat.st_gid

        uid_new = uid + 1
        os.chown(filename, uid_new, -1)
        fstat = os.lstat(filename)
        self.assertEquals(fstat.st_uid, uid_new)
        self.assertEquals(fstat.st_gid, gid)

        gid_new = gid + 1
        os.chown(filename, -1, gid_new)
        fstat = os.lstat(filename)
        self.assertEquals(fstat.st_uid, uid_new)
        self.assertEquals(fstat.st_gid, gid_new)

        os.rmdir(filename)
        self.assertRaises(OSError, os.stat, filename)
        self.assertTrue(basename(filename) not in llfuse.listdir(self.mnt_dir))


    def tst_write(self):
        name = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, name)
        self.assertTrue(filecmp.cmp(name, src, False))

        # Don't unlink file, we want to see if cache flushing
        # works

    def tst_statvfs(self):
        os.statvfs(self.mnt_dir)

    def tst_link(self):
        name1 = os.path.join(self.mnt_dir, self.newname())
        name2 = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, name1)
        self.assertTrue(filecmp.cmp(name1, src, False))
        os.link(name1, name2)

        fstat1 = os.lstat(name1)
        fstat2 = os.lstat(name2)

        self.assertEquals(fstat1, fstat2)
        self.assertEquals(fstat1.st_nlink, 2)

        self.assertTrue(basename(name2) in llfuse.listdir(self.mnt_dir))
        self.assertTrue(filecmp.cmp(name1, name2, False))
        os.unlink(name2)
        fstat1 = os.lstat(name1)
        self.assertEquals(fstat1.st_nlink, 1)
        os.unlink(name1)

    def tst_readdir(self):
        dir_ = os.path.join(self.mnt_dir, self.newname())
        file_ = dir_ + "/" + self.newname()
        subdir = dir_ + "/" + self.newname()
        subfile = subdir + "/" + self.newname()
        src = self.src

        os.mkdir(dir_)
        shutil.copyfile(src, file_)
        os.mkdir(subdir)
        shutil.copyfile(src, subfile)

        listdir_is = llfuse.listdir(dir_)
        listdir_is.sort()
        listdir_should = [ basename(file_), basename(subdir) ]
        listdir_should.sort()
        self.assertEquals(listdir_is, listdir_should)

        os.unlink(file_)
        os.unlink(subfile)
        os.rmdir(subdir)
        os.rmdir(dir_)

    def tst_truncate(self):
        filename = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, filename)
        self.assertTrue(filecmp.cmp(filename, src, False))
        fstat = os.stat(filename)
        size = fstat.st_size
        fd = os.open(filename, os.O_RDWR)

        os.ftruncate(fd, size + 1024) # add > 1 block
        self.assertEquals(os.stat(filename).st_size, size + 1024)

        os.ftruncate(fd, size - 1024) # Truncate > 1 block
        self.assertEquals(os.stat(filename).st_size, size - 1024)

        os.close(fd)
        os.unlink(filename)
            
    def tst_truncate_nocache(self):
        filename = os.path.join(self.mnt_dir, self.newname())
        src = self.src
        shutil.copyfile(src, filename)
        self.assertTrue(filecmp.cmp(filename, src, False))
        fstat = os.stat(filename)
        size = fstat.st_size
        
        try:
            s3ql.cli.ctrl.main(['flushcache', self.mnt_dir])
        except:
            sys.excepthook(*sys.exc_info())
            self.fail("s3qlctrl raised exception")
                    
        fd = os.open(filename, os.O_RDWR)

        os.ftruncate(fd, size + 1024) # add > 1 block
        self.assertEquals(os.stat(filename).st_size, size + 1024)

        os.ftruncate(fd, size - 1024) # Truncate > 1 block
        self.assertEquals(os.stat(filename).st_size, size - 1024)

        os.close(fd)
        os.unlink(filename)
                
# Somehow important according to pyunit documentation
def suite():
    return unittest.makeSuite(fuse_tests)


# Allow calling from command line
if __name__ == "__main__":
    unittest.main()
