#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from config import KERNEL_VERSION


def generate_build_matrix() -> list:
    matrix_path = Path(__file__).parent.parent / "config" / "matrix.json"
    with open(matrix_path, 'r') as f:
        matrix = json.load(f)

    builds = []
    for key, configs in matrix.items():
        android, kernel = key.split('-')
        for cfg in configs:
            # Only include entries the matrix marks as enabled - keeps this
            # in sync with update_matrix.py (which adds new respins as
            # enabled: false) and with build-kernel.sh's local build logic.
            if not cfg.get("enabled", True):
                continue
            build = {
                "android": android,
                "kernel": kernel,
                "sub_level": cfg["sub_level"],
                "os_patch": cfg["os_patch_level"],
            }
            if "revision" in cfg:
                build["revision"] = cfg["revision"]
            if "kernel_tag" in cfg:
                build["kernel_tag"] = cfg["kernel_tag"]
            if cfg.get("lts"):
                build["lts"] = True
            builds.append(build)

    # Sort by Android version and kernel version
    builds.sort(key=lambda x: (
        int(x["android"].replace("android", "")),
        float(x["kernel"]),
        x["sub_level"] if x["sub_level"] != "X" else "ZZZZ"  # X (LTS) goes last
    ))

    return builds


def generate_classified_matrix() -> dict:
    """Generate a matrix grouped by Android version (enabled entries only -
    see the note in generate_build_matrix())."""
    matrix_path = Path(__file__).parent.parent / "config" / "matrix.json"
    with open(matrix_path, 'r') as f:
        matrix = json.load(f)

    classified = {}
    for key, configs in matrix.items():
        android, kernel = key.split('-')
        for cfg in configs:
            if not cfg.get("enabled", True):
                continue
            if android not in classified:
                classified[android] = {}
            if kernel not in classified[android]:
                classified[android][kernel] = []
            classified[android][kernel].append(cfg)

    # Sort
    sorted_classified = {}
    for android in sorted(classified.keys(), key=lambda x: int(x.replace("android", ""))):
        sorted_classified[android] = {}
        for kernel in sorted(classified[android].keys(), key=lambda x: float(x)):
            # Sort by sub_level, X (LTS) goes last
            sorted_classified[android][kernel] = sorted(
                classified[android][kernel],
                key=lambda x: x["sub_level"] if x["sub_level"] != "X" else "ZZZZ"
            )

    return sorted_classified


def save_matrix_output():
    builds = generate_build_matrix()
    output = 'matrix=' + json.dumps(builds)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(output + '\n')
    print(f"Matrix generated: {len(builds)} builds")

    # Save version number
    version_output = f'kernel_version={KERNEL_VERSION}'
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(version_output + '\n')
    print(f"Kernel version: {KERNEL_VERSION}")

    # Also save the classified matrix
    classified = generate_classified_matrix()
    classified_output = 'classified_matrix=' + json.dumps(classified)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(classified_output + '\n')
    print(f"Classified matrix saved")

    # Save matrix summary
    summary = []
    for android in sorted(classified.keys(), key=lambda x: int(x.replace("android", ""))):
        for kernel, configs in classified[android].items():
            sub_levels = [c["sub_level"] for c in configs]
            summary.append(f"{android}-{kernel}: {', '.join(sub_levels)}")

    summary_output = 'matrix_summary=' + json.dumps(summary)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(summary_output + '\n')

    # Save Markdown-formatted summary
    md_summary = "### Build Matrix Summary\n\n"
    for android in sorted(classified.keys(), key=lambda x: int(x.replace("android", ""))):
        md_summary += f"**{android.upper()}**\n\n"
        for kernel, configs in classified[android].items():
            sub_levels = ", ".join([c["sub_level"] + (" (LTS)" if c.get("lts") else "") for c in configs])
            md_summary += f"- {kernel}: {sub_levels}\n"
        md_summary += "\n"

    with open("matrix_summary.md", 'w', encoding='utf-8') as f:
        f.write(md_summary)


if __name__ == '__main__':
    save_matrix_output()
