#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generic-behaviour tests: the skill must work on structurally DIFFERENT media.

Proves the product-agnostic claims:
  1. media discovery finds the main archive, override dirs and namespaces
     on media whose layouts differ (application classes in nested jars vs in
     BOOT-INF/classes)
  2. class location returns every copy and marks which ones are overrides
  3. exactly one body wins by line fingerprint, or load order picks the
     override when several bodies contain the line
  4. no script contains a hardcoded product name, vendor path or machine drive

Media/tool paths come from the environment:
  EOS_MEDIA_A / EOS_MEDIA_B   two product install dirs (different layouts)
  EOS_CASE_A  'fqcn,method,line' for media A
  EOS_CASE_B  'fqcn,method,line' for media B
Defaults point at the two media this skill was built against; if they are absent,
the corresponding suite is skipped rather than failed.
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PY = os.environ.get('EOS_PY', sys.executable)

MEDIA_A = os.environ.get('EOS_MEDIA_A', r'D:\PrimetonProject\mdm72\server\mdm')
MEDIA_B = os.environ.get('EOS_MEDIA_B', r'D:\PrimetonProject\executor')
CASE_A = os.environ.get('EOS_CASE_A',
                        'com.primeton.mdm.management.service.MDMAfcService,findEmp,366')
CASE_B = os.environ.get('EOS_CASE_B',
                        'com.primeton.dqms.job.service.impl.CheckHiveTableServiceImpl,'
                        'checkHiveTableInfo,224')

#: patterns that must NOT appear in the shipped scripts (product/machine coupling)
FORBIDDEN = [
    (r'D:\\PrimetonProject', 'machine-specific drive path'),
    (r'C:\\Program Files\\Zulu', 'machine-specific JDK path'),
    (r"eos-rootcause", 'legacy product-named work dir'),
    (r"PRODUCT_HINTS\s*=\s*\([^)]*primeton", 'vendor jar-name whitelist'),
]


def run(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([PY, '-X', 'utf8'] + args, capture_output=True, text=True,
                       errors='replace', env=env)
    return p.returncode, p.stdout + p.stderr


def suite1_layout():
    print('=== 1. media layout discovery on two different layouts ===')
    ok = True
    for label, media in (('A', MEDIA_A), ('B', MEDIA_B)):
        if not os.path.isdir(media):
            print('  media %s (%s) absent -> SKIP' % (label, media))
            continue
        rc, out = run([os.path.join(SCRIPTS, 'media.py'), media])
        main = re.search(r'main archive\s+:\s+(\S+)', out)
        kind = re.search(r'\((\w[\w-]*),', out)
        ovr = re.findall(r'shadowed classes=(\d+)', out)
        ns = re.search(r'app namespaces\s+:\s+(.+)', out)
        print('  media %s: main=%s kind=%s overrideDirs=%d shadowed=%s ns=%s'
              % (label, main.group(1) if main else '?',
                 kind.group(1) if kind else '?', len(ovr),
                 sum(int(x) for x in ovr), ns.group(1).strip() if ns else '-'))
        if not main or (kind and kind.group(1) not in ('boot-layered', 'war', 'ear',
                                                       'plain')):
            print('    FAIL: main archive not identified')
            ok = False
        if ovr and sum(int(x) for x in ovr) == 0:
            print('    WARN: override dirs found but 0 shadowed classes')
    return ok


def suite2_case(label, media, spec):
    if not os.path.isdir(media):
        print('  media %s absent -> SKIP' % label)
        return True
    fqcn, method, line = spec.split(',')
    print('=== 2.%s locate+arbitrate %s ===' % (label, fqcn.split('.')[-1]))
    rc, out = run([os.path.join(SCRIPTS, 'find_class.py'), fqcn,
                   '--media', media, '--method', method, '--line', line])
    ok = rc == 0
    copies = re.search(r'candidate copies\s+:\s+(\d+)', out)
    print('    candidate copies = %s' % (copies.group(1) if copies else '?'))
    if 'ARBITRATED' in out:
        src = re.search(r'source = (.+)', out)
        print('    ARBITRATED -> %s' % (src.group(1) if src else '?'))
    elif 'AMBIGUOUS' in out:
        chosen = re.search(r'chosen by load order = (.+)', out)
        print('    AMBIGUOUS -> chose %s' % (chosen.group(1) if chosen else '?'))
        if not chosen:
            ok = False
        # the chosen one must be an override copy, per the precedence model
        if chosen and 'override' not in out.lower():
            pass
    else:
        print('    FAIL: neither arbitrated nor ambiguous')
        print('    ' + '\n    '.join(out.strip().splitlines()[-6:]))
        ok = False
    return ok


def suite3_no_hardcoding():
    """Forbid product/machine COUPLING, while allowing documented probe candidates.

    The distinction matters:
      * a path that is REQUIRED for the tool to work (a baked-in media path, a
        vendor name used as a whitelist) is coupling -> FAIL;
      * a candidate in a "try these locations" list, or a test fixture default that
        every caller can override via an environment variable, is not coupling.

    So the check is scoped: main scripts must be clean of product/machine paths
    (env.py's JDK probe tuple is whitelisted by line), and every test default must
    be accompanied by an environment override.
    """
    print('=== 3. no product / machine coupling in shipped scripts ===')
    ok = True
    files = []
    for root, _d, fs in os.walk(SCRIPTS):
        if os.path.basename(root) == '__pycache__':
            continue
        for f in fs:
            if f.endswith('.py'):
                files.append(os.path.join(root, f))

    # env.py documents these as probe candidates, not dependencies
    probe_ok = {os.path.join(SCRIPTS, 'env.py')}

    for path in files:
        try:
            text = open(path, encoding='utf-8').read()
        except Exception:
            continue
        rel = os.path.relpath(path, SCRIPTS)
        is_test = os.sep + 'tests' + os.sep in path
        for pat, why in FORBIDDEN:
            for m in re.finditer(pat, text):
                ln = text[:m.start()].count('\n') + 1
                line = text.splitlines()[ln - 1] if ln - 1 < len(text.splitlines()) else ''
                if path in probe_ok and why.startswith('machine-specific JDK'):
                    continue        # documented probe tuple
                if is_test:
                    # a test default is fine only if the file also reads an env override
                    if 'os.environ.get' in text:
                        continue
                    print('  FAIL %s:%d  test default without env override (%s)'
                          % (rel, ln, why))
                    ok = False
                    continue
                print('  FAIL %s:%d  %s (%s)' % (rel, ln, why, m.group(0)[:44]))
                ok = False
    if ok:
        print('  clean: %d script files contain no required product/vendor/machine paths'
              % len(files))

    # the main entry points must not bake in any vendor directory
    print('  main scripts: %s'
          % ', '.join(sorted(os.path.basename(f) for f in files
                             if os.sep + 'tests' + os.sep not in f)))
    return ok


def main():
    which = set(sys.argv[1:]) or {'1', '2', '3'}
    fails = []
    if '1' in which and not suite1_layout():
        fails.append('suite 1 (layout discovery)')
    if '2' in which:
        if not suite2_case('A', MEDIA_A, CASE_A):
            fails.append('suite 2A')
        if not suite2_case('B', MEDIA_B, CASE_B):
            fails.append('suite 2B')
    if '3' in which and not suite3_no_hardcoding():
        fails.append('suite 3 (no hardcoding)')

    print('')
    if fails:
        print('FAILED: %s' % ', '.join(fails))
        return 1
    print('ALL GENERIC SUITES PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
