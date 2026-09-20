#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Line-number tolerant class locator + patch arbitration for Primeton EOS/MDM.

Solves the line-number problem in THREE independent ways, because a logged line
may fail to match for three different reasons:

  (R1) SPARSE LINE TABLE -- javac only records a line when it produced bytecode.
       Measured (20250408 body): 34 of 60 members have holes, and 673 of 1111
       spanned lines (61%) carry NO row. So judging "does this method contain
       line N" by
           min(table) <= N <= max(table)
       is WRONG -- N can sit in a hole and the range test still says HIT.
       Fix: build ONE class-wide line -> owner-method map and match EXACTLY.

  (R2) LINE BELONGS TO A SYNTHETIC METHOD -- the "holes" of an outer method are
       filled by the lambdas that belong to it. Measured on MDMAfcService.findEmp:
           line 366 -> findEmp
           line 367 -> lambda$findEmp$10     <-- looks like a hole in findEmp
           line 368 -> lambda$findEmp$10
           line 370 -> findEmp
       Fix: the class-wide map spans outer AND synthetic methods, so the line
       resolves to the real owner, and the outer method is recovered via the
       lambda$<outer>$<n> naming convention.

  (R3) CROSS-VERSION DRIFT -- a patch that inserts code shifts every later line.
       Measured between two real patch jars of MDMAfcService, over 54 common
       members, the per-method delta was NOT uniform -- 8 distinct values,
       including a NEGATIVE one (a member that moved earlier):
           -13 x1, +17 x3, +20 x25, +23 x5, +35 x1, +36 x2, +39 x1, +40 x16
       A single global offset therefore CANNOT map a logged line across
       versions. Fix: resolve the line to a METHOD first (R1/R2 do this), then
       compare methods across jars, and only use the line as a tie-breaker.

Usage
-----
  # resolve a logged line to its owning method, in every candidate class body
  python -X utf8 line_lookup.py <FQCN> --method findEmp --line 366 [--lib DIR]

  # if the line resolves to a lambda, the outer method is reported automatically
  python -X utf8 line_lookup.py <FQCN> --line 367 --lib DIR

  # cross-version comparison: which jar's offsets match the logged line?
  python -X utf8 line_lookup.py <FQCN> --method findEmp --line 366 --cross

Output is pure ASCII on stdout; the full report is UTF-8 JSON in the work dir.
"""

import argparse
import json
import os
import re
import sys
import time
import zipfile

# Shared robust javap parser. Do NOT go back to a regex block-splitter here:
# a declaration-matching regex silently drops constructors (no space before the
# name) and `static {}` initializers (no parentheses at all). Measured on one real
# media: 62 violations across a 60-class sample, 0 after switching to this parser.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as envmod      # noqa: E402
import javap_parse as jp  # noqa: E402

JAVAP = envmod.javap_path()      # may be None; the default backend needs no JDK
#: default: an override dir if the caller set one, else the current directory.
DEFAULT_LIB = os.environ.get('EOS_LIB') or os.environ.get('EOS_OVERRIDE_DIRS') or '.'

SYNTH_PREFIX = 'lambda$'


def leading_zeros(name):
    n = 0
    for ch in name:
        if ch == '0':
            n += 1
        else:
            break
    return n


def parse_methods(jar, fqcn, javap=JAVAP, with_bytecode=False, backend='py'):
    """{signature: [(source_line, bytecode_offset), ...]} via the robust parser.

    backend='py' (default) reads the class file in-process -- ~178x faster than
    javap on this media and validated to produce identical rows. Use
    backend='javap' to cross-check a specific class.
    """
    return jp.parse_members(jar, fqcn, javap, with_bytecode, backend)


def owner_of(line, line_map):
    """[(owner_name, bytecode_offset)] for every method claiming this source line."""
    return line_map.get(line, [])


def short_name(sig):
    """Delegate to the shared parser so `<clinit>` and constructors are named right."""
    return jp.member_name(sig)


def name_matches(candidate, target, fqcn):
    """Does a member name satisfy the --method target?

    A stack trace prints a constructor as `<init>`. The pure-Python backend also
    reports `<init>`, while the javap backend prints the class simple name (for a
    nested class that is the ENTIRE suffix after the last '.', e.g.
    `ValueCheckRule$CasCadeElement`). Accept all three spellings.
    """
    if candidate == target:
        return True
    simple = fqcn.rsplit('.', 1)[-1]              # keeps '$' for nested classes
    leaf = simple.split('$')[-1]
    if target in ('<init>', simple, leaf) and candidate in (simple, '<init>'):
        return True
    if target in ('<init>', simple, leaf) and candidate == leaf:
        return True
    if target == '<clinit>' and candidate == '<clinit>':
        return True
    return False


def outer_of(synth_name):
    """lambda$findEmp$10 -> findEmp ; lambda$null$8 -> None (no outer info)."""
    m = re.match(r'^lambda\$([A-Za-z_$][\w$]*)\$\d+$', synth_name)
    return m.group(1) if m else None


def build_line_map(methods):
    """Line -> owning member map; spans ordinary AND synthetic members."""
    return jp.build_line_map(methods)


def list_lib_jars(libdir):
    out = []
    for root, _d, files in os.walk(libdir):
        for fn in files:
            if fn.lower().endswith('.jar'):
                out.append(os.path.join(root, fn))
    out.sort(key=lambda p: os.path.basename(p))
    return out


def candidates_for(libdir, entry):
    cands = []
    for rank, path in enumerate(list_lib_jars(libdir)):
        base = os.path.basename(path)
        try:
            with zipfile.ZipFile(path) as z:
                if entry in z.namelist():
                    info = z.getinfo(entry)
                    cands.append({'jar': base, 'path': path, 'rank': rank,
                                  'leadingZeros': leading_zeros(base),
                                  'crc': '%08x' % info.CRC, 'size': info.file_size})
        except Exception:
            continue
    cands.sort(key=lambda c: c['rank'])
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('fqcn')
    ap.add_argument('--lib', default=os.environ.get('EOS_LIB', DEFAULT_LIB))
    ap.add_argument('--method', default=None, help='expected outer method (optional hint)')
    ap.add_argument('--line', type=int, required=True, help='the line number from the log')
    ap.add_argument('--cross', action='store_true',
                    help='compare the owning method across every distinct class body')
    ap.add_argument('--javap', default=JAVAP)
    ap.add_argument('--backend', choices=('py', 'javap'), default='py',
                    help='py = read class files in-process (fast, default); '
                         'javap = shell out (cross-check)')
    ap.add_argument('--work', default=envmod.work_dir())
    args = ap.parse_args()

    fqcn = args.fqcn.replace('/', '.')
    if fqcn.endswith('.class'):
        fqcn = fqcn[:-len('.class')]
    entry = fqcn.replace('.', '/') + '.class'
    logged = args.line

    t0 = time.time()
    cands = candidates_for(args.lib, entry)
    scan_sec = time.time() - t0

    print('class      : %s' % fqcn)
    print('logged line: %d%s' % (logged, ('   (expected method: %s)' % args.method)
                                 if args.method else ''))
    print('lib scan   : %.2f s   candidate jars = %d' % (scan_sec, len(cands)))
    if not cands:
        print('')
        print('NOT IN lib/ -> not patched; use fatjar_lookup.py, then rerun this script')
        print('   with --lib <dir where you extracted the nested jar>')
        return 2

    # dedupe class bodies by CRC: identical bytecode => identical line tables
    groups = {}
    for c in cands:
        groups.setdefault(c['crc'], []).append(c)
    print('distinct class bodies (CRC) = %d of %d jars' % (len(groups), len(cands)))

    report = {'fqcn': fqcn, 'loggedLine': logged, 'candidates': cands, 'bodies': []}
    any_exact = False

    for crc, members in groups.items():
        jar = members[0]['path']
        print('')
        print('--- body crc %s   %s' % (crc, ' | '.join(m['jar'] for m in members)))
        t1 = time.time()
        methods, err = parse_methods(jar, fqcn, args.javap, backend=args.backend)
        javap_sec = time.time() - t1
        if err:
            print('    %s' % err)
            report['bodies'].append({'crc': crc, 'jars': [m['jar'] for m in members],
                                     'error': err})
            continue

        line_map = build_line_map(methods)
        owners = owner_of(logged, line_map)

        # nearest lines on either side -- tells the agent whether the line is a
        # comment/blank (real hole) or simply absent from this version
        below = max((l for l in line_map if l < logged), default=None)
        above = min((l for l in line_map if l > logged), default=None)

        rec = {'crc': crc, 'jars': [m['jar'] for m in members],
               'javapSec': round(javap_sec, 2), 'owners': [], 'below': below,
               'above': above, 'methodBodies': {k: v for k, v in methods.items()}}
        print('    javap %.2fs   methods with line table = %d' % (javap_sec, len(methods)))

        if owners:
            # --method given? keep only owners consistent with the expected method
            if args.method:
                kept = []
                for nm, off, sig in owners:
                    outer = outer_of(nm)
                    if name_matches(nm, args.method, fqcn) or \
                            (outer and name_matches(outer, args.method, fqcn)):
                        kept.append((nm, off, sig))
                if kept:
                    owners = kept
                else:
                    print('    line %d exists here but belongs to %s -- NOT %s()'
                          % (logged, ', '.join(nm + '()' for nm, _, _ in owners),
                             args.method))
                    print('    -> treat as a NON-MATCH for the expected method'
                          ' (cross-version drift)')
                    rec['mismatchForExpected'] = [nm for nm, _, _ in owners]
                    owners = []
        if owners:
            any_exact = True
            for nm, off, sig in owners:
                outer = outer_of(nm)
                rec['owners'].append({'method': nm, 'offset': off, 'signature': sig,
                                      'outerMethod': outer})
                tag = ''
                if nm.startswith(SYNTH_PREFIX):
                    tag = '   [SYNTHETIC] belongs to %s()' % (outer or '?')
                    if args.method and outer and name_matches(outer, args.method, fqcn):
                        tag += '   <-- matches expected method'
                elif args.method and name_matches(nm, args.method, fqcn):
                    tag += '   <-- matches expected method'
                print('    EXACT  line %d -> %s()  (bytecode offset %d)%s'
                      % (logged, nm, off, tag))
                rec_span = methods.get(sig)
                if rec_span:
                    print('           %s spans lines %d-%d'
                          % (nm, min(r[0] for r in rec_span), max(r[0] for r in rec_span)))
        else:
            print('    NO EXACT ROW for line %d in this class body.' % logged)
            print('      nearest recorded lines: below=%s above=%s' % (below, above))
            if below is not None and above is not None:
                gap = above - below
                owners_below = line_map.get(below, [])
                owners_above = line_map.get(above, [])
                same_method = bool(owners_below and owners_above
                                   and owners_below[0][0] == owners_above[0][0])
                print('      gap between them = %d line(s); neighbours owned by %s'
                      % (gap,
                         ('the SAME method (' + owners_below[0][0] + ')')
                         if same_method else 'DIFFERENT methods'))
                if same_method:
                    # a genuine comment/blank/declaration hole INSIDE one method
                    nm = owners_below[0][0]
                    print('      -> logged line sits inside %s(); it is a COMMENT / BLANK /'
                          % nm)
                    print('         DECLARATION line that produced no bytecode.')
                    print('         Read the source around %s() lines %d-%d and the'
                          % (nm, below, above))
                    print('         statement is between them.')
                    rec['holeKind'] = 'comment-hole-in-method'
                    rec['holeOwner'] = nm
                else:
                    print('      -> neighbours belong to DIFFERENT methods, so this is NOT')
                    print('         a comment hole: the logged line comes from a DIFFERENT')
                    print('         VERSION of this class (a patch added/removed code).')
                    print('         below: %s()   above: %s()'
                          % (owners_below[0][0] if owners_below else '?',
                             owners_above[0][0] if owners_above else '?'))
                    rec['holeKind'] = 'cross-version-drift'
                    print('         -> use --cross to convert the line to each version')
            elif below is None and above is not None:
                print('      -> line is BEFORE the first recorded line: logged line'
                      ' probably comes from a NEWER/OLDER body')
            elif above is None and below is not None:
                print('      -> line is AFTER the last recorded line: same conclusion')

        # cross-version comparison of the owning method
        if args.cross:
            target = args.method
            if not target and owners:
                target = owners[0][0]
                if target.startswith(SYNTH_PREFIX):
                    target = outer_of(target) or target
            if target:
                spans = []
                for sig, rows in methods.items():
                    if name_matches(short_name(sig), target, fqcn):
                        spans.append((min(r[0] for r in rows), max(r[0] for r in rows)))
                if spans:
                    lo = min(s[0] for s in spans)
                    hi = max(s[1] for s in spans)
                    rec['crossTarget'] = target
                    rec['crossSpan'] = [lo, hi]
                    print('    cross: %s() occupies source lines %d-%d in THIS body'
                          % (target, lo, hi))
                    if lo <= logged <= hi:
                        print('           logged line %d is INSIDE this span' % logged)
                    else:
                        print('           logged line %d is OUTSIDE this span'
                              '  (delta %+d vs the span start)'
                              % (logged, logged - lo))

        report['bodies'].append(rec)

    print('')
    if any_exact:
        print('RESULT: the logged line resolves in at least one class body -- use the')
        print('        printed method/offset (see ⑤ in SKILL.md for the bytecode step).')
    else:
        print('RESULT: no class body has an exact row for line %d.' % logged)
        print('        Most likely the logged line comes from a DIFFERENT version of this')
        print('        class (a patch that added/removed code above it). Actions:')
        print('          1. re-run with --cross to compare the owning method span per body')
        print('          2. check the ~[jarName] in the stack frame against the candidate list')
        print('          3. only then fall back to the fatjar original with fatjar_lookup.py')

    # ---- cross-version CONVERSION using (method, bytecode offset) as the anchor ----
    # The bytecode offset within the owning method is far more stable than the
    # source line, because it does not move when unrelated code above the method
    # is inserted or removed by a patch.
    if args.cross:
        anchors = []
        for rec in report['bodies']:
            if rec.get('owners'):
                anchors.append(rec)
        print('')
        print('=== cross-version conversion (anchor = owning method + bytecode offset) ===')
        if not anchors:
            print('  no body resolved the logged line exactly, so there is no anchor to')
            print('  convert FROM. Identify the correct jar first (see ~[jarName] in the')
            print('  stack frame), or run with --method to restrict the search.')
        else:
            print('  NOTE: a single global offset can NEVER convert a logged line across')
            print('  versions. Measured on this very class between two real patch jars, the')
            print('  per-member delta had 8 distinct values (-13 to +40), including a')
            print('  negative one -- because each patch inserted a different amount of')
            print('  code. Per-member anchoring is therefore the only valid method.')
            for anchor in anchors:
                first = anchor['owners'][0]
                nm, off, sig = first['method'], first['offset'], first['signature']
                base = short_name(sig)
                print('')
                print('  anchor: %s line %d -> %s() bytecode offset %d'
                      % (anchor['crc'], logged, nm, off))
                for rec in report['bodies']:
                    if rec is anchor or 'methodBodies' not in rec:
                        continue
                    cand_rows = None
                    for s2, rows in rec['methodBodies'].items():
                        if name_matches(short_name(s2), base, fqcn):
                            cand_rows = rows
                            break
                    if cand_rows is None:
                        print('    -> %s : method %s() ABSENT in this body'
                              % (rec['crc'], base))
                        continue
                    # pick the line whose bytecode offset is closest to the anchor's
                    best = min(cand_rows, key=lambda r: abs(r[1] - off))
                    span = (min(r[0] for r in cand_rows), max(r[0] for r in cand_rows))
                    print('    -> %s : %s() spans %d-%d ; offset %d maps to LINE %d'
                          % (rec['crc'], base, span[0], span[1], off, best[0]))
                    print('         conversion: logged %d  ->  %d   (delta %+d)'
                          % (logged, best[0], best[0] - logged))
                    rec['convertedFrom'] = anchor['crc']
                    rec['convertedLine'] = best[0]

    os.makedirs(args.work, exist_ok=True)
    rp = os.path.join(args.work, 'line-lookup-%s-%d.json'
                      % (fqcn.split('.')[-1], logged))
    with open(rp, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print('')
    print('report -> %s' % rp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
