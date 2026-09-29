import fnmatch
import re
from collections import defaultdict
from functools import cmp_to_key

from .model import digest
from .versions import change_level, compare, parts


def match(package, watch):
    """Return both a decision and a human-readable explanation."""
    if package.kind not in watch["kinds"]:
        return False, "package kind excluded"
    f = watch["filters"]
    for key, value in (("packages", package.name), ("sources", package.source)):
        if f.get(key) and not any(fnmatch.fnmatchcase(value, pattern) for pattern in f[key]):
            return False, f"does not match {key} globs"
        if any(fnmatch.fnmatchcase(value, pattern) for pattern in f.get("exclude_" + key, [])):
            return False, f"matches exclude_{key}"
    for key, value in (("package_regex", package.name), ("source_regex", package.source)):
        if f.get(key) and not re.search(f[key], value):
            return False, f"does not match {key}"
    version = (
        package.version
        if f["version_field"] == "full"
        else parts(package.version, watch["type"])[1]
    )
    if f.get("version_include") and not re.search(f["version_include"], version):
        return False, "does not match version_include"
    if f.get("version_exclude") and re.search(f["version_exclude"], version):
        return False, "matches version_exclude"
    if f.get("min_version") and compare(package.version, f["min_version"], watch["type"]) < 0:
        return False, "below min_version (full native version)"
    return True, "included"


def changes(old, new, watch):
    """Compare complete version sets; replacing the current version isn't a removal."""
    old_slots, new_slots = defaultdict(dict), defaultdict(dict)
    for package in old:
        old_slots[package.slot][package.version] = package
    for package in new:
        new_slots[package.slot][package.version] = package
    result = []
    key = cmp_to_key(lambda a, b: compare(a, b, watch["type"]))
    for slot in sorted(set(old_slots) | set(new_slots)):
        before, after = old_slots[slot], new_slots[slot]
        previous = before[max(before, key=key)] if before else None
        for version in sorted(after, key=key):
            package = after[version]
            earlier = before.get(version)
            level = None
            is_downgrade = (
                previous is not None
                and version == max(after, key=key)
                and compare(version, previous.version, watch["type"]) < 0
            )
            if is_downgrade:
                event_type = "package.downgraded"
                earlier = previous
                level = change_level(previous.version, version, watch["type"])
            elif earlier:
                if (
                    earlier.content == package.content
                    and earlier.source == package.source
                    and earlier.source_version == package.source_version
                ):
                    continue
                event_type = "package.repacked"
            elif previous is None:
                event_type = "package.added"
            else:
                order = compare(version, previous.version, watch["type"])
                # An older version newly retained in a multiversion repository is an addition.
                event_type = (
                    "package.updated"
                    if order > 0
                    else (
                        "package.downgraded"
                        if previous.version not in after
                        and compare(max(after, key=key), previous.version, watch["type"]) < 0
                        else "package.added"
                    )
                )
                level = change_level(previous.version, version, watch["type"])
            if event_type not in watch["events"]:
                continue
            if level and watch["version_policy"] != "any" and watch["version_policy"] != level:
                continue
            result.append(
                {
                    "type": event_type,
                    "level": level,
                    "old": (earlier or previous).to_dict() if earlier or previous else None,
                    "new": package.to_dict(),
                }
            )
        if not after and "package.removed" in watch["events"]:
            result.append(
                {"type": "package.removed", "level": None, "old": previous.to_dict(), "new": None}
            )
    return result


def group(changeset, watch):
    grouped = defaultdict(list)
    for change in changeset:
        package = change["new"] or change["old"]
        if watch["group_by_source"]:
            key = (change["type"], package["source"], package["source_version"])
        else:
            key = (change["type"], digest(change))
        grouped[key].append(change)
    return [
        {
            "type": key[0],
            "source": (items[0]["new"] or items[0]["old"])["source"],
            "source_version": (items[0]["new"] or items[0]["old"])["source_version"],
            "changes": items,
        }
        for key, items in sorted(grouped.items())
    ]


def missing_requirements(event, inventory, watch):
    r = watch["readiness"]
    if not r or event["type"] == "package.removed":
        return []
    candidates = [
        p
        for p in inventory
        if p.source == event["source"] and p.source_version == event["source_version"]
    ]
    missing = []
    for suite in r["suites"]:
        packages = [p for p in candidates if p.suite == suite]
        if r["require_source"] and not any(p.kind == "source" for p in packages):
            missing.append(f"{suite}: source {event['source']} {event['source_version']}")
        for pattern in r["binaries"]:
            for arch in r["architectures"]:
                if not any(
                    p.kind == "binary"
                    and p.architecture in (arch, "all", "noarch")
                    and fnmatch.fnmatchcase(p.name, pattern)
                    for p in packages
                ):
                    missing.append(f"{suite}/{arch}: {pattern}")
    return missing
