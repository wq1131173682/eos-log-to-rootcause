"""Regression suite for line_lookup.py -- covers the three failure modes + edges."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)

PY = os.environ.get('EOS_PY', sys.executable)
SCRIPT = os.path.join(SCRIPTS, 'line_lookup.py')
LIB = os.environ.get('EOS_LIB', r'D:\PrimetonProject\mdm72\server\mdm\lib')
WORK = os.environ.get('EOS_RC_WORK', os.path.join(HERE, '_testwork'))
CLS = 'com.primeton.mdm.management.service.MDMAfcService'

CASES = [
    ('R1 exact hit, unique body',
     ['--method', 'findEmp', '--line', '366'], 0, ['EXACT', 'findEmp']),
    ('R1 line in comment hole inside one method',
     ['--method', 'buildDataTypes', '--line', '392'], 0, ['EXACT', 'comment']),
    ('R2 line owned by synthetic lambda',
     ['--method', 'findEmp', '--line', '367'], 0, ['SYNTHETIC', 'lambda$findEmp$10']),
    ('R3 same line, different method in other patch (disambiguation)',
     ['--method', 'findEmp', '--line', '367'], 0, ['NON-MATCH', 'listMdmUsers']),
    ('R3 cross-version drift -> no exact, neighbours differ',
     ['--method', 'findEmp', '--line', '366', '--cross'], 0,
     ['DIFFERENT', 'conversion', 'maps to LINE']),
    ('edge: line beyond end of class',
     ['--method', 'findEmp', '--line', '99999'], 0, ['NO EXACT ROW', 'RESULT']),
    ('edge: line before start of class',
     ['--method', 'findEmp', '--line', '1'], 0, ['NO EXACT ROW', 'RESULT']),
    ('edge: class not in lib',
     ['--line', '10'], 2, ['NOT IN lib/']),
]

env = dict(os.environ, EOS_RC_WORK=WORK)
fails = []
for name, extra, want_rc, wants in CASES:
    cls = CLS if 'class not in lib' not in name else 'com.primeton.gocom.afcenter.sdk.localclient.EmployeeLocalImpl'
    cmd = [PY, '-X', 'utf8', SCRIPT, cls] + extra + ['--lib', LIB]
    p = subprocess.run(cmd, capture_output=True, text=True, errors='replace', env=env)
    out = p.stdout + p.stderr
    ok_rc = (p.returncode == want_rc)
    missing = [w for w in wants if w not in out]
    status = 'PASS' if (ok_rc and not missing) else 'FAIL'
    if status == 'FAIL':
        fails.append((name, p.returncode, want_rc, missing))
    print('%-4s %-58s rc=%s(want %s) missing=%s' % (status, name, p.returncode, want_rc, missing))

print('')
print('total=%d  failed=%d' % (len(CASES), len(fails)))
if fails:
    for f in fails:
        print('  FAILED:', f)
    sys.exit(1)
print('ALL LINE-NUMBER CASES PASS')
