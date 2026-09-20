#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tool discovery -- no machine-specific or product-specific paths baked in.

Every tool is resolved in the same order:

  1. explicit environment variable   (EOS_JAVAP / EOS_JAVA / EOS_CFR / ...)
  2. PATH
  3. well-known install locations for Windows, Linux and macOS

Nothing here assumes a JDK vendor, a drive letter, a product name, or a
directory layout. If a tool cannot be found, the getters return None and callers
must degrade gracefully -- which is why the pure-Python class parser is the
default backend: it needs no JDK at all.
"""

import os
import shutil
import sys

IS_WINDOWS = os.name == 'nt'
EXE = '.exe' if IS_WINDOWS else ''

#: where a decompiler is commonly dropped; the skill's own tools/ dir comes first
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CFR_NAMES = ('cfr.jar', 'cfr-0.152.jar', 'cfr-0.150.jar')
_PROCYON_NAMES = ('procyon-decompiler.jar', 'procyon-decompiler-0.6.0.jar')

#: well-known JDK install roots to PROBE (not dependencies): a path here is only a
#: candidate, tried after the env var and after PATH. Vendors are listed because a
#: machine may have exactly one of them; missing ones are simply skipped.
_JDK_ROOTS_WIN = (
    r'C:\Program Files\Eclipse Adoptium',
    r'C:\Program Files\Java',
    r'C:\Program Files\Microsoft',
    r'C:\Program Files\Amazon Corretto',
    r'C:\Program Files\BellSoft',
    r'C:\Program Files\Zulu',
    r'C:\Program Files\Semeru',
    r'C:\Program Files\IBM',
)
_JDK_ROOTS_NIX = (
    '/usr/lib/jvm', '/usr/java', '/opt/java', '/opt/jdk', '/opt/jdk-*',
    '/Library/Java/JavaVirtualMachines',
)


def _from_env(varnames):
    for v in varnames:
        p = os.environ.get(v)
        if p and os.path.exists(p):
            return p
    return None


def _from_path(names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _jdk_bin_roots():
    """Yield candidate <jdk>/bin directories, newest-looking first."""
    roots = []
    for r in (_JDK_ROOTS_WIN if IS_WINDOWS else _JDK_ROOTS_NIX):
        if not os.path.isdir(r):
            continue
        try:
            entries = sorted(os.listdir(r), reverse=True)
        except OSError:
            continue
        for e in entries:
            p = os.path.join(r, e)
            if os.path.isdir(p):
                roots.append(p)
        roots.append(r)
    # an explicit JAVA_HOME wins over any probing
    jh = os.environ.get('JAVA_HOME')
    if jh and os.path.isdir(jh):
        roots.insert(0, jh)
    return roots


def find_java_tool(tool):
    """Locate a JDK tool (`javap`, `java`, `jar`) or return None."""
    direct = _from_env(['EOS_%s' % tool.upper(), '%s_BIN' % tool.upper()])
    if direct:
        return direct
    hit = _from_path([tool + EXE, tool])
    if hit:
        return hit
    for root in _jdk_bin_roots():
        cand = os.path.join(root, 'bin', tool + EXE)
        if os.path.exists(cand):
            return cand
    return None


def javap_path():
    return find_java_tool('javap')


def java_path():
    return find_java_tool('java')


def decompiler_path(kind='cfr'):
    """Locate a decompiler jar. `kind` is 'cfr' or 'procyon'."""
    names = _CFR_NAMES if kind == 'cfr' else _PROCYON_NAMES
    var = 'EOS_CFR' if kind == 'cfr' else 'EOS_PROCYON'
    p = _from_env([var, 'EOS_DECOMPILER'])
    if p:
        return p
    dirs = [
        os.environ.get('EOS_TOOLS_DIR'),
        os.path.join(_SKILL_ROOT, 'tools'),
        os.path.join(_SKILL_ROOT, 'scripts', 'tools'),
        os.path.join(os.path.expanduser('~'), '.dsh', 'tools', 'java-decompilers'),
        os.path.join(os.path.expanduser('~'), 'tools'),
    ]
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for n in names:
            cand = os.path.join(d, n)
            if os.path.exists(cand):
                return cand
        # any cfr*.jar / procyon*.jar in the directory
        try:
            for f in sorted(os.listdir(d)):
                low = f.lower()
                if kind == 'cfr' and low.startswith('cfr') and low.endswith('.jar'):
                    return os.path.join(d, f)
                if kind == 'procyon' and low.startswith('procyon') and low.endswith('.jar'):
                    return os.path.join(d, f)
        except OSError:
            pass
    # last resort: a decompiler jar on PATH (some distros ship a wrapper)
    return _from_path(names)


def work_dir(subdir=None):
    """Scratch directory for reports and extracted classes.

    Never inside the media: the skill is strictly read-only on the product.
    """
    base = os.environ.get('EOS_RC_WORK')
    if not base:
        base = os.path.join(os.path.expanduser('~'), '.log-to-rootcause', 'work')
    if subdir:
        base = os.path.join(base, subdir)
    os.makedirs(base, exist_ok=True)
    return base


def describe():
    """Human-readable tool inventory, for reports and troubleshooting."""
    return {
        'python': sys.executable,
        'javap': javap_path(),
        'java': java_path(),
        'cfr': decompiler_path('cfr'),
        'procyon': decompiler_path('procyon'),
        'workDir': work_dir(),
        'note': 'javap/java are only needed for bytecode TEXT and cross-checks; '
                'the default backend parses class files in pure Python.',
    }


if __name__ == '__main__':
    import json
    print(json.dumps(describe(), indent=1, ensure_ascii=False))
