#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Usage-time learning oracle for the eos-log-to-rootcause skill.

The skill "evolves while it is used": every real analysis run appends one case
to an append-only journal under the work dir, and later runs with the same
(class, method, line) look that journal up first so a previously *confirmed*
verdict is replayed instead of re-derived from scratch. No generic arbitration
rule is ever changed from here -- this only accumulates evidence and lets it
short-circuit itself.

Case lifecycle
--------------
  recorded     any arbitration run writes one row
  confirmed    promoted ONLY when independent ground truth agrees:
                 * caller passes --true <jar> (the jar actually loaded by the
                   JVM, i.e. the ~[jar] from the log) and it matches, or
                 * a human accepts the case later (not yet wired)
  Unconfirmed rows never drive replay -- they only accumulate as evidence.

Media fingerprint
-----------------
  Derived from stable, machine-independent features only (main-archive basename,
  packaging kind, override-dir jar basenames). Absolute paths never enter it, so
  the same product on another machine reuses its history while a different
  product is never polluted by another's conclusions.

Disable
-------
  EOS_ORACLE=0   switch off recording AND lookup (used by tests / CI).

Format
------
  <work>/cases.jsonl   one JSON object per line, append-only.
"""

import hashlib
import json
import os
import os.path
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

_ISOLATION = chr(0x1f)      # separator that cannot appear inside the joined strings
_DISABLED = os.environ.get('EOS_ORACLE', '1').strip().lower() in ('0', 'false', 'no')


def enabled():
    return not _DISABLED


def _work_dir():
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import env
    return env.work_dir()


def journal_path():
    return os.path.join(_work_dir(), 'cases.jsonl')


# --------------------------------------------------------------------------- #
# media fingerprint (stable, machine-independent)
# --------------------------------------------------------------------------- #
def media_fp(layout):
    """A stable id for a media layout, never containing absolute paths."""
    parts = []
    main = layout.get('main')
    if main:
        parts.append('main=%s' % os.path.basename(main.get('name') or main.get('path') or ''))
        parts.append('kind=%s' % (main.get('kind') or ''))
    ovr_names = []
    for e in layout.get('overrideDirs', []):
        d = e.get('dir') or ''
        ovr_names.append(os.path.basename(d.rstrip('/\\')))
        try:
            for f in sorted(os.listdir(d)):
                if f.lower().endswith('.jar'):
                    ovr_names.append(os.path.basename(f))
        except OSError:
            pass
    if ovr_names:
        parts.append('ovr=' + ','.join(sorted(set(ovr_names))))
    fp = hashlib.sha1(_ISOLATION.join(parts).encode('utf-8')).hexdigest()[:12]
    return fp


# --------------------------------------------------------------------------- #
# truth matching
# --------------------------------------------------------------------------- #
def source_basename(source):
    """Reduce a candidate source label to a comparable jar basename.

    source may be a bare jar name, a full path, a nested label
    ('BOOT-INF/lib/x.jar!/classes'), or 'lib/x.jar'. Strip path and any
    '!/...' suffix, so '00000000-20250408.jar' compares cleanly.
    """
    s = str(source or '').replace('\\', '/')
    if '!/' in s:
        s = s.split('!/', 1)[0]
    return os.path.basename(s.rstrip('/'))


def truth_matches(sources, truth):
    """True if any source's jar basename equals (or embeds) the truth jar name."""
    if not truth:
        return None                      # no ground truth given -> unconfirmed
    t = source_basename(truth).lower()
    if not t:
        return None
    for s in sources or []:
        b = source_basename(s).lower()
        if b and (b == t or t in b or b in t):
            return True
    return False


# --------------------------------------------------------------------------- #
# journal read/write
# --------------------------------------------------------------------------- #
def _read_all():
    path = journal_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def lookup(media, fqcn, method, line):
    """Most recent CONFIRMED case for this (media, fqcn, method, line), or None."""
    if not enabled():
        return None
    for c in reversed(_read_all()):
        if (c.get('mediaFp') == media and c.get('fqcn') == fqcn
                and c.get('method') == method and c.get('line') == line
                and c.get('confirmed')):
            return c
    return None


def record(media, fqcn, method, line, verdict, sources, truth=None):
    """Append one case. Returns the case dict (with `confirmed` decided)."""
    match = truth_matches(sources, truth)
    confirmed = bool(truth and match)
    case = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'mediaFp': media,
        'fqcn': fqcn,
        'method': method,
        'line': line,
        'verdict': verdict,
        'sources': list(sources or []),
        'truth': truth,
        'truthMatch': match,
        'confirmed': confirmed,
    }
    if not enabled():
        return case
    try:
        path = journal_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(case, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return case


def review_stats():
    """Small summary of the journal, for reports: totals and confirmation rate."""
    rows = _read_all()
    confirmed = sum(1 for c in rows if c.get('confirmed'))
    return {'total': len(rows), 'confirmed': confirmed,
            'byFqcn': len({(c.get('fqcn'), c.get('method')) for c in rows})}
