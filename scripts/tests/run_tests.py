#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validation suite for the eos-log-to-rootcause scripts.

Run this after ANY change to classfile.py / javap_parse.py / lib_lookup.py /
line_lookup.py / fatjar_lookup.py. It re-proves the two guarantees the skill
relies on, plus the line-number edge cases.

  1. pure-Python parser == javap, byte for byte, on every product class
  2. the two backends produce IDENTICAL script output (7 scenarios)
  3. the line-number edge cases (hole / synthetic / cross-version / out of range)
  4. GENERIC behaviour: layout discovery on two different media, locate+arbitrate,
     and no product/vendor/machine coupling in the shipped scripts

Media and tool paths come from the environment so this works on any machine:
  EOS_LIB       media lib/ directory  (default: a known local media)
  EOS_FAT       fatjar                (default: a known local media)
  EOS_MEDIA_A / EOS_MEDIA_B           two media with different layouts (suite 4)
  EOS_RC_WORK   scratch/work dir      (default: ./_testwork next to this file)
  EOS_PY        python executable     (default: sys.executable)

Usage:
  python -X utf8 tests/run_tests.py            # all suites
  python -X utf8 tests/run_tests.py 1 3        # selected suites
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, SKILL)

PY = os.environ.get('EOS_PY', sys.executable)
LIB = os.environ.get('EOS_LIB', r'D:\PrimetonProject\mdm72\server\mdm\lib')
FAT = os.environ.get('EOS_FAT',
                     r'D:\PrimetonProject\mdm72\server\mdm\mall-boot-7.2.0-exec.jar'
                     .replace('mall-boot', 'mdmall-boot'))
WORK = os.environ.get('EOS_RC_WORK', os.path.join(HERE, '_testwork'))


def run(path, extra_env=None, args=None):
    import subprocess
    env = dict(os.environ, EOS_LIB=LIB, EOS_FAT=FAT, EOS_RC_WORK=WORK)
    if extra_env:
        env.update(extra_env)
    cmd = [PY, '-X', 'utf8', path] + (args or [])
    p = subprocess.run(cmd, capture_output=True, text=True, errors='replace', env=env)
    return p.returncode, p.stdout, p.stderr


def main():
    which = set(sys.argv[1:]) or {'1', '2', '3', '4'}
    os.makedirs(WORK, exist_ok=True)
    failures = []

    if '1' in which:
        print('=== 1. pure-Python parser vs javap (whole media) ===')
        rc, out, err = run(os.path.join(HERE, 'validate_classfile.py'))
        tail = '\n'.join((out + err).strip().splitlines()[-6:])
        print(tail)
        if rc != 0:
            failures.append('suite 1 (parser equivalence)')

    if '2' in which:
        print('')
        print('=== 2. backend output equivalence (py vs javap) ===')
        rc, out, err = run(os.path.join(HERE, 'test_backend_equiv.py'))
        tail = '\n'.join((out + err).strip().splitlines()[-4:])
        print(tail)
        if rc != 0:
            failures.append('suite 2 (backend equivalence)')

    if '3' in which:
        print('')
        print('=== 3. line-number edge cases ===')
        rc, out, err = run(os.path.join(HERE, 'regress_line.py'))
        tail = '\n'.join((out + err).strip().splitlines()[-4:])
        print(tail)
        if rc != 0:
            failures.append('suite 3 (line-number cases)')

    if '4' in which:
        print('')
        print('=== 4. generic behaviour (different media, no coupling) ===')
        rc, out, err = run(os.path.join(HERE, 'test_generic.py'))
        tail = '\n'.join((out + err).strip().splitlines()[-6:])
        print(tail)
        if rc != 0:
            failures.append('suite 4 (generic behaviour)')

    print('')
    if failures:
        print('FAILED: %s' % ', '.join(failures))
        return 1
    print('ALL SUITES PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
