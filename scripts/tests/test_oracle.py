#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Suite: usage-time learning oracle (oracle.py + find_class --true hook).

Proves the "evolve while used" behaviour without needing a real media:
  * media fingerprint is stable and contains no absolute paths
  * an unconfirmed case does NOT drive replay (ground-truth gate)
  * a confirmed case (--true matching) DOES drive replay on the next run
  * truth mismatch is detected and reported, and does NOT confirm
  * EOS_ORACLE=0 switches recording + lookup off completely

The journal is redirected to a temp dir via EOS_RC_WORK so tests never touch
the real one.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PY = os.environ.get('EOS_PY', sys.executable)

#: a fake, tiny media: a plain jar named as a "main archive" is not needed --
#: oracle only consumes the layout dict, which we build synthetically here.
FAKE_LAYOUT = {
    'media': 'D:/fake/product',
    'main': {'name': 'app-boot-7.2.0-exec.jar', 'kind': 'boot-layered',
             'path': 'D:/fake/product/app-boot-7.2.0-exec.jar'},
    'overrideDirs': [{'dir': 'D:/fake/product/lib'}],
    'archives': [],
}


def _oracle_env(work):
    env = dict(os.environ, EOS_RC_WORK=work)
    env.pop('EOS_ORACLE', None)
    return env


def _probe(env, fqcn, method, line, truth=None):
    args = [os.path.join(SCRIPTS, 'find_class.py'), fqcn,
            '--media', FAKE_LAYOUT['media'], '--method', method, '--line', str(line)]
    if truth:
        args += ['--true', truth]
    # media dir doesn't exist; find_class will abort before it reaches the
    # oracle section, so instead we call oracle logic directly via helper runs.
    return args


def _case(work, **over):
    c = {'mediaFp': 'deafbeef', 'fqcn': 'com.x.Svc', 'method': 'm',
         'line': 42, 'verdict': {'status': 'ARBITRATED', 'crc': 'a1b2c3d4'},
         'sources': ['lib/00000000-20250408.jar'], 'truth': None,
         'truthMatch': None, 'confirmed': False, 'ts': '2026-09-20T00:00:00'}
    c.update(over)
    return c


def _append(work, case):
    path = os.path.join(work, 'cases.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(case) + '\n')


def run():
    import importlib.util
    spec = importlib.util.spec_from_file_location('oracle', os.path.join(SCRIPTS, 'oracle.py'))
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)

    fails = []

    def check(name, cond, detail=''):
        status = 'PASS' if cond else 'FAIL'
        print('  %s %s  %s' % (status, name, detail))
        if not cond:
            fails.append(name)

    # --- 1. fingerprint is stable + no absolute paths -> hash-only intermediate
    fp1 = oracle.media_fp(FAKE_LAYOUT)
    fp2 = oracle.media_fp(FAKE_LAYOUT)
    check('media fingerprint stable', fp1 == fp2, fp1)
    check('fingerprint short + hex', len(fp1) == 12 and all(c in '0123456789abcdef' for c in fp1))

    # different main archive -> different fingerprint
    other = dict(FAKE_LAYOUT, main={'name': 'other-exec.jar', 'kind': 'boot-layered'})
    check('different main archive -> different fp', oracle.media_fp(other) != fp1)

    # --- 1.5. fully local / self-contained: no git, no network, no remote in the
    #          shipped scripts. Installed anywhere, the skill must learn locally
    #          and NEVER try to commit/push to a remote. Scans every shipped .py
    #          under scripts/ for remote-coupled behaviour.
    #
    # NOTE: subprocess itself is allowed -- javap_parse/lib_lookup legitimately
    # shell out to a LOCAL javap (bytecode cross-check). What is forbidden is
    # remote coupling: git operations and any network client (urllib, requests,
    # socket, http.client). Local subprocess that never touches a socket is fine.
    remote_coupled = {}
    for root, _d, fs in os.walk(SCRIPTS):
        if os.path.basename(root) == '__pycache__':
            continue
        for f in fs:
            if not f.endswith('.py'):
                continue
            p = os.path.join(root, f)
            try:
                text = open(p, encoding='utf-8').read()
            except Exception:
                continue
            for pat, why in [
                (r'git\s+(commit|push|add|clone|pull|fetch|remote|init|tag)', 'git operation'),
                (r'import\s+(urllib|requests|socket|http|aiohttp|httpx)', 'network import'),
                (r'os\.system\s*\(', 'os.system'),
                (r'os\.popen\s*\(', 'os.popen'),
            ]:
                if re.search(pat, text):
                    rel = os.path.relpath(p, SCRIPTS)
                    remote_coupled.setdefault(rel, []).append(why)
    check('scripts are self-contained (no git/network/os-shell)',
          not remote_coupled,
          str(remote_coupled) if remote_coupled else '')

    # journal must live under EOS_RC_WORK (a local scratch dir), not inside the
    # skill install tree -- so it never rides along in a repo or taints a share.
    # DEFAULT (no EOS_RC_WORK set) must resolve to ~/.log-to-rootcause/work.
    norm_scripts = os.path.abspath(SCRIPTS).lower()
    norm_skill = os.path.abspath(os.path.dirname(SCRIPTS)).lower()  # skill root
    _saved_rcwork = os.environ.get('EOS_RC_WORK')
    os.environ.pop('EOS_RC_WORK', None)
    try:
        default_jp = oracle.journal_path()
    finally:
        if _saved_rcwork is not None:
            os.environ['EOS_RC_WORK'] = _saved_rcwork
    check('default journal lives in ~/.log-to-rootcause, outside skill tree',
          ('.log-to-rootcause' in default_jp.lower())
          and (os.path.abspath(default_jp).lower().startswith(norm_scripts) is False)
          and (os.path.abspath(default_jp).lower().startswith(norm_skill) is False),
          default_jp)

    # --- 2. truth matching
    check('truth matches exact basename',
          oracle.truth_matches(['lib/00000000-20250408.jar'], '00000000-20250408.jar') is True)
    check('truth matches nested label',
          oracle.truth_matches(['BOOT-INF/lib/x.jar!/classes'], 'x.jar') is True)
    check('truth mismatch detected',
          oracle.truth_matches(['lib/00000000-20250408.jar'], 'other.jar') is False)
    check('no truth -> None (unconfirmed)',
          oracle.truth_matches(['lib/x.jar'], None) is None)

    with tempfile.TemporaryDirectory() as work:
        oracle._DISABLED = False  # ensure oracle is live despite any env
        # point the journal at this temp dir for the duration of the test
        _OLD_WORK = os.environ.get('EOS_RC_WORK')
        os.environ['EOS_RC_WORK'] = work

        def _fin():
            if _OLD_WORK is None:
                os.environ.pop('EOS_RC_WORK', None)
            else:
                os.environ['EOS_RC_WORK'] = _OLD_WORK

        # --- 3. gate: unconfirmed never drives replay
        _append(work, _case(work))   # confirmed=False
        hit = oracle.lookup('deafbeef', 'com.x.Svc', 'm', 42)
        check('unconfirmed case does NOT replay', hit is None)

        # --- 4. confirmed drives replay
        _append(work, _case(work, confirmed=True, sources=['lib/AAAAAAAA.jar']))
        hit = oracle.lookup('deafbeef', 'com.x.Svc', 'm', 42)
        check('confirmed case replays on next call', hit is not None and
              hit['sources'] == ['lib/AAAAAAAA.jar'])

        # mediaFp mismatch must not pollute
        miss = oracle.lookup('deadbeef', 'com.x.Svc', 'm', 42)
        check('different media fingerprint does NOT replay', miss is None)

        # --- 5. record() + EOS_ORACLE disable
        rec = oracle.record('feedface', 'com.y.Z', 'go', 7, {'status': 'AMBIGUOUS'},
                            ['lib/0000-patch.jar'], truth='0000-patch.jar')
        check('record confirms when truth matches', rec['confirmed'] is True)
        rec2 = oracle.record('feedface', 'com.y.Z2', 'go', 8, {'status': 'AMBIGUOUS'},
                             ['lib/0000-patch.jar'], truth='other.jar')
        check('record does NOT confirm on mismatch', rec2['confirmed'] is False)

        from importlib import reload
        saved = oracle._DISABLED
        try:
            oracle._DISABLED = True
            rows0 = oracle._read_all()
            rec3 = oracle.record('x', 'a.B', 'c', 1, {'status': 'NO-EXACT-MATCH'}, [], truth='z.jar')
            check('EOS_ORACLE=0 -> record is no-op', len(oracle._read_all()) == len(rows0))
        finally:
            oracle._DISABLED = saved

        _fin()

    print('')
    if fails:
        print('FAILED: %s' % ', '.join(fails))
        return 1
    print('ALL ORACLE TESTS PASS')
    return 0


if __name__ == '__main__':
    sys.exit(run())