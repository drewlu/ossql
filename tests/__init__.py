'''
__init__.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2008-2009 Nikolaus Rath <Nikolaus@rath.org>

This program can be distributed under the terms of the GNU GPLv3.
'''

from __future__ import division, print_function


# Export all modules
import os
testdir = os.path.dirname(__file__)
__all__ = [ name[:-3] for name in os.listdir(testdir) if name.endswith(".py") and
            name != '__init__.py' ]
