#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Media discovery -- understand ANY product's install layout, no product names.

The skill must work on products it has never seen. So instead of hardcoding
"the patch dir is lib/ and product classes start with com.primeton", this module
DISCovers the layout:

  * top-level fatjars / wars / ears / jars
  * directories that look like an override/patch area (jars that ALSO exist in
    the fatjar, or jars named patch/hotfix/override/sp/plugin)
  * config/, logs/, bin/ start scripts (which may reveal the real classpath order)
  * the application's own top-level package namespace, derived from the fatjar's
    BOOT-INF/classes and from class names shared between the override dir and the
    fatjar -- NOT assumed.

Everything is read-only: this only lists and opens archives.
"""

import io
import os
import re
import zipfile

#: directory names that commonly hold override/patch jars
_OVERRIDE_HINTS = ('lib', 'libs', 'patch', 'patches', 'hotfix', 'override',
                   'overrides', 'sp', 'servicepack', 'plugins', 'plugin',
                   'extend', 'ext', 'modules', 'dropins')
#: names that make a .jar look like a patch
_PATCH_NAME_HINTS = ('patch', 'hotfix', 'fix', 'sp', 'override', 'mdm', 'efix')
#: never treat these as the application's own namespace
_COMMON_TOP = ('java', 'javax', 'jdk', 'sun', 'com', 'org', 'net', 'io', 'jakarta',
               'lombok', 'kotlin', 'scala', 'groovy', 'org.springframework')
#: third-party markers, used only to ORDER results (never to exclude product code)
_THIRD_PARTY = ('spring', 'jackson', 'guava', 'netty', 'log4j', 'slf4j', 'tomcat',
                'jetty', 'hibernate', 'mysql', 'ojdbc', 'sqljdbc', 'mssql', 'gauss',
                'redisson', 'poi-', 'antlr', 'snakeyaml', 'kotlin', 'commons',
                'hadoop', 'hive', 'zookeeper', 'kafka', 'druid', 'fastjson',
                'cglib', 'asm', 'ehcache', 'quartz', 'velocity', 'freemarker')

ARCHIVE_EXT = ('.jar', '.war', '.ear', '.zip')


def find_archives(media, recursive=False, max_depth=2):
    """[(path, size)] for archives in the media, shallow by default."""
    out = []
    media = os.path.abspath(media)
    base_depth = media.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(media):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth > max_depth:
            dirs[:] = []
            continue
        for f in files:
            if f.lower().endswith(ARCHIVE_EXT):
                p = os.path.join(root, f)
                try:
                    out.append((p, os.path.getsize(p)))
                except OSError:
                    pass
        if not recursive and depth >= 1:
            dirs[:] = []
    out.sort(key=lambda t: -t[1])
    return out


def is_zip(path):
    try:
        with zipfile.ZipFile(path) as z:
            z.namelist()
        return True
    except Exception:
        return False


def zip_layout(path):
    """Structural facts about an archive, used to classify it.

    Recognises the common Java packaging shapes without naming any product:
      boot-layered : BOOT-INF/classes + BOOT-INF/lib   (Spring Boot fatjar)
      boot-flat    : boot loader present, classes at root
      war          : WEB-INF/classes or WEB-INF/lib
      ear          : contains nested .jar/.war modules
      plain        : ordinary jar
    """
    info = {'path': path, 'name': os.path.basename(path),
            'kind': 'unknown', 'appClasses': 0, 'nestedJars': 0,
            'topPackages': {}, 'loaderClasses': 0}
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except Exception as exc:  # noqa: BLE001
        info['kind'] = 'unreadable:%s' % type(exc).__name__
        return info

    app = [n for n in names
           if n.startswith('BOOT-INF/classes/') and n.endswith('.class')]
    libs = [n for n in names if n.startswith('BOOT-INF/lib/') and n.endswith('.jar')]
    werb = [n for n in names if n.startswith('WEB-INF/classes/') and n.endswith('.class')]
    wlibs = [n for n in names if n.startswith('WEB-INF/lib/') and n.endswith('.jar')]
    loader = [n for n in names if n.startswith('org/springframework/boot/loader/')]
    inner = [n for n in names
             if n.lower().endswith(('.jar', '.war')) and not n.startswith('BOOT-INF/')]

    info['loaderClasses'] = len(loader)
    if app or libs:
        info['kind'] = 'boot-layered'
        info['appClasses'] = len(app)
        info['nestedJars'] = len(libs)
        roots = [n[len('BOOT-INF/classes/'):] for n in app]
    elif werb or wlibs:
        info['kind'] = 'war'
        info['appClasses'] = len(werb)
        info['nestedJars'] = len(wlibs)
        roots = [n[len('WEB-INF/classes/'):] for n in werb]
    elif inner:
        info['kind'] = 'ear'
        info['nestedJars'] = len(inner)
        roots = []
    else:
        info['kind'] = 'plain'
        plain = [n for n in names if n.endswith('.class')]
        info['appClasses'] = len(plain)
        roots = plain

    # derive the application's own top-level package namespace from what it ships
    pkgs = {}
    for r in roots:
        parts = r.split('/')
        if len(parts) >= 2:
            key = '.'.join(parts[:2]) if len(parts) > 2 else parts[0]
            pkgs[key] = pkgs.get(key, 0) + 1
    info['topPackages'] = dict(sorted(pkgs.items(), key=lambda kv: -kv[1])[:15])
    return info


def _classes_in(path, limit=None, include_nested=False):
    """Dotted class names inside one archive (layered-aware).

    include_nested=True also opens BOOT-INF/lib/*.jar and WEB-INF/lib/*.jar, which
    is REQUIRED to compare a patch dir against a Spring Boot fatjar: the fatjar
    keeps almost all application classes in nested jars, and its own
    BOOT-INF/classes/ entries may be as few as one.
    """
    out = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.endswith('.class'):
                if n.startswith('BOOT-INF/classes/'):
                    out.append(n[len('BOOT-INF/classes/'):-6].replace('/', '.'))
                elif n.startswith('WEB-INF/classes/'):
                    out.append(n[len('WEB-INF/classes/'):-6].replace('/', '.'))
                elif n.startswith(('BOOT-INF/', 'WEB-INF/', 'META-INF/')):
                    continue
                else:
                    out.append(n[:-6].replace('/', '.'))
                if limit and len(out) >= limit:
                    return out
            elif include_nested and n.endswith('.jar') and n.startswith(
                    ('BOOT-INF/lib/', 'WEB-INF/lib/')):
                try:
                    with zipfile.ZipFile(io.BytesIO(z.read(n))) as nz:
                        for cn in nz.namelist():
                            if cn.endswith('.class'):
                                out.append(cn[:-6].replace('/', '.'))
                                if limit and len(out) >= limit:
                                    return out
                except Exception:
                    continue
    return out


def discover(media, verbose=False):
    """Build a MediaLayout describing where code lives and how it is overridden.

    Automatic detection covers the usual layouts. Two environment variables exist
    for the cases it cannot infer (a patch dir with no naming hint and no class
    overlap, or a non-standard main archive):

      EOS_OVERRIDE_DIRS   ';' or ':' separated directories to treat as override areas
      EOS_MAIN_ARCHIVE    explicit path to the main application archive
    """
    media = os.path.abspath(media)
    if not os.path.isdir(media):
        raise SystemExit('media directory not found: %s' % media)

    layout = {
        'media': media,
        'archives': [],
        'main': None,             # the big application archive
        'overrideDirs': [],       # dirs whose jars can shadow the main archive
        'configDirs': [],
        'logDirs': [],
        'startScripts': [],
        'appNamespaces': [],      # candidate product package prefixes
        'overriddenClasses': 0,   # how many classes are shadowed at all
        'overrideJars': [],
    }

    for name in ('config', 'conf', 'etc'):
        p = os.path.join(media, name)
        if os.path.isdir(p):
            layout['configDirs'].append(p)
    for name in ('logs', 'log'):
        p = os.path.join(media, name)
        if os.path.isdir(p):
            layout['logDirs'].append(p)
    for sub in ('bin', 'scripts'):
        p = os.path.join(media, sub)
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.lower().endswith(('.sh', '.bat', '.cmd')):
                    layout['startScripts'].append(os.path.join(p, f))

    arch = find_archives(media, recursive=True, max_depth=2)
    infos = []
    for p, size in arch:
        if not is_zip(p):
            continue
        info = zip_layout(p)
        info['size'] = size
        infos.append(info)
    layout['archives'] = infos

    # main archive = the one carrying the most application code, else the biggest.
    # A layered fatjar is identified by its kind/loader/nested jars -- NOT by
    # appClasses alone, because a plain dependency jar (a JDBC driver, redisson)
    # can easily have more top-level classes than the fatjar's own
    # BOOT-INF/classes count (measured: redisson 1253 vs a fatjar with 1).
    def score(i):
        layered = 1 if i.get('kind') in ('boot-layered', 'war') else 0
        return (layered, i.get('nestedJars', 0), i.get('appClasses', 0),
                i.get('size', 0))
    if infos:
        layout['main'] = max(infos, key=score)
    # explicit override for a non-standard main archive
    forced_main = os.environ.get('EOS_MAIN_ARCHIVE')
    if forced_main:
        forced_main = os.path.abspath(forced_main)
        for i in infos:
            if os.path.abspath(i['path']) == forced_main:
                layout['main'] = i
                layout['mainForced'] = True
                break
        else:
            if os.path.exists(forced_main) and is_zip(forced_main):
                info = zip_layout(forced_main)
                info['size'] = os.path.getsize(forced_main)
                infos.append(info)
                layout['archives'] = infos
                layout['main'] = info
                layout['mainForced'] = True

    # ---- override dirs: directories of jars that can shadow the main archive ----
    main_classes = None
    if layout['main']:
        try:
            # include nested jars: a fatjar's application classes mostly live there,
            # so without this the shadow count is always 0 (measured defect)
            main_classes = set(_classes_in(layout['main']['path'], include_nested=True))
        except Exception:
            main_classes = None

    by_dir = {}
    for info in infos:
        if layout['main'] and info['path'] == layout['main']['path']:
            continue
        d = os.path.dirname(info['path'])
        by_dir.setdefault(d, []).append(info)

    for d, items in by_dir.items():
        base = os.path.basename(d).lower()
        looks_like_override = base in _OVERRIDE_HINTS or any(
            h in i['name'].lower() for i in items for h in _PATCH_NAME_HINTS)
        if not looks_like_override:
            continue
        entry = {'dir': d, 'jars': [i['name'] for i in items],
                 'shadowCount': 0, 'sample': []}
        if main_classes:
            for i in items:
                try:
                    cs = set(_classes_in(i['path']))
                except Exception:
                    continue
                shadow = cs & main_classes
                entry['shadowCount'] += len(shadow)
                if shadow and len(entry['sample']) < 5:
                    entry['sample'].extend(sorted(shadow)[:5 - len(entry['sample'])])
        layout['overrideDirs'].append(entry)

    layout['overrideDirs'].sort(key=lambda e: -e['shadowCount'])
    layout['overriddenClasses'] = sum(e['shadowCount'] for e in layout['overrideDirs'])

    # explicit override dirs: needed when a patch dir carries no naming hint AND
    # shares no class with the main archive (pure "new class" patches)
    forced = os.environ.get('EOS_OVERRIDE_DIRS')
    if forced:
        sep = ';' if ';' in forced else os.pathsep
        known = {os.path.abspath(e['dir']) for e in layout['overrideDirs']}
        for d in [x.strip() for x in forced.split(sep) if x.strip()]:
            d = os.path.abspath(d)
            if d in known or not os.path.isdir(d):
                continue
            jars = sorted(f for f in os.listdir(d) if f.lower().endswith(ARCHIVE_EXT))
            entry = {'dir': d, 'jars': jars, 'shadowCount': 0, 'sample': [],
                     'forced': True}
            if main_classes:
                for f in jars:
                    try:
                        cs = set(_classes_in(os.path.join(d, f)))
                    except Exception:
                        continue
                    shadow = cs & main_classes
                    entry['shadowCount'] += len(shadow)
                    if shadow and len(entry['sample']) < 5:
                        entry['sample'].extend(
                            sorted(shadow)[:5 - len(entry['sample'])])
            layout['overrideDirs'].append(entry)
            layout['overriddenClasses'] += entry['shadowCount']

    # ---- application namespace: from main archive's own classes ----
    ns = set()
    if layout['main']:
        for pkg in layout['main'].get('topPackages', {}):
            top = pkg.split('.')[0]
            if pkg not in _COMMON_TOP and top not in _COMMON_TOP:
                ns.add(pkg)
    # also learn from classes that are shadowed (those are definitely app code)
    for e in layout['overrideDirs']:
        for c in e['sample']:
            parts = c.split('.')
            if len(parts) >= 3 and parts[0] not in _COMMON_TOP:
                ns.add('.'.join(parts[:3]))
            elif len(parts) >= 2:
                ns.add('.'.join(parts[:2]))
    layout['appNamespaces'] = sorted(ns)[:20]

    return layout


def format_layout(layout):
    """ASCII-safe multi-line summary."""
    L = []
    L.append('media            : %s' % layout['media'])
    m = layout['main']
    if m:
        L.append('main archive     : %s  (%s, %.1f MB)'
                 % (m['name'], m['kind'], m.get('size', 0) / 1048576.0))
        L.append('  appClasses=%d  nestedJars=%d  loaderClasses=%d'
                 % (m['appClasses'], m['nestedJars'], m['loaderClasses']))
        if m.get('topPackages'):
            L.append('  topPackages: %s'
                     % ', '.join('%s(%d)' % (k, v)
                                 for k, v in list(m['topPackages'].items())[:8]))
    else:
        L.append('main archive     : <none found>')
    L.append('archives         : %d' % len(layout['archives']))
    L.append('config dirs      : %s' % (layout['configDirs'] or '-'))
    L.append('log dirs         : %s' % (layout['logDirs'] or '-'))
    L.append('start scripts    : %s' % [os.path.basename(s) for s in layout['startScripts']] or '-')
    L.append('app namespaces   : %s' % (layout['appNamespaces'] or '-'))
    L.append('')
    L.append('override / patch areas (jars that can shadow the main archive):')
    if not layout['overrideDirs']:
        L.append('  (none detected)')
    for e in layout['overrideDirs']:
        L.append('  %s' % e['dir'])
        L.append('     jars=%d  shadowed classes=%d' % (len(e['jars']), e['shadowCount']))
        for s in e['sample'][:3]:
            L.append('        e.g. %s' % s)
    return '\n'.join(L)


if __name__ == '__main__':
    import sys
    lay = discover(sys.argv[1] if len(sys.argv) > 1 else '.')
    print(format_layout(lay))
