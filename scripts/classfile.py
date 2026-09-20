#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure-Python Java .class parser: member names + LineNumberTable, no JVM.

WHY: every `javap` invocation starts a JVM (~0.4-0.9 s measured). Arbitration
needs the LineNumberTable of EVERY distinct class body, so a class with 5 candidate
bodies costs several seconds even though the answer is a few hundred bytes of data
that already sits in the .class file.

This parser reads the class file directly:
  constant pool -> methods -> Code attribute -> LineNumberTable
and returns the same (source_line, bytecode_offset) rows javap prints, in
microseconds instead of seconds.

Everything here is verified against javap output by validate_classfile.py, which
must report 0 mismatches across every product class on the media before this
backend is trusted.
"""

import struct
import zipfile

# ---------------------------------------------------------------- constant pool
_CP_SIZES = {
    3: 4, 4: 4, 5: 8, 6: 8,      # Integer Float Long Double
    7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4,   # Class String Field/Method/IMethodref NameAndType
    15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2,  # MethodHandle MethodType Dynamic InvokeDynamic Module Package
}


class _R:
    __slots__ = ('d', 'i')

    def __init__(self, data):
        self.d = data
        self.i = 0

    def u1(self):
        v = self.d[self.i]
        self.i += 1
        return v

    def u2(self):
        v = struct.unpack_from('>H', self.d, self.i)[0]
        self.i += 2
        return v

    def u4(self):
        v = struct.unpack_from('>I', self.d, self.i)[0]
        self.i += 4
        return v

    def skip(self, n):
        self.i += n


def _parse_constant_pool(r, count):
    """Return index -> utf8 str for Utf8 entries (others are None)."""
    pool = [None] * count
    i = 1
    while i < count:
        tag = r.u1()
        if tag == 1:                     # Utf8
            ln = r.u2()
            raw = r.d[r.i:r.i + ln]
            r.i += ln
            pool[i] = raw.decode('utf-8', 'replace')
        else:
            size = _CP_SIZES.get(tag)
            if size is None:
                raise ValueError('unknown constant pool tag %d at index %d' % (tag, i))
            r.skip(size)
            if tag in (5, 6):            # Long/Double take two slots
                i += 1
        i += 1
    return pool


def _parse_attributes(r, cp, wanted):
    """Parse an attribute table; return {attr_name: [payload_start, length]} for wanted.

    Payloads are captured as byte slices so nested structures can be re-read.
    """
    out = {}
    n = r.u2()
    for _ in range(n):
        name_idx = r.u2()
        length = r.u4()
        start = r.i
        r.skip(length)
        name = cp[name_idx] if name_idx < len(cp) else None
        if wanted is None or name in wanted:
            out.setdefault(name, []).append(r.d[start:start + length])
    return out


def _line_rows_from_code(code_payload, cp):
    """Extract [(line, start_pc), ...] from a Code attribute payload."""
    r = _R(code_payload)
    r.u2()                                # max_stack
    r.u2()                                # max_locals
    code_len = r.u4()
    r.skip(code_len)
    exc_len = r.u2()
    r.skip(exc_len * 8)                   # each: start_pc,end_pc,handler_pc,catch_type
    attrs = _parse_attributes(r, cp, {'LineNumberTable'})
    payloads = attrs.get('LineNumberTable') or []
    rows = []
    for p in payloads:
        rr = _R(p)
        cnt = rr.u2()
        for _ in range(cnt):
            start_pc = rr.u2()
            line = rr.u2()
            rows.append((line, start_pc))
    rows.sort(key=lambda t: (t[1], t[0]))
    return rows


def parse_class_bytes(data):
    """Parse class bytes -> list of dicts: name, descriptor, access, lines.

    `lines` is [(source_line, bytecode_offset), ...], identical to what
    `javap -p -l` prints in a method's LineNumberTable.
    """
    r = _R(data)
    if r.u4() != 0xCAFEBABE:
        raise ValueError('not a class file (bad magic)')
    r.u2()                                # minor
    r.u2()                                # major
    cp_count = r.u2()
    cp = _parse_constant_pool(r, cp_count)

    r.u2()                                # access_flags
    r.u2()                                # this_class
    r.u2()                                # super_class
    iface = r.u2()
    r.skip(iface * 2)

    for _ in range(r.u2()):               # fields
        r.u2(); r.u2(); r.u2()
        _parse_attributes(r, cp, None)

    methods = []
    for _ in range(r.u2()):
        access = r.u2()
        name_idx = r.u2()
        desc_idx = r.u2()
        attrs = _parse_attributes(r, cp, {'Code'})
        name = cp[name_idx] if name_idx < len(cp) else None
        desc = cp[desc_idx] if desc_idx < len(cp) else None
        lines = []
        for payload in attrs.get('Code') or []:
            lines.extend(_line_rows_from_code(payload, cp))
        methods.append({'name': name, 'descriptor': desc, 'access': access,
                        'lines': lines})
    return methods


def parse_class_in_jar(jar, entry):
    with zipfile.ZipFile(jar) as z:
        return parse_class_bytes(z.read(entry))


def class_bytes_in_jar(jar, entry):
    with zipfile.ZipFile(jar) as z:
        return z.read(entry)


def members_from_class_bytes(data):
    """Same shape as javap_parse.parse_members: {signature: [(line, offset), ...]}.

    Signature is `name(descriptor-params)` — enough for member_name() and for
    overload disambiguation, without reimplementing javap's source-style printing.
    """
    out = {}
    for m in parse_class_bytes(data):
        if not m['lines']:
            continue
        desc = m['descriptor'] or ''
        params = desc.split('(', 1)[1].rsplit(')', 1)[0] if '(' in desc else ''
        sig = '%s(%s)' % (m['name'], params)
        if sig in out:
            out[sig] = out[sig] + m['lines']
        else:
            out[sig] = list(m['lines'])
    return out


if __name__ == '__main__':
    import sys
    jar, entry = sys.argv[1], sys.argv[2]
    ms = parse_class_in_jar(jar, entry)
    print('methods (all) = %d' % len(ms))
    for m in ms:
        if m['lines']:
            print('  %-34s lines %s' % (m['name'], sorted({l for l, _ in m['lines']})))
