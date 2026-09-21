#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Product-agnostic class locator + override/arbitration for layered Java apps.

Works on ANY product: nothing here knows about Primeton, EOS, MDM, dqms or any
other vendor. The layout is discovered at runtime by media.py, and the class-name
namespace comes from what the product actually ships.

WHAT IT ANSWERS
  * which archive/directory provides a class
  * whether some other jar SHADOWS it (the "patch beats the original" case), and
    if so, in what precedence order
  * which of those candidate class bodies actually contains a logged source line
    (line-number fingerprint), so the effective version can be identified
  * where the class came from when several copies exist, with CRC dedupe

PRECEDENCE MODEL (generic)
  A layered app is loaded with an extra classpath directory in front of the
  application archive (Spring Boot: -Dloader.path=...; other loaders: a classpath
  entry, a plugin dir, a lib dir). Within that directory, the FIRST jar whose name
  sorts earlier in ASCII order wins for a given class -- that is the behaviour of
  jar/classpath ordering in the JVM, and vendor tools exploit it by prefixing file
  names with zeros. So precedence is derived from SORT ORDER, which is measurable,
  rather than from any vendor's naming convention.

  When the fingerprint is ambiguous, sort order decides; when sort order and the
  fingerprint disagree, the FP is reported so a human can look.

Usage
-----
  python -X utf8 find_class.py <FQCN> --media <product dir>
  python -X utf8 find_class.py <FQCN> --media <dir> --method m --line N
  python -X utf8 find_class.py <FQCN> --media <dir> --method m --line N --backend javap
  python -X utf8 find_class.py --media <dir> --layout          # just show the layout
"""

import argparse
import json
import os
import sys
import time
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import classfile as cf      # noqa: E402
import env as envmod        # noqa: E402
import javap_parse as jp    # noqa: E402
import media as mediamod    # noqa: E402
import oracle               # noqa: E402

ARCHIVE_EXT = ('.jar', '.war', '.ear', '.zip')


def leading_zeros(name):
    n = 0
    for ch in name:
        if ch == '0':
            n += 1
        else:
            break
    return n


def _all_archives(layout):
    """[(path, name)] for every readable archive in the media."""
    out = []
    for a in layout['archives']:
        if a.get('kind', '').startswith('unreadable'):
            continue
        out.append((a['path'], a['name']))
    return out


def find_candidates(layout, fqcn):
    """Every place that provides `fqcn`: archives (incl. nested) + override dirs.

    Returns dicts with: source (jar/dir label), path, kind, order, crc, size,
    isOverride (came from an override dir), nestedIn (for BOOT-INF/lib jars).
    """
    entry = fqcn.replace('.', '/') + '.class'
    media = layout['media']
    override_dirs = {os.path.abspath(e['dir']) for e in layout['overrideDirs']}
    main_path = layout['main']['path'] if layout['main'] else None

    cands = []

    def add(source, path, kind, order, crc, size, nested_in=None,
            in_override=None, is_main=None):
        ov = (os.path.abspath(os.path.dirname(path)) in override_dirs
              if in_override is None else in_override)
        mn = (path == main_path) if is_main is None else is_main
        cands.append({
            'source': source, 'path': path, 'kind': kind,
            # `order` is the full load-order tuple (rank, seq); `orderSeq` is a
            # flat, displayable/sortable integer that preserves the same ranking.
            'order': order,
            'orderSeq': order[0] * 100000 + order[1],
            'crc': '%08x' % crc, 'size': size, 'nestedIn': nested_in,
            'leadingZeros': leading_zeros(os.path.basename(path)),
            'isOverride': ov,
            'isMainArchive': mn,
        })

    order = 0
    for path, name in _all_archives(layout):
        order += 1
        # Load-order model, product-agnostic and derived from MEASURABLE facts:
        #   * anything in an override/patch directory is loaded BEFORE the main
        #     application archive (that is the whole point of -Dloader.path, a
        #     plugin dir, or an exploded patch dir);
        #   * within one directory, plain file-name sort order decides, because
        #     that is what the JVM does with a classpath directory of jars.
        # Without the override bias the main archive would win every tie and the
        # effective (patched) version would be misidentified -- reproduced on the
        # executor media, where both bodies contained the logged line.
        in_override = os.path.abspath(os.path.dirname(path)) in override_dirs
        is_main = bool(layout['main'] and path == layout['main']['path'])
        if in_override:
            rank = 0
        elif is_main:
            rank = 2
        else:
            rank = 1
        eff_order = (rank, order)
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                # application classes stored as plain entries
                for pref, label in (('BOOT-INF/classes/', name + '!/classes'),
                                    ('WEB-INF/classes/', name + '!/classes'),
                                    ('', name)):
                    if pref and (pref + entry) in names:
                        info = z.getinfo(pref + entry)
                        add(label, path, 'app-classes', eff_order, info.CRC,
                            info.file_size, in_override=in_override, is_main=is_main)
                        break
                else:
                    if entry in names:
                        info = z.getinfo(entry)
                        add(name, path, 'plain', eff_order, info.CRC, info.file_size,
                            in_override=in_override, is_main=is_main)
                # classes inside nested jars
                for n in names:
                    if not n.endswith('.jar'):
                        continue
                    if not n.startswith(('BOOT-INF/lib/', 'WEB-INF/lib/')):
                        continue
                    try:
                        with zipfile.ZipFile(
                                __import__('io').BytesIO(z.read(n))) as nz:
                            if entry in nz.namelist():
                                info = nz.getinfo(entry)
                                add(os.path.basename(n), path, 'nested',
                                    eff_order, info.CRC, info.file_size,
                                    nested_in=n,
                                    in_override=in_override, is_main=is_main)
                    except Exception:
                        continue
        except Exception:
            continue

    # a class may also exist as a loose file in an override dir (exploded patch);
    # an exploded patch dir outranks every jar
    rel = fqcn.replace('.', os.sep) + '.class'
    for e in layout['overrideDirs']:
        p = os.path.join(e['dir'], rel)
        if os.path.exists(p):
            with open(p, 'rb') as fh:
                data = fh.read()
            add('loose:' + os.path.relpath(p, media), p, 'loose', (0, 0),
                zipfile.crc32(data) & 0xFFFFFFFF, len(data),
                in_override=True, is_main=False)

    cands.sort(key=lambda c: (c['orderSeq'], c['leadingZeros'],
                              os.path.basename(c['path'])))
    return cands


def read_body(cand, fqcn):
    """Class bytes for a candidate, wherever it lives."""
    entry = fqcn.replace('.', '/') + '.class'
    if cand['kind'] == 'loose':
        with open(cand['path'], 'rb') as fh:
            return fh.read()
    with zipfile.ZipFile(cand['path']) as z:
        if cand['kind'] == 'app-classes':
            pref = ('BOOT-INF/classes/' if 'BOOT-INF/classes/' + entry in z.namelist()
                    else 'WEB-INF/classes/')
            return z.read(pref + entry)
        if cand['kind'] == 'nested':
            return zipfile.ZipFile(
                __import__('io').BytesIO(z.read(cand['nestedIn']))).read(entry)
        return z.read(entry)


def main():
    ap = argparse.ArgumentParser(
        description='Product-agnostic class locator and override arbitration')
    ap.add_argument('fqcn', nargs='?', help='fully qualified class name')
    ap.add_argument('--media', required=True,
                    help='product install directory (containing the fatjar/war)')
    ap.add_argument('--method', default=None, help='expected method name')
    ap.add_argument('--line', type=int, default=None, help='line number from the log')
    ap.add_argument('--layout', action='store_true', help='only print the media layout')
    ap.add_argument('--backend', choices=('py', 'javap'), default='py')
    ap.add_argument('--true', dest='truth', default=None,
                    help='jar actually loaded by the JVM (the ~[jar] from the log). '
                         'When given, the arbitration verdict is self-checked against '
                         'it and the case is recorded as confirmed (drives future '
                         'replay). Absent, the case is recorded unconfirmed.')
    ap.add_argument('--json', default=None, help='write the full report to this file')
    args = ap.parse_args()

    t0 = time.time()
    layout = mediamod.discover(args.media)
    print(mediamod.format_layout(layout))
    print('')

    if args.layout or not args.fqcn:
        return 0

    fqcn = args.fqcn.replace('/', '.')
    if fqcn.endswith('.class'):
        fqcn = fqcn[:-len('.class')]

    # usage-time learning: replay a previously CONFIRMED verdict if we've seen
    # this (media, class, method, line) before. This is a short-circuit hint,
    # never authoritative -- the full analysis below still runs.
    fp = oracle.media_fp(layout)
    if oracle.enabled() and args.method and args.line is not None:
        hit = oracle.lookup(fp, fqcn, args.method, args.line)
        if hit:
            print('oracle     : [history] confirmed on %s -> %s (crc %s)'
                  % (hit.get('ts'), ' | '.join(hit.get('sources', [])),
                     hit.get('verdict', {}).get('crc', '?')))
            print('             re-derived below for safety; cross-check both.')
            print('')

    cands = find_candidates(layout, fqcn)
    print('class            : %s' % fqcn)
    print('candidate copies : %d   (media scan %.1fs)'
          % (len(cands), time.time() - t0))
    if not cands:
        print('')
        print('NOT FOUND in this media. Check the class name, or run with --layout to')
        print('see where code lives. A class may also be loaded from outside the')
        print('install dir (shared lib, container image, remote classloader).')
        return 2

    print('')
    print('  %-6s %-6s %-9s %-9s %-9s %s'
          % ('order', 'zeros', 'crc', 'bytes', 'override', 'source'))
    groups = {}
    for c in cands:
        groups.setdefault(c['crc'], []).append(c)
    for c in cands:
        print('  %-6d %-6d %-9s %-9d %-9s %s'
              % (c['orderSeq'], c['leadingZeros'], c['crc'], c['size'],
                 'yes' if c['isOverride'] else '-', c['source']))

    print('')
    print('distinct class bodies (CRC) = %d of %d copies' % (len(groups), len(cands)))

    # ---- finger the effective body by the logged line, when given ----
    verdict = {'status': 'LOCATED'}
    if args.method and args.line is not None:
        print('')
        print('LineNumberTable check for %s (backend=%s), one parse per distinct body:'
              % (args.method, args.backend))
        results = []
        for crc, members in groups.items():
            path = members[0]['path']
            t1 = time.time()
            try:
                ms, err = jp.parse_members(path, fqcn, backend=args.backend,
                                           entry=fqcn.replace('.', '/') + '.class') \
                    if False else (None, None)
            except Exception:
                ms, err = None, None
            # parse from the bytes we already have: backend-agnostic and exact
            try:
                data = read_body(members[0], fqcn)
                mset = cf.members_from_class_bytes(data)
                # normalise constructor naming to the javap convention
                simple = fqcn.rsplit('.', 1)[-1]
                norm = {}
                for sig, rows in mset.items():
                    if sig.startswith('<init>'):
                        sig = simple + sig[len('<init>'):]
                    norm[sig] = rows
                mset = norm
                err = None if mset else 'NO-LINE-TABLE'
            except Exception as exc:  # noqa: BLE001
                mset, err = {}, '%s: %s' % (type(exc).__name__, exc)
            dt = time.time() - t1
            hit = False
            owners = []
            span = None
            if mset:
                for sig, rows in mset.items():
                    nums = [l for l, _ in rows]
                    if not nums:
                        continue
                    if args.line in nums:
                        hit = True
                        owners.append((jp.member_name(sig),
                                       [o for l, o in rows if l == args.line][0]))
                allnums = [l for rows in mset.values() for l, _ in rows]
                span = (min(allnums), max(allnums)) if allnums else None
            rec = {'crc': crc, 'sources': [m['source'] for m in members],
                   'override': any(m['isOverride'] for m in members),
                   'minOrder': min(m['orderSeq'] for m in members),
                   'span': span, 'exactHit': hit, 'owners': owners,
                   'error': err, 'sec': round(dt, 3)}
            results.append(rec)
            note = ''
            if owners:
                note = '  <<< EXACT line %d -> %s' % (
                    args.line, ', '.join('%s@%d' % o for o in owners))
            elif span:
                inside = span[0] <= args.line <= span[1]
                note = '  (%s for line %d%s)' % (
                    'line falls in a hole' if inside else 'line outside span',
                    args.line, '' if err is None else ', err=' + str(err))
            print('  crc %-9s span %-14s %-6s %s%s'
                  % (crc, str(span), '%.3fs' % dt,
                     ' | '.join(m['source'] for m in members), note))

        hitters = [r for r in results if r['exactHit']]
        print('')
        if len(hitters) == 1:
            h = hitters[0]
            verdict = {'status': 'ARBITRATED', 'crc': h['crc'],
                       'source': h['sources'][0], 'sources': h['sources'],
                       'span': h['span'], 'owners': h['owners'],
                       'precedenceOrder': h['minOrder']}
            print('ARBITRATED: only one class body has an exact row for line %d' % args.line)
            print('  source = %s' % ' | '.join(h['sources']))
            print('  crc=%s span=%s owner=%s' % (h['crc'], h['span'], h['owners']))
        elif len(hitters) > 1:
            hitters.sort(key=lambda r: r['minOrder'])
            h = hitters[0]
            verdict = {'status': 'AMBIGUOUS', 'chosen': h['sources'][0],
                       'chosenBy': 'load order', 'candidates': [r['sources'] for r in hitters]}
            print('AMBIGUOUS: %d bodies contain line %d; load order decides (earliest wins):'
                  % (len(hitters), args.line))
            for r in hitters:
                print('   order=%d %s' % (r['minOrder'], ' | '.join(r['sources'])))
            print('  chosen by load order = %s' % ' | '.join(h['sources']))
            print('  cross-check against the jar name printed by the JVM (~[name.jar]) if any.')
        else:
            verdict = {'status': 'NO-EXACT-MATCH'}
            print('No body has an exact row for line %d.' % args.line)
            print('  see 3.5 in SKILL.md: hole-in-line-table / synthetic method /')
            print('  cross-version drift. Next: line_lookup.py --cross, or confirm the')
            print('  running version from the log itself.')

        # usage-time learning: append this arbitration as one case. When --true
        # agrees, it is recorded confirmed and drives future replay (oracle.py).
        if args.method and args.line is not None:
            src = list(h['sources']) if verdict.get('sources') else (
                [verdict['source']] if verdict.get('source') else [])
            case = oracle.record(fp, fqcn, args.method, args.line, verdict, src,
                                 truth=args.truth)
            if oracle.enabled():
                tag = ('CONFIRMED by --true' if case['confirmed'] else
                       'recorded (unconfirmed)')
                print('oracle     : %s -> %s%s'
                      % (tag, oracle.journal_path(),
                         '  truth=%s' % args.truth if args.truth else ''))
                if args.truth and not case['confirmed']:
                    print('             truth mismatch: arbitration says %s, '
                          'log loaded %s' % (' | '.join(src) or '?', args.truth))
                print('')

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'layout': layout, 'fqcn': fqcn, 'candidates': cands,
                       'verdict': verdict}, fh, ensure_ascii=False, indent=1)
        print('')
        print('report -> %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
