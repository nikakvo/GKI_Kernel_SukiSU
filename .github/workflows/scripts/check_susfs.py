#!/usr/bin/env python3
"""Reports what has landed in susfs4ksu since the commit you're pinned
to, and whether any of it touches the files this pipeline actually
depends on - so deciding whether to move a --susfs-commit pin forward is
an informed call instead of a guess or a full speculative build.

Why this exists
---------------
susfs4ksu moves most days. Two of its files decide whether a build that
worked yesterday still works today:

  kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch
      Patches the SukiSU-Ultra tree itself. It is written against a
      specific SukiSU-Ultra API shape and evolves independently of any
      SukiSU-Ultra tag, so it is the file that historically broke builds
      out of nowhere while --ksu-commit stayed pinned and unchanged.

  kernel_patches/50_add_susfs_in_gki-<android>-<kernel>.patch
      Patches the AOSP kernel tree. Less volatile, but a change here can
      start conflicting with a given GKI respin.

Everything else in the repo (README, manager-side code, other branches)
cannot affect a build here. This script separates those two categories
so "14 new commits" turns into "13 of them are irrelevant, 1 needs a
test build".

It never checks anything out and never touches a build workspace's
checkout state - it only fetches and reads.

Usage
-----
  # every branch enabled in matrix.json, last 14 days of activity
  python3 check_susfs.py

  # compare against the pins you actually build with
  python3 check_susfs.py --pin 'gki-android12-5.10=ec785f4,gki-android13-5.15=bca0d23'

  # single branch
  python3 check_susfs.py --branch gki-android13-5.15 --pin bca0d23

  # in CI, or anywhere without an existing workspace clone
  python3 check_susfs.py --pin ... --github-summary
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SUSFS_REPO_URL = "https://github.com/ShirkNeko/susfs4ksu.git"

# Files a change to which can plausibly break a build here, most
# dangerous first. The label is what gets printed; the matcher takes the
# branch name so the per-branch kernel patch can be matched exactly
# rather than by wildcard (a change to another branch's patch file would
# be noise).
KSU_INTEGRATION_PATCH = "kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch"
SUSFS_VERSION_HEADER = "kernel_patches/include/linux/susfs.h"

# Verdicts differ by mode. With a pin, the question is "can I move it?".
# Without one you are already building branch HEAD, so there is no pin to
# move - the useful question becomes "is my next build likely to differ
# from my last one?". Using the same wording for both would tell an
# unpinned branch to test before moving a pin it does not have.
RISK_UP_TO_DATE = "UP TO DATE"
RISK_NONE = "SAFE TO MOVE"
RISK_TEST = "TEST FIRST"
RISK_HIGH = "TEST FIRST (integration patch changed)"

RISK_QUIET = "QUIET"
RISK_ACTIVE = "ACTIVE"
RISK_ACTIVE_HIGH = "ACTIVE (integration patch changed)"

UNPINNED_RISK = {
    RISK_UP_TO_DATE: RISK_QUIET,
    RISK_NONE: RISK_QUIET,
    RISK_TEST: RISK_ACTIVE,
    RISK_HIGH: RISK_ACTIVE_HIGH,
}


def run(cmd: list, cwd: Path, check: bool = True) -> str:
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({' '.join(cmd)}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def find_or_clone_repo(workspace: Path, explicit: Path = None) -> tuple:
    """Returns (repo_path, cleanup_path_or_None).

    Prefers an existing susfs4ksu clone in the build workspace so this
    costs nothing when run on the same machine that builds. Falls back to
    a blobless throwaway clone (CI, or a fresh checkout).
    """
    if explicit:
        if not (explicit / ".git").exists():
            raise RuntimeError(f"--repo {explicit} is not a git checkout")
        return explicit, None

    candidate = workspace / "susfs4ksu"
    if (candidate / ".git").exists():
        return candidate, None

    tmp = Path(tempfile.mkdtemp(prefix="susfs-check-"))
    print(f"No susfs4ksu clone at {candidate} - cloning a temporary one...",
          file=sys.stderr)
    subprocess.run(
        ["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
         SUSFS_REPO_URL, str(tmp / "susfs4ksu")],
        check=True,
    )
    return tmp / "susfs4ksu", tmp


def enabled_branches(matrix_path: Path) -> list:
    """Derives susfs4ksu branch names from the enabled matrix entries,
    the same way BuildConfig.kernel_branch does."""
    if not matrix_path.exists():
        return []
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    branches = []
    for family, entries in data.items():
        if not any(e.get("enabled") for e in entries):
            continue
        android, _, kernel = family.partition("-")
        branch = f"gki-{android}-{kernel}"
        if branch not in branches:
            branches.append(branch)
    return branches


def parse_pins(raw: str) -> tuple:
    """Returns (per_branch_dict, bare_ref_or_None). Mirrors the syntax
    kernel_builder._resolve_susfs_pin() accepts, so the exact string you
    build with can be pasted here."""
    raw = (raw or "").strip()
    if not raw:
        return {}, None
    if "=" not in raw:
        return {}, raw
    pins = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        branch, sep, ref = part.partition("=")
        if not sep or not branch.strip() or not ref.strip():
            raise RuntimeError(
                f"Could not parse --pin entry '{part}'. Expected a bare ref, or "
                f"comma-separated <branch>=<ref> pairs."
            )
        pins[branch.strip()] = ref.strip()
    return pins, None


def fetch_branch(repo: Path, branch: str) -> bool:
    """Updates origin/<branch> without touching the working tree. The
    leading + forces the remote-tracking ref even when the local clone
    sits on a detached pin, which is how builds leave it."""
    result = subprocess.run(
        ["git", "fetch", "--quiet", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        cwd=str(repo), capture_output=True, text=True,
    )
    return result.returncode == 0


def describe(repo: Path, ref: str) -> str:
    out = run(["git", "log", "-1", "--format=%h %cs %s", ref], repo, check=False)
    return out.strip() or "(unknown)"


def susfs_version_at(repo: Path, ref: str) -> str:
    out = run(["git", "show", f"{ref}:{SUSFS_VERSION_HEADER}"], repo, check=False)
    match = re.search(r'#define\s+SUSFS_VERSION\s+"([^"]+)"', out)
    return match.group(1) if match else "?"


def analyse_branch(repo: Path, branch: str, pin: str, since_days: int) -> dict:
    result = {
        "branch": branch,
        "pin": pin,
        "error": None,
        "head": None,
        "pinned_at": None,
        "count": 0,
        "relevant": [],
        "other": 0,
        "risk": RISK_UP_TO_DATE,
        "version_pin": None,
        "version_head": None,
    }

    if not fetch_branch(repo, branch):
        result["error"] = (f"branch '{branch}' does not exist on "
                           f"{SUSFS_REPO_URL} (susfs4ksu forks can lag "
                           f"behind upstream for newer GKI versions)")
        return result

    remote = f"origin/{branch}"
    result["head"] = describe(repo, remote)
    result["version_head"] = susfs_version_at(repo, remote)

    kernel_patch = f"kernel_patches/50_add_susfs_in_gki-{branch.replace('gki-', '')}.patch"

    if pin:
        resolved = run(["git", "rev-parse", "--verify", f"{pin}^{{commit}}"],
                       repo, check=False).strip()
        if not resolved:
            result["error"] = f"pin '{pin}' does not resolve to a commit"
            return result
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", resolved, remote],
            cwd=str(repo), capture_output=True,
        )
        if ancestor.returncode != 0:
            owners = run(["git", "branch", "-r", "--contains", resolved,
                          "--format=%(refname:short)"], repo, check=False)
            owner_list = [l.strip() for l in owners.splitlines()
                          if l.strip() and "->" not in l]
            result["error"] = (
                f"pin '{pin}' is not on {branch}"
                + (f" - it is on {', '.join(owner_list)}" if owner_list else "")
                + ". susfs4ksu keeps one branch per GKI version and a hash is "
                  "valid for exactly one of them."
            )
            return result
        result["pinned_at"] = describe(repo, resolved)
        result["version_pin"] = susfs_version_at(repo, resolved)
        rev_range = f"{resolved}..{remote}"
        window = ""
    else:
        rev_range = remote
        window = f"--since={since_days}.days.ago"

    log_cmd = ["git", "log", "--format=%H\x1f%h\x1f%cs\x1f%s", rev_range]
    if window:
        log_cmd.insert(2, window)
    commits = [l for l in run(log_cmd, repo, check=False).splitlines() if l.strip()]
    result["count"] = len(commits)

    for line in commits:
        full, short, date, subject = line.split("\x1f", 3)
        files = run(["git", "show", "--pretty=", "--name-only", full],
                    repo, check=False).splitlines()
        files = [f.strip() for f in files if f.strip()]
        hits = []
        if KSU_INTEGRATION_PATCH in files:
            hits.append(("HIGH", KSU_INTEGRATION_PATCH))
        if kernel_patch in files:
            hits.append(("MED", kernel_patch))
        if hits:
            result["relevant"].append({
                "short": short, "date": date, "subject": subject, "hits": hits,
            })
        else:
            result["other"] += 1

    if result["count"] == 0:
        result["risk"] = RISK_UP_TO_DATE
    elif not result["relevant"]:
        result["risk"] = RISK_NONE
    elif any(sev == "HIGH" for c in result["relevant"] for sev, _ in c["hits"]):
        result["risk"] = RISK_HIGH
    else:
        result["risk"] = RISK_TEST

    if not pin:
        result["risk"] = UNPINNED_RISK[result["risk"]]
    return result


def print_report(results: list, pinned_mode: bool, since_days: int):
    for r in results:
        print()
        print(f"{r['branch']}")
        print("-" * len(r["branch"]))
        if r["error"]:
            print(f"  ERROR: {r['error']}")
            continue
        if r["pinned_at"]:
            print(f"  your pin   : {r['pinned_at']}  [SUSFS {r['version_pin']}]")
        else:
            print(f"  your pin   : (none - this branch builds from branch HEAD)")
        print(f"  branch HEAD: {r['head']}  [SUSFS {r['version_head']}]")

        if r["count"] == 0:
            print("  nothing new" if pinned_mode
                  else f"  no commits in the last {since_days} days")
        elif pinned_mode:
            print(f"  {r['count']} commit(s) newer than your pin "
                  f"({len(r['relevant'])} touching files this pipeline uses, "
                  f"{r['other']} unrelated)")
        else:
            print(f"  {r['count']} commit(s) in the last {since_days} days "
                  f"({len(r['relevant'])} touching files this pipeline uses, "
                  f"{r['other']} unrelated)")

        for c in r["relevant"]:
            worst = "HIGH" if any(s == "HIGH" for s, _ in c["hits"]) else "MED"
            print(f"    [{worst}] {c['short']} {c['date']}  {c['subject']}")
            for _, path in c["hits"]:
                print(f"           -> {path}")

        print(f"  verdict: {r['risk']}")
        if r["risk"] in (RISK_HIGH, RISK_ACTIVE_HIGH):
            print("           10_enable_susfs_for_ksu.patch changed - this is the "
                  "file that patches\n           the SukiSU-Ultra tree itself and "
                  "the usual cause of a sudden build break.")
        if r["risk"] in (RISK_TEST, RISK_HIGH):
            print("           Build once WITHOUT --susfs-commit for this branch "
                  "before moving the pin.\n           If it succeeds with no "
                  ".rej files, the two projects are back in sync.")
        elif r["risk"] in (RISK_ACTIVE, RISK_ACTIVE_HIGH):
            print("           This branch is unpinned, so your next build will "
                  "pick these up\n           automatically and may not match your "
                  "last one. Pin with --susfs-commit\n           if you want that "
                  "to be your decision rather than upstream's.")
        if r["version_pin"] and r["version_pin"] != r["version_head"]:
            print(f"           SUSFS version bumped {r['version_pin']} -> "
                  f"{r['version_head']} since your pin.")


def write_github_summary(results: list, pinned_mode: bool, since_days: int):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## susfs4ksu pin check", ""]
    lines.append("| Branch | Your pin | Branch HEAD | New | Relevant | Verdict |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        if r["error"]:
            lines.append(f"| `{r['branch']}` | - | - | - | - | ERROR |")
            continue
        pin = r["pinned_at"].split()[0] if r["pinned_at"] else "(unpinned)"
        head = r["head"].split()[0]
        lines.append(
            f"| `{r['branch']}` | `{pin}` | `{head}` | {r['count']} | "
            f"{len(r['relevant'])} | {r['risk']} |"
        )
    lines.append("")
    for r in results:
        if r["error"]:
            lines.append(f"**`{r['branch']}`** — {r['error']}")
            lines.append("")
            continue
        if not r["relevant"]:
            continue
        lines.append(f"**`{r['branch']}`** — commits touching files this pipeline uses:")
        lines.append("")
        for c in r["relevant"]:
            worst = "HIGH" if any(s == "HIGH" for s, _ in c["hits"]) else "MED"
            paths = ", ".join(f"`{p}`" for _, p in c["hits"])
            lines.append(f"- **{worst}** `{c['short']}` {c['date']} — {c['subject']} ({paths})")
        lines.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report what changed in susfs4ksu since your --susfs-commit pin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pin", default="",
                        help="Same syntax as build.py's --susfs-commit: a bare ref, "
                             "or 'branch=ref,branch=ref'. Omit to just report recent "
                             "activity instead of comparing against a pin.")
    parser.add_argument("--branch", action="append", default=[],
                        help="susfs4ksu branch to check (repeatable). Defaults to the "
                             "branches used by enabled matrix.json entries.")
    parser.add_argument("--since-days", type=int, default=14,
                        help="Lookback window for branches with no pin (default: 14)")
    parser.add_argument("--workspace", default=os.environ.get(
        "GKI_WORKSPACE", str(Path.home() / "gki-workspace")),
        help="Build workspace - an existing susfs4ksu clone there is reused")
    parser.add_argument("--repo", default=None,
                        help="Path to a susfs4ksu checkout to use directly")
    parser.add_argument("--github-summary", action="store_true",
                        help="Also append a Markdown table to $GITHUB_STEP_SUMMARY")
    parser.add_argument("--fail-on-risk", action="store_true",
                        help="Exit non-zero if any branch needs testing before its "
                             "pin can move (for scheduled CI runs)")
    args = parser.parse_args()

    try:
        pins, bare = parse_pins(args.pin)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    branches = args.branch or enabled_branches(
        Path(__file__).resolve().parent.parent / "config" / "matrix.json"
    )
    if not branches:
        print("No branches to check - pass --branch, or enable entries in matrix.json.",
              file=sys.stderr)
        return 1

    repo, cleanup = find_or_clone_repo(Path(args.workspace).expanduser(),
                                       Path(args.repo).expanduser() if args.repo else None)
    try:
        results = []
        for branch in branches:
            pin = pins.get(branch) if pins else bare
            results.append(analyse_branch(repo, branch, pin, args.since_days))

        pinned_mode = bool(pins or bare)
        print(f"susfs4ksu: {SUSFS_REPO_URL}")
        print(f"checkout used: {repo}")
        print_report(results, pinned_mode, args.since_days)
        print()

        if args.github_summary:
            write_github_summary(results, pinned_mode, args.since_days)

        if any(r["error"] for r in results):
            return 1
        # Only pinned branches can "need testing" - an unpinned branch is
        # already tracking HEAD by choice, so flagging it as a failure
        # every week would just train people to ignore the run.
        if args.fail_on_risk and any(r["risk"] in (RISK_TEST, RISK_HIGH) for r in results):
            return 2
        return 0
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
