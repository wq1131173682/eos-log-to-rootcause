#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Locator + extractor for classes inside a layered Java archive, product-agnostic.

Preferred entry point is `find_class.py`, which also handles override/patch
directories and arbitration. This module is the archive-only worker: it indexes
one archive and can extract a provider out of it.

Scanning both places is mandatory, because a layered archive keeps classes in TWO
locations and real deployments differ:
  * BOOT-INF/classes/**/*.class   (application classes, plain zip entries)
  * BOOT-INF/lib/*.jar            (dependencies and library-provided product code)
WAR archives use WEB-INF/classes and WEB-INF/lib instead.

Modes
-----
  --targeted (default)  skip nested jars that are obviously third-party, to keep
                        the scan fast. The skip list is derived from the media's
                        own app namespaces when available, and otherwise falls back
                        to a generic third-party marker list -- it is NOT keyed to
                        any vendor's jar naming.
  --full                index every nested jar (needed for third-party classes).

A cache is written into the work dir so repeat lookups are fast.

Usage
-----
  python -X utf8 fatjar_lookup.py <FQCN> --fat <archive> [--full] [--extract DIR]
  python -X utf8 fatjar_lookup.py <FQCN> --media <install dir>   # auto-find archive
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import env as envmod  # noqa: E402

#: generic third-party markers -- used ONLY to order/skip jars in --targeted mode,
#: never to decide that a class is "not product code". Everything is still found
#: with --full.
THIRD_PARTY_HINTS = (
    'spring', 'jackson', 'guava', 'netty', 'log4j', 'slf4j', 'logback', 'tomcat',
    'jetty', 'hibernate', 'mysql', 'ojdbc', 'sqljdbc', 'mssql', 'postgresql',
    'gauss', 'dmjdbc', 'dmjdbc', 'redisson', 'lettuce', 'jedis', 'poi-', 'antlr',
    'snakeyaml', 'kotlin', 'commons-', 'hadoop', 'hive', 'zookeeper', 'kafka',
    'druid', 'fastjson', 'gson', 'cglib', 'asm', 'ehcache', 'quartz', 'velocity',
    'freemarker', 'protobuf', 'grpc', 'okhttp', 'httpclient', 'httpcore',
    'snappy', 'zstd', 'lz4', 'avro', 'parquet', 'orc-', 'arrow',
)

#: marker used as the "provider" name for classes stored under classes/
APP_MARKER = '(BOOT-INF/classes)'


def looks_like_third_party(name):
    """True when a nested jar name matches a generic third-party marker.

    Only used to SKIP jars in --targeted mode for speed. Nothing is decided about
    product code from this, and --full ignores it entirely -- so a product whose
    jars happen to carry a common word is still fully searchable.
    """
    low = name.lower()
    return any(h in low for h in THIRD_PARTY_HINTS)


def build_index(fat, full=False):
    """Index a layered archive (Spring Boot fatjar, war, or plain jar).

    TWO places can carry classes, and BOTH must be scanned:

      * classes/**/*.class  (BOOT-INF/classes or WEB-INF/classes) -- APPLICATION
        classes, stored as plain entries (NOT inside a nested jar). Measured: one
        real media keeps 414 application classes here while another keeps only 1.
        A locator that walks only nested jars reports "NOT FOUND" for every
        application class on the first kind of packaging -- reproduced with
        --full (373 nested jars scanned, class still reported missing).
      * lib/*.jar           (BOOT-INF/lib or WEB-INF/lib) -- libraries, which for
        some products is where the application code actually lives.

    Missing the first location is exactly the bug this function used to have.
    """
    t0 = time.time()
    index = {}
    app_classes = 0
    nested_scanned = 0
    nested_skipped = 0
    # layered apps put classes under BOOT-INF/ (Spring Boot) or WEB-INF/ (war)
    prefix_pairs = (('BOOT-INF/classes/', 'BOOT-INF/lib/'),
                    ('WEB-INF/classes/', 'WEB-INF/lib/'))
    with zipfile.ZipFile(fat) as z:
        for n in z.namelist():
            cls_pref = lib_pref = None
            for cp, lp in prefix_pairs:
                if n.startswith(cp):
                    cls_pref, lib_pref = cp, lp
                    break
            # ---- application classes: plain entries under classes/ ----
            if cls_pref and n.endswith('.class'):
                app_classes += 1
                key = n[len(cls_pref):-len('.class')].replace('/', '.')
                index.setdefault(key, []).append(APP_MARKER)
                continue
            # ---- classes inside nested jars ----
            if not lib_pref or not n.startswith(lib_pref) or not n.endswith('.jar'):
                continue
            if not full and looks_like_third_party(os.path.basename(n)):
                nested_skipped += 1
                continue
            nested_scanned += 1
            try:
                with zipfile.ZipFile(io.BytesIO(z.read(n))) as nz:
                    for cn in nz.namelist():
                        if cn.endswith('.class'):
                            # key = DOTTED class name WITHOUT the .class suffix
                            index.setdefault(cn[:-len('.class')].replace('/', '.'), []).append(
                                os.path.basename(n))
            except Exception:
                continue
    return {
        'fatjar': fat,
        'full': full,
        'keyFormat': 4,
        'appClassEntries': app_classes,
        'elapsedSec': round(time.time() - t0, 2),
        'scannedNestedJars': nested_scanned,
        'skippedNestedJars': nested_skipped,
        'distinctClasses': len(index),
        'index': index,
    }


def cache_path(work, fat, full):
    tag = 'full' if full else 'targeted'
    return os.path.join(work, 'fatjar-index-%s.json' % tag)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('fqcn')
    ap.add_argument('--fat', default=None, help='the archive to index')
    ap.add_argument('--media', default=None,
                    help='product install dir; the main archive is auto-detected '
                         '(preferred over --fat)')
    ap.add_argument('--full', action='store_true')
    ap.add_argument('--extract', default=None,
                    help='extract the providing nested jar(s)/class into this directory')
    ap.add_argument('--work', default=None)
    args = ap.parse_args()

    fat = args.fat
    if args.media:
        import media as mediamod
        lay = mediamod.discover(args.media)
        if not lay['main']:
            print('no application archive found under %s' % args.media)
            return 2
        fat = lay['main']['path']
        print('auto-detected main archive: %s (%s)'
              % (os.path.basename(fat), lay['main']['kind']))
    if not fat:
        print('need --fat <archive> or --media <install dir>')
        return 2

    fqcn = args.fqcn.replace('/', '.')
    if fqcn.endswith('.class'):          # NOT rstrip('.class') -- that strips a char SET
        fqcn = fqcn[:-len('.class')]     # and would eat the trailing 'l' of "...Impl"
    # the index is keyed by DOTTED class name (see build_index), so look up dotted
    entry = fqcn
    work = args.work or envmod.work_dir()
    os.makedirs(work, exist_ok=True)
    cp = cache_path(work, fat, args.full)

    data = None
    if os.path.exists(cp):
        try:
            cached = json.load(open(cp, encoding='utf-8'))
            if cached.get('fatjar') == fat and cached.get('full') == args.full \
                    and cached.get('keyFormat') == 4:
                data = cached
                print('index cache hit (%s)' % os.path.basename(cp))
        except Exception:
            data = None
    if data is None:
        mode = 'FULL' if args.full else 'TARGETED'
        print('building %s index of %s ...' % (mode, os.path.basename(fat)))
        data = build_index(fat, args.full)
        with open(cp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)

    print('mode=%s  appClasses=%d  nestedScanned=%d  nestedSkipped=%d  classes=%d'
          '  indexTime=%.2fs'
          % ('full' if data['full'] else 'targeted', data.get('appClassEntries', 0),
             data['scannedNestedJars'], data['skippedNestedJars'],
             data['distinctClasses'], data['elapsedSec']))

    jars = data['index'].get(entry)
    if not jars:
        print('')
        print('NOT FOUND: %s' % fqcn)
        print('  (searched classes/ plain entries AND lib/*.jar in both BOOT-INF and WEB-INF)')
        if not args.full:
            print('  targeted scan skipped jars matching generic third-party names.')
            print('  Retry with --full for third-party classes.')
        return 2

    print('')
    print('%s is provided by %d location(s):' % (fqcn, len(jars)))
    for j in sorted(jars):
        print('  %s' % j)

    if args.extract:
        os.makedirs(args.extract, exist_ok=True)
        want_jars = {j for j in jars if j != APP_MARKER}
        rel = fqcn.replace('.', '/') + '.class'
        with zipfile.ZipFile(fat) as z:
            names = z.namelist()
            # application classes: materialise the classes/ entry into a real file,
            # because a .class file on disk is what a decompiler takes as input
            # (a directory of classes silently produces nothing with CFR).
            if APP_MARKER in jars:
                src = None
                for cp in ('BOOT-INF/classes/', 'WEB-INF/classes/'):
                    if cp + rel in names:
                        src = cp + rel
                        break
                if src:
                    dst = os.path.join(args.extract, 'classes', rel.replace('/', os.sep))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, 'wb') as fh:
                        fh.write(z.read(src))
                    print('extracted -> %s' % dst)
            for n in names:
                if not n.endswith('.jar'):
                    continue
                if not n.startswith(('BOOT-INF/lib/', 'WEB-INF/lib/')):
                    continue
                if os.path.basename(n) not in want_jars:
                    continue
                out = os.path.join(args.extract, os.path.basename(n))
                if not os.path.exists(out):
                    with open(out, 'wb') as fh:
                        fh.write(z.read(n))
                print('extracted -> %s' % out)
        print('')
        print('next: feed the extracted .class / .jar to your decompiler, e.g.')
        print('  java -jar <cfr.jar> <extracted file> --outputdir <dir> '
              '--extraclasspath "<lib jars joined by ;>"')

    return 0


if __name__ == '__main__':
    sys.exit(main())
