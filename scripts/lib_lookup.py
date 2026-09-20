#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DEPRECATED shim -- kept only so older command lines keep working.

Use `find_class.py` instead: it discovers the media layout by itself and assumes
no product, vendor, directory name or package namespace.

This wrapper maps the old `--lib <dir>` form onto the generic locator by treating
that directory as an explicit override area, and infers `--media` from it.
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIND = os.path.join(_HERE, 'find_class.py')


def main():
    sys.stderr.write('[note] lib_lookup.py is deprecated; '
                     'delegating to find_class.py\n')
    argv = sys.argv[1:]
    lib = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == '--lib' and i + 1 < len(argv):
            lib = argv[i + 1]
            i += 2
            continue
        if argv[i] in ('--work', '--javap') and i + 1 < len(argv):
            i += 2                      # accepted but no longer needed
            continue
        rest.append(argv[i])
        i += 1

    media = os.environ.get('EOS_MEDIA')
    if not media:
        media = os.path.dirname(os.path.abspath(lib)) if lib else os.getcwd()

    env = dict(os.environ)
    if lib:
        env['EOS_OVERRIDE_DIRS'] = lib
        env.setdefault('EOS_RC_WORK',
                       os.path.join(os.path.expanduser('~'), '.log-to-rootcause',
                                    'work'))
    if not any(a == '--media' for a in rest):
        rest += ['--media', media]
    sys.stderr.write('[note] --media inferred as %s '
                     '(set EOS_MEDIA to override)\n' % media)
    return subprocess.call([sys.executable, '-X', 'utf8', _FIND] + rest, env=env)


if __name__ == '__main__':
    sys.exit(main())
