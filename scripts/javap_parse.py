#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Robust member/line parser for the media's .class files.

TWO BACKENDS, same result shape {signature: [(source_line, bytecode_offset), ...]}:

  * 'py'    (default) -- reads the class file directly in Python (classfile.py).
            NO JVM, NO subprocess. Benchmarked on this media: 0.002 s per class
            vs 0.36 s for javap -- about 178x faster. Validated to reproduce
            javap's LineNumberTable EXACTLY on all 134 product classes that have
            code (validate_classfile.py reports 0 mismatches).
  * 'javap' -- shells out to `javap -p -l`. Kept as a cross-check backend; use it
            when you suspect the pure-Python parser or want bytecode text too.

Both backends must return identical line rows; `--backend javap` on the lookup
scripts is how you confirm that on any specific class.

WHY THE OLD REGEX APPROACH WAS ABANDONED (measured on this media):
A regex block splitter looking for "<modifiers> <type> <name>(" silently drops

  1. CONSTRUCTORS -- javap prints `public com.x.Y();` with NO SPACE between the
     type path and the name, so the lookahead never matches and the constructor's
     LineNumberTable is merged into the previous member.
  2. `static {}` / <clinit> -- contains no "(" at all, so it can never match.

A 60-class audit produced 62 violations; 72 of the 142 product classes contain
`static {}` and 120 contain constructors. The pure-Python backend sidesteps the
whole problem: it reads the method table directly.
"""

import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import classfile as _cf  # noqa: E402
import env as _env       # noqa: E402

#: resolved at import time; None when no JDK is installed. The default backend does
#: not need it -- javap is only for bytecode text and cross-checks.
JAVAP = _env.javap_path()

#: a javap declaration line: exactly two leading spaces, trailing ';'
_DECL_RE = re.compile(r'^  \S.*;\s*$')


def is_decl_line(line):
    return bool(_DECL_RE.match(line))


def member_name(sig):
    """Stable short name for a member signature.

    - `static {};`            -> <clinit>
    - `<init>(...)`           -> <init>   (pure-Python backend: JVM spelling)
    - `public com.x.Y();`     -> Y        (javap backend: class simple name)
    - `public void foo(int);` -> foo
    """
    s = sig.strip().rstrip(';').strip()
    if s.endswith('{}') or s == 'static {}':
        return '<clinit>'
    head = s.split('(', 1)[0].strip()
    if not head:
        return s
    name = head.split()[-1]
    if '.' in name:                       # javap prints constructors fully qualified
        return name.rsplit('.', 1)[-1]
    return name


# --------------------------------------------------------------------- backend
def parse_members_py(jar, fqcn, entry=None):
    """Parse from the class file directly (no JVM).

    The class file stores constructors as `<init>`; javap prints the class simple
    name. Rename here so BOTH backends produce identical member names and the
    caller's output does not change when switching backends.
    """
    entry = entry or (fqcn.replace('.', '/') + '.class')
    data = _cf.class_bytes_in_jar(jar, entry)
    members = _cf.members_from_class_bytes(data)
    if not members:
        return {}, 'NO-LINE-TABLE(compiled without -g, or class has no code)'
    simple = fqcn.rsplit('.', 1)[-1]
    if any('<init>' in sig for sig in members):
        renamed = {}
        for sig, rows in members.items():
            if sig.startswith('<init>'):
                sig = simple + sig[len('<init>'):]
            renamed[sig] = rows
        members = renamed
    return members, None


def parse_members_javap(jar, fqcn, with_bytecode=False, javap=JAVAP):
    """Parse `javap -p -l [-c]` text (line-by-line, never a block regex)."""
    flags = ['-p', '-l'] + (['-c'] if with_bytecode else [])
    proc = subprocess.run([javap] + flags + ['-cp', jar, fqcn],
                          capture_output=True, text=True, errors='replace')
    text = proc.stdout
    if not text.strip():
        return {}, 'EMPTY-OUTPUT(rc=%s)' % proc.returncode

    members = {}
    cur_sig = None
    buf = []

    def flush():
        if cur_sig is None:
            return
        rows = [(int(a), int(b)) for a, b in
                re.findall(r'line (\d+): (\d+)', '\n'.join(buf))]
        if rows:
            members[cur_sig] = members.get(cur_sig, []) + rows

    for line in text.splitlines():
        if is_decl_line(line):
            flush()
            cur_sig = line.strip()
            buf = []
        else:
            buf.append(line)
    flush()

    if not members:
        return {}, 'NO-LINE-TABLE(compiled without -g, or class has no code)'
    return members, None


def parse_members(jar, fqcn, javap=JAVAP, with_bytecode=False, backend='py'):
    """Dispatch to the requested backend (default: fast pure-Python)."""
    if backend == 'javap':
        return parse_members_javap(jar, fqcn, with_bytecode, javap)
    return parse_members_py(jar, fqcn)


def build_line_map(members):
    """Class-wide map: source line -> [(member_name, offset, signature), ...].

    This is the structure that makes line resolution correct. It spans ordinary
    AND synthetic members, so a line that looks like a "hole" in an outer method
    (e.g. findEmp line 367) resolves to the lambda that really owns it.
    """
    line_map = {}
    for sig, rows in members.items():
        nm = member_name(sig)
        for ln, off in rows:
            line_map.setdefault(ln, []).append((nm, off, sig))
    return line_map


def counts_for_audit(jar, fqcn, javap=JAVAP):
    """(parser_member_count, javap_line_number_table_count) for invariant checks."""
    proc = subprocess.run([javap, '-p', '-l', '-cp', jar, fqcn],
                          capture_output=True, text=True, errors='replace')
    truth = proc.stdout.count('LineNumberTable:')
    members, _err = parse_members_javap(jar, fqcn, javap=javap)
    return len(members), truth, proc.stdout


if __name__ == '__main__':
    jarpath, cls = sys.argv[1], sys.argv[2]
    backend = sys.argv[3] if len(sys.argv) > 3 else 'py'
    m, err = parse_members(jarpath, cls, backend=backend)
    print('backend=%s  members with line table = %d  err=%s' % (backend, len(m), err))
    for sig, rows in m.items():
        print('  %-52s lines %s' % (member_name(sig), sorted({r[0] for r in rows})))
