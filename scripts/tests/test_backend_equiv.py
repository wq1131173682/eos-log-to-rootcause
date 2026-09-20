"""Backend-equivalence test: py vs javap must produce IDENTICAL script output.

Runs each lookup scenario twice (--backend py / --backend javap) and diffs the
output, so the fast path can never silently change a conclusion.
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
S = os.path.dirname(HERE)

PY = os.environ.get('EOS_PY', sys.executable)
LIB = os.environ.get('EOS_LIB', r'D:\PrimetonProject\mdm72\server\mdm\lib')
WORK = os.environ.get('EOS_RC_WORK', os.path.join(HERE, '_testwork'))
env = dict(os.environ, EOS_RC_WORK=WORK)

SCENARIOS = [
    ('lib_lookup findEmp:366',
     ['lib_lookup.py', 'com.primeton.mdm.management.service.MDMAfcService',
      '--method', 'findEmp', '--line', '366']),
    ('lib_lookup 367 hole',
     ['lib_lookup.py', 'com.primeton.mdm.management.service.MDMAfcService',
      '--method', 'findEmp', '--line', '367']),
    ('line_lookup findEmp:366',
     ['line_lookup.py', 'com.primeton.mdm.management.service.MDMAfcService',
      '--method', 'findEmp', '--line', '366']),
    ('line_lookup lambda:367',
     ['line_lookup.py', 'com.primeton.mdm.management.service.MDMAfcService',
      '--method', 'findEmp', '--line', '367']),
    ('line_lookup ctor',
     ['line_lookup.py', 'com.primeton.gocom.afcenter.resource.service.ResourceTemplateServiceImpl',
      '--method', '<init>', '--line', '20']),
    ('line_lookup static{}',
     ['line_lookup.py', 'com.primeton.mdm.management.spi.impl.MDMTwoElementValidator$1',
      '--method', '<clinit>', '--line', '74']),
    ('line_lookup --cross',
     ['line_lookup.py', 'com.primeton.mdm.management.service.MDMAfcService',
      '--method', 'findEmp', '--line', '366', '--cross']),
]

# lines that legitimately differ between backends by design (timings only)
IGNORE = re.compile(r'\d+\.\d+s|lib scan\s*:|report ->')


def norm(out):
    keep = []
    for ln in out.splitlines():
        if IGNORE.search(ln):
            # keep the line but blank out the timing numbers themselves
            ln = re.sub(r'\d+\.\d+s', 'Ts', ln)
            ln = re.sub(r'lib scan\s*:\s*\d+\.\d+ s', 'lib scan : Ts', ln)
        # the backend label is echoed by design; it is not a result
        ln = re.sub(r'backend=\w+', 'backend=X', ln)
        keep.append(ln.rstrip())
    return '\n'.join(keep).strip()


fails = []
for label, args in SCENARIOS:
    outs = {}
    times = {}
    for backend in ('py', 'javap'):
        cmd = [PY, '-X', 'utf8', os.path.join(S, args[0])] + args[1:] + \
              ['--lib', LIB, '--backend', backend]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, errors='replace', env=env)
        times[backend] = time.time() - t0
        outs[backend] = (p.returncode, norm(p.stdout + p.stderr))
    same = outs['py'] == outs['javap']
    sp = times['javap'] / times['py'] if times['py'] else 0
    print('%-4s %-34s py=%.2fs javap=%.2fs  %.1fx %s'
          % ('PASS' if same else 'FAIL', label, times['py'], times['javap'], sp,
             '' if same else '  <-- OUTPUT DIFFERS'))
    if not same:
        fails.append((label, outs))

print('')
print('scenarios=%d  differing=%d' % (len(SCENARIOS), len(fails)))
for label, outs in fails:
    print('')
    print('=== DIFF in %s ===' % label)
    a = outs['py'][1].splitlines()
    b = outs['javap'][1].splitlines()
    import difflib
    for line in list(difflib.unified_diff(b, a, 'javap', 'py', lineterm=''))[:40]:
        print(line)
sys.exit(1 if fails else 0)
