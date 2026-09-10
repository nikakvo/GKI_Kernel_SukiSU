#!/usr/bin/env python3
"""
update_matrix.py - refreshes matrix.json with the latest known respin
(sub_level + os_patch_level + kernel_tag) for each Android/kernel branch,
sourced directly from Google's kernel/common repo.

Technique: `git ls-remote --tags` (the same approach kernel_builder.py
already uses for deprecated-branch detection) to discover every respin
tag (e.g. android13-5.15-2025-12_r10), then one gitiles TEXT-format fetch
per *winning* tag to read the Makefile's VERSION/PATCHLEVEL/SUBLEVEL and
resolve the actual sub_level number (the tag name alone doesn't carry it).

matrix.json becomes a CACHE of what this script last found - Google's
repo remains the source of truth. This script does NOT touch the
`enabled` flag on existing entries, and NEVER deletes an existing entry
(even an old one). New patch-levels it discovers are added with
"enabled": false so nothing gets built without your explicit opt-in, and
only within the --months lookback window (default 24) to avoid the
matrix growing back to 2021.

Usage:
    python3 update_matrix.py                 # update in place, last 24 months
    python3 update_matrix.py --dry-run        # only print what would change
    python3 update_matrix.py --months 12      # narrower lookback window
"""
import subprocess
import re
import json
import sys
import time
import base64
import urllib.request
import urllib.error
from pathlib import Path
from datetime import date
from collections import defaultdict

REMOTE = "https://android.googlesource.com/kernel/common"
MATRIX_FILE = Path(__file__).resolve().parent / "matrix.json"

# Only the families this project builds (matches config.py's
# ANDROID_KERNEL_MAP, plus android16/17 for tracking ahead of time).
TRACKED = {
    "android12": "5.10",
    "android13": "5.15",
    "android14": "6.1",
    "android15": "6.6",
    "android16": "6.12",
    "android17": "6.18",
}

TAG_RE = re.compile(r'^(android1[2-7])-(5\.10|5\.15|6\.1|6\.6|6\.12|6\.18)-(\d{4}-\d{2})_r(\d+)$')

# Newer per-sublevel LTS-merge tags (e.g. android13-5.15.209_r00), used
# once a branch is EOL on the date-based scheme and instead receives
# periodic merges from the upstream Linux -stable tree straight onto the
# android*-lts branch. The sub_level is embedded directly in the tag, so
# unlike the dash scheme above no Makefile fetch is needed to learn it -
# and critically, it must NEVER be keyed by os_patch_level like the dash
# entries are: Google can (and does) publish more than one of these
# under what would be the "same" rough date window, so os_patch_level is
# not a unique key across the two schemes.
DOT_TAG_RE = re.compile(r'^(android1[2-7])-(5\.10|5\.15|6\.1|6\.6|6\.12|6\.18)\.(\d+)_r(\d+)$')

# A raw commit SHA (kernel_builder.py accepts this as a kernel_tag value
# to pin an LTS-merge commit before Google has cut its official _r00
# tag - see kernel_builder.py's _is_commit_sha). It's just as much an
# "LTS-identity" (unique per sub_level, NOT per os_patch_level) as a
# DOT_TAG_RE match is - a kernel_tag that's a SHA must never be treated
# as a dash-style entry (keyed by os_patch_level) here, or it collides
# with whatever dash-style respin happens to share that same rough date
# window and gets silently overwritten/downgraded by it.
_SHA_RE = re.compile(r'^[0-9a-fA-F]{7,40}$')


def is_lts_style_tag(kernel_tag: str) -> bool:
    """True if this kernel_tag identifies an LTS-merge build - either a
    dot-style official tag or a raw SHA pinned ahead of one - as opposed
    to a regular dash-style date-based respin tag. Determines which
    identity (sub_level vs os_patch_level) this entry must be matched/
    updated by in main() below."""
    return bool(DOT_TAG_RE.match(kernel_tag) or _SHA_RE.match(kernel_tag))

# Dot-style tags carry no date, so the --months window can't filter them.
# Instead: only ever consider the N highest sub_levels found per
# (android, kernel) - keeps a fresh run from importing years of LTS
# history as a wall of "+ NEW" entries.
DOT_KEEP_LATEST_N = 15

REQUEST_DELAY = 0.4          # seconds between Makefile fetches - avoids 429
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0       # seconds, doubles each retry (2, 4, 8, 16, 32)


def fetch_tags() -> list[str]:
    print("Fetching tag list from kernel/common (git ls-remote)...")
    result = subprocess.run(
        ["git", "ls-remote", "--tags", REMOTE],
        capture_output=True, text=True, check=True, timeout=120,
    )
    tags = []
    for line in result.stdout.splitlines():
        if "refs/tags/" not in line:
            continue
        tag = line.split("refs/tags/", 1)[1]
        if tag.endswith("^{}"):
            continue
        tags.append(tag)
    print(f"Found {len(tags)} tags total.")
    return tags


def months_ago(patch: str, now: date) -> int:
    """How many months back a 'YYYY-MM' patch string is, relative to now."""
    y, m = (int(x) for x in patch.split("-"))
    return (now.year - y) * 12 + (now.month - m)


def find_latest_respins(tags: list[str], months: int) -> dict:
    """{(android, kernel): {os_patch_level: (respin, tag_name)}} - only
    patches within the lookback window. Dash-style (date-based) tags
    only - see find_latest_lts_respins() for the newer per-sublevel
    scheme."""
    now = date.today()
    latest = defaultdict(dict)
    for tag in tags:
        m = TAG_RE.match(tag)
        if not m:
            continue
        android, kernel, patch, respin = m.groups()
        if TRACKED.get(android) != kernel:
            continue
        if months_ago(patch, now) > months:
            continue
        respin = int(respin)
        key = (android, kernel)
        current = latest[key].get(patch)
        if current is None or respin > current[0]:
            latest[key][patch] = (respin, tag)
    return latest


def find_latest_lts_respins(tags: list[str]) -> dict:
    """{(android, kernel): {sub_level: (respin, tag)}} for the newer
    per-sublevel LTS-merge tags (android13-5.15.209_r00 etc). sub_level
    is read straight from the tag - no Makefile fetch needed. Keeps
    only the highest-respin tag per sub_level, and only the
    DOT_KEEP_LATEST_N highest sub_levels per (android, kernel), since
    these tags carry no date to bound the lookback by."""
    latest = defaultdict(dict)
    for tag in tags:
        m = DOT_TAG_RE.match(tag)
        if not m:
            continue
        android, kernel, sub_level, respin = m.groups()
        if TRACKED.get(android) != kernel:
            continue
        respin = int(respin)
        key = (android, kernel)
        current = latest[key].get(sub_level)
        if current is None or respin > current[0]:
            latest[key][sub_level] = (respin, tag)

    # Trim to the DOT_KEEP_LATEST_N highest sub_levels per (android, kernel).
    trimmed = defaultdict(dict)
    for key, by_sub in latest.items():
        top_subs = sorted(by_sub.keys(), key=lambda s: int(s), reverse=True)[:DOT_KEEP_LATEST_N]
        for sub_level in top_subs:
            trimmed[key][sub_level] = by_sub[sub_level]
    return trimmed


def fetch_sub_level(tag: str) -> str | None:
    """Read VERSION/PATCHLEVEL/SUBLEVEL from the Makefile at this exact tag,
    with retry/backoff on HTTP 429 (gitiles rate limiting)."""
    url = f"{REMOTE}/+/refs/tags/{tag}/Makefile?format=TEXT"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Python"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = base64.b64decode(resp.read()).decode("utf-8", errors="ignore")
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"  429 for {tag}, waiting {delay:.0f}s and retrying "
                      f"({attempt}/{MAX_RETRIES})...")
                time.sleep(delay)
                continue
            print(f"  ! Failed to read Makefile for {tag}: {e}")
            return None
        except Exception as e:
            print(f"  ! Failed to read Makefile for {tag}: {e}")
            return None
    else:
        return None

    m = re.search(r'^SUBLEVEL\s*=\s*(\S+)', content, re.MULTILINE)
    if not m:
        print(f"  ! Could not find SUBLEVEL in the Makefile for {tag}")
        return None
    return m.group(1)


def load_matrix() -> dict:
    if MATRIX_FILE.exists():
        with open(MATRIX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def reorder_entry(entry: dict) -> dict:
    """Canonical key order for readability: sub_level, os_patch_level,
    kernel_tag, revision (if present), then 'enabled' always LAST so it's
    easy to scan/toggle without reading through everything else first."""
    order = ["sub_level", "os_patch_level", "kernel_tag", "revision"]
    reordered = {}
    for k in order:
        if k in entry:
            reordered[k] = entry[k]
    for k, v in entry.items():
        if k not in reordered and k != "enabled":
            reordered[k] = v
    if "enabled" in entry:
        reordered["enabled"] = entry["enabled"]
    return reordered


def write_matrix_compact(matrix: dict, path: Path):
    """Write matrix.json with each entry on a single line (matches the
    original hand-edited style) instead of json.dump's one-key-per-line."""
    lines = ["{"]
    top_keys = list(matrix.keys())
    for ki, key in enumerate(top_keys):
        entries = matrix[key]
        lines.append(f'    "{key}": [')
        for ei, entry in enumerate(entries):
            comma = "," if ei < len(entries) - 1 else ""
            lines.append(f"        {json.dumps(reorder_entry(entry), ensure_ascii=False)}{comma}")
        comma = "," if ki < len(top_keys) - 1 else ""
        lines.append(f"    ]{comma}")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    dry_run = "--dry-run" in sys.argv
    months = 24
    if "--months" in sys.argv:
        idx = sys.argv.index("--months")
        months = int(sys.argv[idx + 1])

    tags = fetch_tags()
    latest = find_latest_respins(tags, months)
    latest_lts = find_latest_lts_respins(tags)
    matrix = load_matrix()

    changes = []
    warnings = []

    for android, kernel in TRACKED.items():
        matrix_key = f"{android}-{kernel}"
        entries = matrix.setdefault(matrix_key, [])

        # Split existing entries by which naming scheme produced them,
        # keyed by their real unique identity for that scheme. This is
        # the critical fix: os_patch_level is NOT a unique key across
        # both schemes - Google can (and does) publish more than one
        # dot-style LTS tag under what looks like the same rough date
        # window as the current dash-style respin, so keying everything
        # by os_patch_level made unrelated entries silently collide and
        # overwrite one another.
        dash_by_patch = {e["os_patch_level"]: e for e in entries if not is_lts_style_tag(e.get("kernel_tag", ""))}
        dot_by_sub = {str(e["sub_level"]): e for e in entries if is_lts_style_tag(e.get("kernel_tag", ""))}

        # --- dash-style (date-based) respins ---
        for patch, (respin, tag) in sorted(latest.get((android, kernel), {}).items()):
            print(f"{matrix_key} / {patch}: latest respin is r{respin} ({tag})")
            sub_level = fetch_sub_level(tag)
            time.sleep(REQUEST_DELAY)
            if sub_level is None:
                warnings.append(f"{matrix_key} {patch}: skipped (could not determine sub_level)")
                continue

            existing = dash_by_patch.get(patch)
            if existing is None:
                new_entry = {
                    "sub_level": sub_level,
                    "os_patch_level": patch,
                    "kernel_tag": tag,
                    "enabled": False,
                }
                entries.append(new_entry)
                dash_by_patch[patch] = new_entry
                changes.append(f"+ NEW: {matrix_key} {patch} -> sub_level {sub_level}, {tag} (enabled: false)")
            else:
                old_tag = existing.get("kernel_tag")
                old_sub = existing.get("sub_level")
                if old_tag != tag or old_sub != sub_level:
                    changes.append(
                        f"~ UPDATED: {matrix_key} {patch}: "
                        f"sub_level {old_sub}->{sub_level}, tag {old_tag}->{tag} "
                        f"(enabled stays: {existing.get('enabled', True)})"
                    )
                    existing["sub_level"] = sub_level
                    existing["kernel_tag"] = tag

        # --- dot-style (per-sublevel LTS-merge) respins ---
        # A brand-new sub_level here needs *some* os_patch_level value
        # (the schema/build.py require one) even though the tag itself
        # doesn't carry a date. Reuse whatever the current dash-style
        # patch label for this branch is this run, matching the
        # existing hand-added convention (208/209 both use "2026-06").
        fallback_patch = None
        if latest.get((android, kernel)):
            fallback_patch = sorted(latest[(android, kernel)].keys())[-1]
        elif dash_by_patch:
            fallback_patch = sorted(dash_by_patch.keys())[-1]

        for sub_level, (respin, tag) in sorted(latest_lts.get((android, kernel), {}).items(), key=lambda kv: int(kv[0])):
            print(f"{matrix_key} / sub_level {sub_level} (LTS): latest respin is r{respin} ({tag})")
            existing = dot_by_sub.get(sub_level)
            if existing is None:
                if fallback_patch is None:
                    warnings.append(
                        f"{matrix_key} sub_level {sub_level} (LTS): skipped adding - no os_patch_level "
                        f"could be inferred (no dash-style respin found for this branch this run either); "
                        f"add it manually with an explicit os_patch_level"
                    )
                    continue
                new_entry = {
                    "sub_level": sub_level,
                    "os_patch_level": fallback_patch,
                    "kernel_tag": tag,
                    "enabled": False,
                    "lts": True,
                }
                entries.append(new_entry)
                dot_by_sub[sub_level] = new_entry
                changes.append(f"+ NEW: {matrix_key} sub_level {sub_level} (LTS) -> {tag} (enabled: false)")
            else:
                old_tag = existing.get("kernel_tag")
                if old_tag != tag:
                    changes.append(
                        f"~ UPDATED: {matrix_key} sub_level {sub_level} (LTS): "
                        f"tag {old_tag}->{tag} (enabled stays: {existing.get('enabled', True)})"
                    )
                    existing["kernel_tag"] = tag
                if not existing.get("lts"):
                    existing["lts"] = True

        entries.sort(key=lambda e: (e["os_patch_level"], int(e["sub_level"]) if str(e["sub_level"]).isdigit() else 0))

        active_patches = set(latest.get((android, kernel), {}).keys())
        active_lts_subs = set(latest_lts.get((android, kernel), {}).keys())
        for e in entries:
            if not e.get("enabled"):
                continue
            tag = e.get("kernel_tag", "")
            if _SHA_RE.match(tag):
                # SHA-pinned entries are inherently unofficial - they'll
                # never show up in the official tag list until Google
                # cuts a real _r00 for that sub_level, so "not found
                # among current tags" is expected here, not a warning
                # sign. (The update loop above will silently upgrade it
                # to the real tag automatically once one appears.)
                continue
            is_dot = is_lts_style_tag(tag)
            still_active = (str(e["sub_level"]) in active_lts_subs) if is_dot else (e["os_patch_level"] in active_patches)
            if not still_active:
                warnings.append(
                    f"⚠ {matrix_key} {e['os_patch_level']} (sub_level {e['sub_level']}) "
                    f"is enabled, but the branch no longer appears among current tags "
                    f"(deprecated/removed, or outside the {months}-month window / "
                    f"top-{DOT_KEEP_LATEST_N} LTS sub_levels) - check manually"
                )

    print("\n" + "=" * 60)
    if changes:
        print(f"Changes ({len(changes)}):")
        for c in changes:
            print(f"  {c}")
    else:
        print("No changes - matrix.json is already up to date.")

    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  {w}")
    print("=" * 60)

    if dry_run:
        print("\n--dry-run: matrix.json was NOT written.")
        return

    if changes:
        write_matrix_compact(matrix, MATRIX_FILE)
        print(f"\nmatrix.json updated: {MATRIX_FILE}")
    else:
        print("\nNothing to write.")


if __name__ == "__main__":
    main()
