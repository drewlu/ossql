'''
local.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function, absolute_import

from .common import AbstractBucket, NoSuchBucket, NoSuchObject
import shutil
import logging
import cPickle as pickle
import os
import errno
import thread

log = logging.getLogger("backend.local")

class Bucket(AbstractBucket):
    '''
    A bucket that is stored on the local hard disk
    '''

    needs_login = False
    
    def __init__(self, name, backend_login, backend_pw): #IGNORE:W0613
        '''Initialize local bucket
        
        Login and password are ignored.
        '''
        super(Bucket, self).__init__()
        self.name = name
        
        if not os.path.exists(name):
            raise NoSuchBucket(name)

    def __str__(self):
        return 'local://%s' % self.name

    def lookup(self, key):
        """Return metadata for given key.

        If the key does not exist, `NoSuchObject` is raised.
        """
        
        path = self._key_to_path(key)
        try:
            with open(path, 'rb') as src:
                return pickle.load(src)
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                raise NoSuchObject(key)
            else:
                raise

    def open_read(self, key):
        """Open object for reading

        Return a tuple of a file-like object. Bucket contents can be read from
        the file-like object, metadata is stored in its *metadata* attribute and
        can be modified by the caller at will.
        """
        
        path = self._key_to_path(key)
        try:
            fh = ObjectR(path) 
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                raise NoSuchObject(key)
            else:
                raise
            
        fh.metadata = pickle.load(fh)
        
        return fh
    
    def open_write(self, key, metadata=None):
        """Open object for writing

        `metadata` can be a dict of additional attributes to store with the
        object. Returns a file-like object.
        """
        
        if metadata is None:
            metadata = dict()
        
        
        path = self._key_to_path(key)

        # By renaming, we make sure that there are no
        # conflicts between parallel reads, the last one wins
        tmpname = '%s#%d-%d' % (path, os.getpid(), thread.get_ident()) 
        
        try:
            dest = open(tmpname, 'wb')
        except IOError as exc:
            if exc.errno != errno.ENOENT:
                raise
            try:
                os.makedirs(os.path.dirname(path))
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                else:
                    # Another thread may have created the directory already
                    pass
            dest = open(tmpname, 'wb', 0)
            
        os.rename(tmpname, path)
        pickle.dump(metadata, dest, 2)
        return dest    
            
    def is_get_consistent(self):
        '''If True, objects retrievals are guaranteed to be up-to-date
        
        If this method returns True, then creating, deleting, or overwriting an
        object is guaranteed to be immediately reflected in subsequent object
        retrieval attempts.
        '''
        
        return True
                    
    def is_list_create_consistent(self):
        '''If True, new objects are guaranteed to show up in object listings
        
        If this method returns True, creation of objects will immediately be
        reflected when retrieving the list of available objects.
        '''

        return True
    
    def clear(self):
        """Delete all objects in bucket"""
        
        for name in os.listdir(self.name):
            path = os.path.join(self.name, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)

    def contains(self, key):
        '''Check if `key` is in bucket'''
        
        path = self._key_to_path(key)
        try:
            os.lstat(path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return False
            raise
        return True

    def delete(self, key, force=False):
        """Delete object stored under `key`

        ``bucket.delete(key)`` can also be written as ``del bucket[key]``.
        If `force` is true, do not return an error if the key does not exist.
        """
        path = self._key_to_path(key)
        try:
            os.unlink(path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                if force:
                    pass
                else:
                    raise NoSuchObject(key)
            else:
                raise

    def list(self, prefix=''):
        '''List keys in bucket

        Returns an iterator over all keys in the bucket.
        '''
        if prefix:
            base = os.path.dirname(self._key_to_path(prefix))     
        else:
            base = self.name
            
        for (path, dirnames, filenames) in os.walk(base, topdown=True):
            
            # Do not look in wrong directories
            if prefix:
                rpath = path[len(self.name):] # path relative to base
                prefix_l = ''.join(rpath.split('/'))
                
                dirs_to_walk = list()
                for name in dirnames:
                    prefix_ll = unescape(prefix_l + name)
                    if prefix_ll.startswith(prefix[:len(prefix_ll)]):
                        dirs_to_walk.append(name)
                dirnames[:] = dirs_to_walk
                                            
            for name in filenames:
                key = unescape(name)
                
                if not prefix or key.startswith(prefix):
                    yield key

    def copy(self, src, dest):
        """Copy data stored under key `src` to key `dest`
        
        If `dest` already exists, it will be overwritten.
        """
        
        path_src = self._key_to_path(src)
        path_dest = self._key_to_path(dest)

        # Can't use shutil.copyfile() here, need to make
        # sure destination path exists
        try:
            dest = open(path_dest, 'wb')
        except IOError as exc:
            if exc.errno != errno.ENOENT:
                raise
            try:
                os.makedirs(os.path.dirname(path_dest))
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                else:
                    # Another thread may have created the directory already
                    pass            
            dest = open(path_dest, 'wb')
        
        try:
            with open(path_src, 'rb') as src:
                shutil.copyfileobj(src, dest)
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                raise NoSuchObject(src)
            else:
                raise
        finally:
            dest.close()

    def rename(self, src, dest):
        """Rename key `src` to `dest`
        
        If `dest` already exists, it will be overwritten.
        """
        src_path = self._key_to_path(src)
        dest_path = self._key_to_path(dest)
        if not os.path.exists(src_path):
            raise NoSuchObject(src)
           
        try: 
            os.rename(src_path, dest_path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
            try:
                os.makedirs(os.path.dirname(dest_path))
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                else:
                    # Another thread may have created the directory already
                    pass                   
            os.rename(src_path, dest_path)  
        
    def _key_to_path(self, key):
        '''Return path for given key'''
        
        # NOTE: We must not split the path in the middle of an
        # escape sequence, or list() will fail to work.
        
        key = escape(key)
        
        if not key.startswith('s3ql_data_'):
            return os.path.join(self.name, key)
        
        no = key[10:]
        path = [ self.name, 's3ql_data_']
        for i in range(0, len(no), 3):
            path.append(no[:i])
        path.append(key)
        
        return os.path.join(*path)

def escape(s):
    '''Escape '/', '=' and '.' in s'''

    s = s.replace('=', '=3D')
    s = s.replace('/', '=2F')
    s = s.replace('#', '=23')

    return s

def unescape(s):
    '''Un-Escape '/', '=' and '.' in s'''

    s = s.replace('=2F', '/')
    s = s.replace('=23', '#')
    s = s.replace('=3D', '=')

    return s

class ObjectR(file):
    '''A local storage object opened for reading'''
    
    def __init__(self, name, metadata=None):
        super(ObjectR, self).__init__(name, 'rb', buffering=0)
        self.metadata = metadata     