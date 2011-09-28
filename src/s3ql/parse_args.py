#!/usr/bin/env python
#
# argparse.py - this file is part of S3QL (http://s3ql.googlecode.com)
#
# Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>
#
# This program can be distributed under the terms of the GNU GPLv3.
#
'''
This module provides a customized ArgumentParser class. Differences
are:

  * a --version argument is added by default
  
  * convenience functions are available for adding --quiet, 
    --debug, --cachedir, --log and --authfile options.
  
  * instead of the usage string one can pass a usage list. The first
    element will be prefixed with ``usage: `` as usual. Additional
    elements will be printed on separate lines and prefixed with
    ``  or:  ``.
    
  * When element of an usage list, the ``DEFAULT_USAGE`` object
    will be replaced by the automatically generated usage message,
    excluding any --help arguments.
    
  * When specified on its own, the replacement will be done including
    any --help arguments.
      
  * The ``usage`` and ``add_help`` settings are inherited from the
    parent parser to the subparsers. 
    
'''

# Pylint really gets confused by this module
#pylint: disable-all

from __future__ import division, print_function, absolute_import

import s3ql
import argparse
import re
import os
import textwrap
import logging.handlers
import sys

__all__ = [ 'ArgumentParser', 'DEFAULT_USAGE']

DEFAULT_USAGE = object()

class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    
    def _format_usage(self, usage, actions, groups, prefix):
        '''Special handling for usage lists
        
        If usage is a list object, its elements will be printed on
        separate lines. DEFAULT_USAGE will be replaced by the
        default usage string of the parser (but, if `usage`` is a list,
        excluding any --help arguments)).
        '''
        
        if isinstance(usage, list):
            # Omit help argument
            actions = [ x for x in actions if not isinstance(x, argparse._HelpAction) ]
            res = []
            for s in usage:
                if not res:
                    res.append('usage: ')
                else:
                    res.append('  or:  ')
                if s is DEFAULT_USAGE:
                    res.append(super(HelpFormatter, self)._format_usage(None, actions, groups, '')[:-1])
                else:
                    res.append(s % dict(prog=self._prog))
                    res.append('\n')
            
            return '%s\n\n' % ''.join(res)

        elif usage is DEFAULT_USAGE:
            return super(HelpFormatter, self)._format_usage(None, actions, groups, prefix)
        else:
            return super(HelpFormatter, self)._format_usage(usage, actions, groups, prefix)
       
    def format_help(self):
        help = super(HelpFormatter, self).format_help()
        if help.count('\n') > 2:
            return help+'\n'
        else:
            return help
            
    
class SubParsersAction(argparse._SubParsersAction):
    '''A replacement for _SubParsersAction that keeps
    track of the parent parser'''
    
    def __init__(self, **kw):
        self.parent = kw.pop('parent')
        super(SubParsersAction, self).__init__(**kw)
        
    def add_parser(self, *a, **kwargs):
        '''Pass parent usage and add_help attributes to new parser'''
        
        if 'usage' not in kwargs:
            # Inherit, but preserve old progs attribute
            usage = self.parent.usage
            repl =  dict(prog=self.parent.prog)
            if isinstance(usage, list):
                usage = [ (x % repl if isinstance(x, str) else x)
                           for x in usage ]
            elif usage:
                usage = usage % repl
            kwargs['usage'] = usage
            
        if 'help' in kwargs:
            kwargs.setdefault('description', kwargs['help'].capitalize() + '.')
            
        kwargs.setdefault('add_help', self.parent.add_help)
        kwargs.setdefault('formatter_class', self.parent.formatter_class)
        
        if 'parents' in kwargs:
            for p in kwargs['parents']:
                if p.epilog:
                    kwargs.setdefault('epilog', p.epilog % dict(prog=self.parent.prog))
            
        return super(SubParsersAction, self).add_parser(*a, **kwargs)
    
        
class ArgumentParser(argparse.ArgumentParser):
        
    def __init__(self, *a, **kw):
        if 'formatter_class' not in kw:
            kw['formatter_class'] = HelpFormatter
        
        super(ArgumentParser, self).__init__(*a, **kw) 
        self.register('action', 'parsers', SubParsersAction)
        
    def add_version(self):
        self.add_argument('--version', action='version', 
                          help="just print program version and exit",
                          version='S3QL %s' % s3ql.VERSION)

    def add_quiet(self):
        self.add_argument("--quiet", action="store_true", default=False,
                          help="be really quiet")
    
    def add_debug_modules(self):
        self.add_argument("--debug", action="append", metavar='<module>',
                          help="activate debugging output from <module>. Use `all` "
                          "to get debug messages from all modules. This option can be "
                          "specified multiple times.")

    def add_debug(self):
        self.add_argument("--debug", action="store_const", const=['all'],
                          help="activate debugging output")
                        
    def add_authfile(self):
        self.add_argument("--authfile", type=str, metavar='<path>',
                      default=os.path.expanduser("~/.s3ql/authinfo2"),
                      help='Read authentication credentials from this file '
                           '(default: `~/.s3ql/authinfo2)`')
    def add_cachedir(self):
        self.add_argument("--cachedir", type=str, metavar='<path>',
                      default=os.path.expanduser("~/.s3ql"),
                      help='Store cached data in this directory '
                           '(default: `~/.s3ql)`')
        
    def add_log(self, default='none'):
        def log_handler(s):
            if s.lower() == 'none':
                return None
            elif s.lower() == 'syslog':
                handler = logging.handlers.SysLogHandler('/dev/log')
                formatter = logging.Formatter(os.path.basename(sys.argv[0])
                                               + '[%(process)s] %(threadName)s: '
                                               + '[%(name)s] %(message)s')
            else:
                handler = logging.handlers.RotatingFileHandler(os.path.expanduser(s), 
                                                            maxBytes=1024**2, backupCount=5)
                formatter = logging.Formatter('%(asctime)s.%(msecs)03d [%(process)s] %(threadName)s: '
                                              '[%(name)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
                
            handler.setFormatter(formatter)
            return handler        
                                
        self.add_argument("--log", type=log_handler, metavar='<target>', default=default,
                      help='Write logging info into this file. File will be rotated when '
                           'it reaches 1 MB, and at most 5 old log files will be kept. '
                           'Specify ``none`` to disable logging. Default: ``%(default)s``')
        
    def add_storage_url(self):
        self.add_argument("storage_url", metavar='<storage-url>',
                          type=storage_url_type, 
                          help='Storage URL of the backend that contains the file system')
                 
    def add_subparsers(self, **kw):
        '''Pass parent and set prog to default usage message'''
        kw.setdefault('parser_class', argparse.ArgumentParser)
        
        kw['parent'] = self
        
        # prog defaults to the usage message of this parser, skipping
        # optional arguments and with no "usage:" prefix
        if kw.get('prog') is None:
            formatter = self._get_formatter()
            positionals = self._get_positional_actions()
            groups = self._mutually_exclusive_groups
            formatter.add_usage(None, positionals, groups, '')
            kw['prog'] = formatter.format_help().strip()
                  
        return super(ArgumentParser, self).add_subparsers(**kw)  
 

def storage_url_type(s):
    '''Validate and canonicalize storage url'''

    if s.startswith('local://'):
        return 'local://%s' % os.path.abspath(s[len('local://'):])
    elif s.startswith('s3://') or s.startswith('s3rr://'):
        return s
    elif re.match(r'^([a-z]+)://([a-zA-Z0-9.-]+)(?::([0-9]+))?(/[a-zA-Z0-9./_-]+)$',
                  s):
        return s
    else:
        msg = '%s is not a valid storage url.' % s
        raise argparse.ArgumentTypeError(msg)
