#!/usr/bin/env python3
"""Quick sanity check: shows only the ENABLED matrix.json entries and
whether each one has a kernel_tag set. Run this from
.github/workflows/scripts/ (same place build-kernel.sh expects)."""
import json
from pathlib import Path

matrix_path = Path(__file__).resolve().parent.parent / "config" / "matrix.json"
with open(matrix_path) as f:
    data = json.load(f)

count = 0
for key, entries in data.items():
    for e in entries:
        if not e.get("enabled", True):
            continue
        count += 1
        tag = e.get("kernel_tag") or "(MISSING - will build from moving branch HEAD)"
        print(f"{key} / sub_level {e.get('sub_level')} / {e.get('os_patch_level')} -> kernel_tag: {tag}")

print(f"\nTotal enabled entries: {count}")
if count != 1:
    print("NOTE: you said you left only 1 enabled - double check matrix.json if this doesn't say 1.")
