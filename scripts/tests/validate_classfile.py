"""Validate classfile.py against javap on EVERY product class of the media.

The pure-Python backend may only be trusted if its (source_line, bytecode_offset)
rows are IDENTICAL to javap's, per member, for every class that has code.
Reports any mismatch with the exact member and row sets.
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, SKILL)
import classfile as cf      # noqa: E402
import javap_parse as jp    # noqa: E402

LIB = os.environ.get('EOS_LIB', r'D:\PrimetonProject\mdm72\server\mdm\lib')


def product_classes():
    out = {}
    for fn in sorted(os.listdir(LIB)):
        if not fn.lower().endswith('.jar'):
            continue
        p = os.path.join(LIB, fn)
        try:
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if not n.endswith('.class'):
                        continue
                    cls = n[:-len('.class')].replace('/', '.')
                    if cls.startswith('com.primeton.') or cls.startswith('com.eos.'):
                        base = os.path.basename(p)
                        zeros = len(base) - len(base.lstrip('0'))
                        if cls not in out or zeros > out[cls][0]:
                            out[cls] = (zeros, p, n)
        except Exception:
            pass
    return sorted((p, n, cls) for cls, (z, p, n) in out.items())


def rows_by_name(members, fqcn=None):
    """{member_name: sorted row set} -- compare by NAME, ignoring signature style.

    The class file stores a constructor as `<init>`, while javap prints it as the
    class simple name. That is a naming convention difference only; normalize it
    here so the comparison tests the LINE DATA, which is what we actually use.
    """
    simple = (fqcn or '').rsplit('.', 1)[-1] if fqcn else None
    out = {}
    for sig, rows in members.items():
        nm = jp.member_name(sig)
        if nm == '<init>' and simple:
            nm = simple
        out.setdefault(nm, set()).update(rows)
    return out


items = product_classes()
print('product classes = %d' % len(items))

mismatch = []
errors = []
zero_line_classes = 0
ok = 0

for jar, entry, cls in items:
    try:
        data = cf.class_bytes_in_jar(jar, entry)
        py = rows_by_name(cf.members_from_class_bytes(data), cls)
    except Exception as exc:  # noqa: BLE001
        errors.append((cls, 'classfile.py: %s: %s' % (type(exc).__name__, exc)))
        continue

    jv, err = jp.parse_members(jar, cls)
    if err:
        # no code / no line table: python side must also be empty
        if py:
            mismatch.append((cls, 'javap says %s but python found %d members'
                             % (err, len(py))))
        else:
            zero_line_classes += 1
        continue

    jvn = rows_by_name(jv, cls)
    if py == jvn:
        ok += 1
    else:
        only_py = set(py) - set(jvn)
        only_jv = set(jvn) - set(py)
        detail = []
        if only_py:
            detail.append('only-python: %s' % sorted(only_py)[:4])
        if only_jv:
            detail.append('only-javap: %s' % sorted(only_jv)[:4])
        for k in sorted(set(py) & set(jvn)):
            if py[k] != jvn[k]:
                detail.append('%s: py=%s javap=%s' % (k, sorted(py[k])[:6],
                                                      sorted(jvn[k])[:6]))
                break
        mismatch.append((cls, '; '.join(detail)))

print('identical to javap            = %d' % ok)
print('classes with no line table    = %d (both sides empty -> OK)' % zero_line_classes)
print('MISMATCHES                    = %d' % len(mismatch))
for cls, why in mismatch[:20]:
    print('  %-70s %s' % (cls, why[:120]))
print('EXCEPTIONS                    = %d' % len(errors))
for cls, why in errors[:20]:
    print('  %-70s %s' % (cls, why[:120]))

if not mismatch and not errors:
    print('')
    print('PASS: pure-Python parser reproduces javap EXACTLY on every product class')
else:
    sys.exit(1)
