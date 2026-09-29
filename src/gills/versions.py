"""Native Debian ordering and RPM's segment/tilde/caret ordering."""

import re

from debian.debian_support import Version


def rpm_segment_cmp(a, b):
    while a or b:
        a = re.sub(r"^[^a-zA-Z0-9~^]+", "", a)
        b = re.sub(r"^[^a-zA-Z0-9~^]+", "", b)
        if a.startswith("~") or b.startswith("~"):
            if not a.startswith("~"):
                return 1
            if not b.startswith("~"):
                return -1
            a, b = a[1:], b[1:]
            continue
        if a.startswith("^") or b.startswith("^"):
            if not a:
                return -1
            if not b:
                return 1
            if not a.startswith("^"):
                return 1
            if not b.startswith("^"):
                return -1
            a, b = a[1:], b[1:]
            continue
        if not a or not b:
            break
        numeric = a[0].isdigit()
        aa = re.match(r"[0-9]+" if numeric else r"[a-zA-Z]+", a).group()
        bb_match = re.match(r"[0-9]+" if numeric else r"[a-zA-Z]+", b)
        if not bb_match:
            return 1 if numeric else -1
        bb = bb_match.group()
        a, b = a[len(aa) :], b[len(bb) :]
        if numeric:
            aa, bb = aa.lstrip("0"), bb.lstrip("0")
            if len(aa) != len(bb):
                return (len(aa) > len(bb)) - (len(aa) < len(bb))
        if aa != bb:
            return (aa > bb) - (aa < bb)
    return bool(a) - bool(b)


def parts(version, backend):
    if backend == "apt":
        v = Version(version)
        return str(v.epoch or "0"), v.upstream_version, v.debian_revision or "0"
    epoch, separator, tail = version.partition(":")
    if not separator:
        epoch, tail = "0", version
    upstream, separator, revision = tail.rpartition("-")
    return epoch, upstream if separator else tail, revision if separator else ""


def compare(a, b, backend):
    if backend == "apt":
        return (Version(a) > Version(b)) - (Version(a) < Version(b))
    ea, va, ra = parts(a, backend)
    eb, vb, rb = parts(b, backend)
    return (
        ((int(ea) > int(eb)) - (int(ea) < int(eb)))
        or rpm_segment_cmp(va, vb)
        or rpm_segment_cmp(ra, rb)
    )


def change_level(old, new, backend):
    a, b = parts(old, backend), parts(new, backend)
    return "upstream" if a[:2] != b[:2] else "packaging"
