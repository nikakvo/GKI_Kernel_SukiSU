#!/usr/bin/env bash
# check-release.sh - is there something new to build, and exactly how?
#
# Tracks SukiSU-Ultra main HEAD + susfs4ksu branch HEAD. Without building
# anything it:
#   1. takes SukiSU main HEAD (never a tag: the manager everyone installs is
#      the CI build of main HEAD, and a tag is usually followed by a version
#      bump commit, so building the tag gives a kernel one version code
#      behind that manager) and computes its version code the same way the
#      kernel Kbuild and the manager do,
#   2. finds the newest susfs commit for the branch,
#   3. applies 10_enable_susfs_for_ksu.patch FOR REAL in a throwaway worktree
#      and runs the pipeline's own recovery (_recover_susfs_init_c(), which
#      also calls the selinux_hide.c / ksud_integration.c / dispatch.c
#      recoveries) plus the post-patch fixups on it - so the
#      verdict comes from the same code the build will run, not from counting
#      hunks. If HEAD's KernelSU patch no longer fits SukiSU (upstream synced
#      it to tiann/KernelSU), it tries the older versions of THAT ONE FILE and
#      suggests a split pin "<HEAD>+<older>" - kernel-side fixes from HEAD,
#      KernelSU wiring from the newest version that still fits - but only when
#      the commits being skipped touched nothing except that patch file.
#      It only does this when that patch actually changed since the last
#      good build - if it didn't, the drift is on the SukiSU side and older
#      patches can't help,
#   3b. on STOP, prints a drift report: for every file with a rejected hunk,
#      which SukiSU commits changed it since the last good build (or that
#      SukiSU didn't touch it, so the susfs patch is what moved),
#   4. prints the exact ./build-kernel.sh command to run,
#   5. remembers that suggestion, and on the NEXT run asks whether you built,
#      flashed and tested it - answer 1 and it updates the known-good pins
#      in config.py by itself.
#   6. (android13-5.15) takes amneziawg-linux-kernel-module master HEAD and
#      runs the build's own prepare_amneziawg_src() on it. If it fits, the
#      commands get --amneziawg-commit <HEAD> and answer 1 saves it too; if
#      not, the build keeps the known-good AmneziaWG pin - a broken AmneziaWG
#      upstream never blocks a kernel. (No compile here: the build proves it.)
#
# The known-good pins live in ONE place:
#   .github/workflows/scripts/config.py
#     DEFAULT_KSU_REF, DEFAULT_KSU_VERSION_CODE, DEFAULT_SUSFS_PINS,
#     DEFAULT_AMNEZIAWG_REF
# They are what every unpinned build uses (build-kernel.sh, build.py, both
# Actions workflows), so saving here also moves the defaults for forks/CI.
#
# Read-only for the build workspace. Clones into a cache dir.

set -uo pipefail

BRANCH="gki-android13-5.15"

SUKI_URL="https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"
SUSFS_URL="https://github.com/ShirkNeko/susfs4ksu.git"
AWG_URL="https://github.com/amnezia-vpn/amneziawg-linux-kernel-module.git"
CACHE="${GKI_CHECK_CACHE:-$HOME/.cache/gki-check}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILDER_DIR="$SCRIPT_DIR/.github/workflows/scripts"
CONFIG_PY="$BUILDER_DIR/config.py"
ASK=1

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --cache)  CACHE="$2"; shift 2 ;;
        --no-ask) ASK=0; shift ;;
        -h|--help)
            echo "Usage: $0 [--branch gki-android13-5.15|gki-android14-6.1|gki-android15-6.6] [--cache DIR] [--no-ask]"
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [ -t 1 ]; then
    R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; C=$'\e[36m'; N=$'\e[0m'
else
    R=""; G=""; Y=""; B=""; C=""; N=""
fi
ok()   { echo "  ${G}OK${N}    $*"; }
warn() { echo "  ${Y}WARN${N}  $*"; }
bad()  { echo "  ${R}STOP${N}  $*"; }
# a combination that was tried and doesn't fit - not the final verdict
tried() { echo "  ${Y}NO FIT${N} $*"; }
hdr()  { echo; echo "${B}=== $* ===${N}"; }

for c in git patch python3; do
    command -v "$c" >/dev/null || { echo "$c not found" >&2; exit 1; }
done
[ -f "$BUILDER_DIR/kernel_builder.py" ] || {
    echo "kernel_builder.py not found at $BUILDER_DIR" >&2
    echo "Run this script from the repo root (next to build-kernel.sh)." >&2
    exit 1; }

# ---- known-good pins, read from config.py ------------------------------------
# ast, not import: importing config.py does a network fetch at import time.
read_pins() {
    local out
    out="$(python3 - "$CONFIG_PY" <<'PY'
import ast, sys
vals = {}
for n in ast.parse(open(sys.argv[1]).read()).body:
    if isinstance(n, ast.Assign) and len(n.targets) == 1:
        name = getattr(n.targets[0], "id", None)
        if name in ("DEFAULT_KSU_REF", "DEFAULT_KSU_VERSION_CODE", "DEFAULT_SUSFS_PINS",
                    "DEFAULT_AMNEZIAWG_REF"):
            vals[name] = ast.literal_eval(n.value)
print(f"PINNED_KSU_REF={vals.get('DEFAULT_KSU_REF', '')}")
print(f"PINNED_KSU_VERSION_CODE={vals.get('DEFAULT_KSU_VERSION_CODE', '')}")
print(f"PINNED_AWG_REF={vals.get('DEFAULT_AMNEZIAWG_REF', '')}")
for b, r in vals.get("DEFAULT_SUSFS_PINS", {}).items():
    print(f"SUSFS {b} {r}")
PY
)" || return 1
    PINNED_KSU_REF="$(printf '%s\n' "$out" | sed -n 's/^PINNED_KSU_REF=//p')"
    PINNED_KSU_VERSION_CODE="$(printf '%s\n' "$out" | sed -n 's/^PINNED_KSU_VERSION_CODE=//p')"
    PINNED_AWG_REF="$(printf '%s\n' "$out" | sed -n 's/^PINNED_AWG_REF=//p')"
    declare -gA PINNED_SUSFS=()
    while read -r _ b r; do PINNED_SUSFS[$b]="$r"; done \
        < <(printf '%s\n' "$out" | grep '^SUSFS ')
    [ -n "$PINNED_KSU_REF" ]
}
read_pins || { echo "Could not read DEFAULT_KSU_REF & co. from $CONFIG_PY" >&2; exit 1; }

mkdir -p "$CACHE"
SUKI="$CACHE/SukiSU-Ultra"
SUSFS="$CACHE/susfs4ksu"

sync_repo() {   # $1=url $2=dir $3=label
    if [ -d "$2/.git" ] || [ -f "$2/HEAD" ]; then
        echo "Updating $3..."
        git -C "$2" fetch -q --tags --prune origin || {
            echo "  fetch failed - using cached copy" >&2; return 0; }
    else
        echo "Cloning $3..."
        git clone -q --filter=blob:none --no-checkout "$1" "$2" || return 1
    fi
}
sync_repo "$SUKI_URL"  "$SUKI"  "SukiSU-Ultra" || exit 1
sync_repo "$SUSFS_URL" "$SUSFS" "susfs4ksu"    || exit 1
# KPM image patcher (informational only, never stops the run)
SKPATCH_URL="https://github.com/ShirkNeko/SukiSU_patch.git"
SKPATCH="$CACHE/SukiSU_patch"
sync_repo "$SKPATCH_URL" "$SKPATCH" "SukiSU_patch (KPM patcher)" || SKPATCH=""
# AmneziaWG (never stops the run: on any trouble the known-good pin is used)
AWGREPO="$CACHE/amneziawg-linux-kernel-module"
if [ "$BRANCH" = "gki-android13-5.15" ]; then
    sync_repo "$AWG_URL" "$AWGREPO" "amneziawg-linux-kernel-module" || AWGREPO=""
else
    AWGREPO=""
fi

sha_of()   { git -C "$1" rev-parse -q --verify "$2^{commit}" 2>/dev/null; }

# A susfs pin is either "<sha>" or the split form "<kernel-side>+<KernelSU-patch>"
# (kernel_builder.py: _split_susfs_ref). These keep both forms comparable.
susfs_k()     { printf '%s' "${1%%+*}"; }                           # kernel-side part
susfs_p()     { case "$1" in *+*) printf '%s' "${1#*+}" ;; esac; }  # patch part or ""
norm_susfs() {  # both parts as full SHAs
    local k p
    [ -n "$1" ] || return 0
    k="$(sha_of "$SUSFS" "$(susfs_k "$1")")"; p="$(susfs_p "$1")"
    if [ -n "$p" ]; then printf '%s+%s' "$k" "$(sha_of "$SUSFS" "$p")"; else printf '%s' "$k"; fi
}
short_susfs() { local p; p="$(susfs_p "$1")"; printf '%s' "${1:0:8}${p:+"+${p:0:8}"}"; }

# ---- 0. follow-up on the build suggested last time ---------------------------
# When the script prints "Build this:" it saves that exact combination here.
# Next run it asks about THAT combination - not whatever upstream has moved to
# in the meantime - so answering "1" can never save something you didn't build.
PENDING="$CACHE/pending-$BRANCH"
BROKEN="$CACHE/broken-$BRANCH"
touch "$BROKEN"
# AmneziaWG commits marked broken (answer 3), one full SHA per line.
AWG_BROKEN="$CACHE/broken-amneziawg"
touch "$AWG_BROKEN"

save_pins() {   # $1=ksu sha  $2=susfs sha  $3=version code  [$4=amneziawg sha] -> edits config.py
    # temp file in the same dir so the final mv is an atomic rename
    local tmp; tmp="$(mktemp "$BUILDER_DIR/.config.py.XXXXXX")" || return 1
    python3 - "$CONFIG_PY" "$tmp" "$BRANCH" "$1" "$2" "$3" "${4:-}" <<'PY' || { rm -f "$tmp"; return 1; }
import ast, re, sys
src_path, dst, branch, ksu, susfs, code, awg = sys.argv[1:]
s = open(src_path).read()

def sub1(pattern, repl, text):
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"pattern not found: {pattern}")
    return new

if branch == "gki-android13-5.15":
    # the phone build moves the SukiSU pin + version code too
    s = sub1(r'^DEFAULT_KSU_REF = "[^"]*"', f'DEFAULT_KSU_REF = "{ksu}"', s)
    s = sub1(r'^DEFAULT_KSU_VERSION_CODE = \d+', f'DEFAULT_KSU_VERSION_CODE = {code}', s)
if awg:
    if not re.fullmatch(r"[0-9a-f]{40}", awg):
        sys.exit(f"not a full AmneziaWG commit SHA: {awg}")
    s = sub1(r'^DEFAULT_AMNEZIAWG_REF = "[^"]*"', f'DEFAULT_AMNEZIAWG_REF = "{awg}"', s)

line = re.compile(r'^(\s*)"' + re.escape(branch) + r'": "[0-9a-f+]*",', re.M)
if line.search(s):
    s = line.sub(lambda m: f'{m.group(1)}"{branch}": "{susfs}",', s, count=1)
else:   # branch not pinned yet: add it before the closing brace of the dict
    s = sub1(r'^(DEFAULT_SUSFS_PINS = \{\n(?:.*\n)*?)(\})',
             lambda m: f'{m.group(1)}    "{branch}": "{susfs}",\n{m.group(2)}', s)

# must still parse and hold exactly what we meant to write
vals = {}
for n in ast.parse(s).body:
    if isinstance(n, ast.Assign) and len(n.targets) == 1:
        vals[getattr(n.targets[0], "id", None)] = n.value
pins = ast.literal_eval(vals["DEFAULT_SUSFS_PINS"])
assert pins.get(branch) == susfs
if branch == "gki-android13-5.15":
    assert ast.literal_eval(vals["DEFAULT_KSU_REF"]) == ksu
    assert ast.literal_eval(vals["DEFAULT_KSU_VERSION_CODE"]) == int(code)
if awg:
    assert ast.literal_eval(vals["DEFAULT_AMNEZIAWG_REF"]) == awg
open(dst, "w").write(s)
PY
    chmod --reference="$CONFIG_PY" "$tmp" 2>/dev/null
    mv -f "$tmp" "$CONFIG_PY"
}

if [ -s "$PENDING" ]; then
    # 5th field (AmneziaWG sha) is empty in files written by older versions
    read -r P_KSU P_SUSFS P_CODE P_DATE P_AWG < "$PENDING"
    PIN_NOW="$(git -C "$SUKI" rev-parse -q --verify "$PINNED_KSU_REF^{commit}" 2>/dev/null)"
    P_KSU_SAVED=0
    [ "$P_KSU" = "$PIN_NOW" ] && [ "$P_SUSFS" = "$(norm_susfs "${PINNED_SUSFS[$BRANCH]:-}")" ] && P_KSU_SAVED=1
    if [ "$P_KSU_SAVED" = 1 ] && { [ -z "${P_AWG:-}" ] || [ "$P_AWG" = "$PINNED_AWG_REF" ]; }; then
        rm -f "$PENDING"          # already saved (e.g. by hand)
    elif [ "$ASK" = 1 ] && [ -t 0 ]; then
        hdr "Last suggested build ($P_DATE)"
        echo "  SukiSU  ${P_KSU:0:8}   version $P_CODE"
        echo "  susfs   $(short_susfs "$P_SUSFS")   ($BRANCH)"
        [ -n "${P_AWG:-}" ] && echo "  AmneziaWG ${P_AWG:0:8}"
        echo
        echo "  Did you build, flash and test it?"
        echo "    1) Yes, it works  - save it as the new known-good build"
        echo "    2) Not yet        - ask me again next time"
        echo "    3) It's broken    - never suggest this combination again"
        read -rp "  Choice [1/2/3, Enter = 2]: " ans
        case "$ans" in
            1)
                if save_pins "$P_KSU" "$P_SUSFS" "$P_CODE" "${P_AWG:-}"; then
                    rm -f "$PENDING"
                    [ "$BRANCH" = "gki-android13-5.15" ] && {
                        PINNED_KSU_REF="$P_KSU"; PINNED_KSU_VERSION_CODE="$P_CODE"; }
                    PINNED_SUSFS[$BRANCH]="$P_SUSFS"
                    [ -n "${P_AWG:-}" ] && PINNED_AWG_REF="$P_AWG"
                    ok "Saved as the known-good build in config.py."
                    echo "        Upload .github/workflows/scripts/config.py to GitHub too -"
                    echo "        that also moves the default pins for CI and forks."
                else
                    bad "Could not update config.py - pins unchanged. Edit them by hand."
                fi ;;
            3)
                # Mark only what was new in that build: a new AmneziaWG on
                # top of the known-good SukiSU/susfs must not block those.
                if [ "$P_KSU_SAVED" = 0 ]; then
                    echo "$P_KSU $P_SUSFS $P_DATE" >> "$BROKEN"
                    warn "SukiSU ${P_KSU:0:8} + susfs $(short_susfs "$P_SUSFS") marked as broken (list: $BROKEN)."
                fi
                if [ -n "${P_AWG:-}" ] && [ "$P_AWG" != "$PINNED_AWG_REF" ]; then
                    echo "$P_AWG" >> "$AWG_BROKEN"
                    warn "AmneziaWG ${P_AWG:0:8} marked as broken (list: $AWG_BROKEN)."
                fi
                rm -f "$PENDING"
                echo "        It won't be suggested again." ;;
            *)
                echo "  OK, asking again next time." ;;
        esac
    fi
fi

uapi_of() {     # $1=ref -> number or nothing
    git -C "$SUKI" show "$1:uapi/supercall.h" 2>/dev/null \
        | grep -m1 -oE "KERNEL_SU_UAPI_VERSION = [0-9]+" | grep -oE "[0-9]+$"
}
version_code_of() {   # $1=sha -> same formula as kernel/Kbuild and the manager
    local kb base off count
    kb="$(git -C "$SUKI" show "$1:kernel/Kbuild" 2>/dev/null)"
    base="$(printf '%s\n' "$kb" | grep -m1 -E '^VERSION_BASE[[:space:]]*:=' | grep -oE '[0-9]+$')"
    off="$(printf '%s\n'  "$kb" | grep -m1 -E '^VERSION_OFFSET[[:space:]]*:=' | grep -oE '[0-9]+$')"
    count="$(git -C "$SUKI" rev-list --count "$1" 2>/dev/null)"
    [ -n "$base" ] && [ -n "$off" ] && [ -n "$count" ] || return 1
    echo $(( base + count - off ))
}

# ---- 1. which SukiSU to build ------------------------------------------------
hdr "SukiSU-Ultra"

PIN_SHA="$(sha_of "$SUKI" "$PINNED_KSU_REF")"
MAIN_SHA="$(sha_of "$SUKI" origin/main)"
MAIN_UAPI="$(uapi_of origin/main)"; : "${MAIN_UAPI:=?}"

# Always main HEAD: the manager CI builds (and the release APK) come from main,
# and SukiSU tags a release BEFORE bumping VERSION_TAG in kernel/Kbuild, so the
# tag itself is one version code behind the manager people install. The newest
# tag is only shown for reference (handy for the release notes).
CAND_SHA="$MAIN_SHA"; CAND_LABEL="main"
NEWEST_TAG="$(git -C "$SUKI" tag --sort=-creatordate | head -n1)"
if [ -n "$NEWEST_TAG" ]; then
    TAG_SHA="$(sha_of "$SUKI" "$NEWEST_TAG")"
    if [ "$TAG_SHA" = "$MAIN_SHA" ]; then
        echo "  Newest tag: ${B}$NEWEST_TAG${N} - main HEAD is the tag itself (UAPI $MAIN_UAPI)."
    elif git -C "$SUKI" merge-base --is-ancestor "$TAG_SHA" "$MAIN_SHA" 2>/dev/null; then
        echo "  Newest tag: ${B}$NEWEST_TAG${N} ($(version_code_of "$TAG_SHA" || echo '?')) - main is" \
             "$(git -C "$SUKI" rev-list --count "$TAG_SHA..$MAIN_SHA") commit(s) ahead, building main (UAPI $MAIN_UAPI)."
    else
        echo "  Newest tag: $NEWEST_TAG (not on main) - building main (UAPI $MAIN_UAPI)."
    fi
fi
CAND_CODE="$(version_code_of "$CAND_SHA")" || CAND_CODE="?"

printf "  %-10s %-10s %-12s %s\n" "" "SHA" "DATE" "VERSION"
printf "  %-10s %-10s %-12s %s\n" "built" "${PIN_SHA:0:8}" \
    "$(git -C "$SUKI" log -1 --format=%cs "$PIN_SHA" 2>/dev/null)" "$PINNED_KSU_VERSION_CODE"
printf "  %-10s %-10s %-12s %s\n" "newest" "${CAND_SHA:0:8}" \
    "$(git -C "$SUKI" log -1 --format=%cs "$CAND_SHA")" "$CAND_CODE"

KSU_NEW=0
if [ "$CAND_SHA" != "$PIN_SHA" ]; then
    KSU_NEW=1
    n_all="$(git -C "$SUKI" rev-list --count "$PIN_SHA..$CAND_SHA" 2>/dev/null || echo '?')"
    kernel_log="$(git -C "$SUKI" log --oneline --no-decorate "$PIN_SHA..$CAND_SHA" -- kernel uapi 2>/dev/null)"
    n_kernel="$(printf '%s' "$kernel_log" | grep -c . || true)"
    echo
    echo "  $n_all new commit(s) since the last build, ${B}$n_kernel${N} of them touch kernel/ or uapi/:"
    if [ -n "$kernel_log" ]; then
        printf '%s\n' "$kernel_log" | head -n 15 | sed 's/^/      /'
    else
        echo "      (none - manager/CI changes only)"
    fi
fi

# ---- 2. susfs ----------------------------------------------------------------
hdr "susfs4ksu ($BRANCH)"
SUSFS_PIN="${PINNED_SUSFS[$BRANCH]:-}"
SUSFS_PIN_FULL="$(norm_susfs "$SUSFS_PIN")"
SUSFS_PIN_K="$(susfs_k "$SUSFS_PIN_FULL")"
SUSFS_HEAD="$(sha_of "$SUSFS" "origin/$BRANCH")"
[ -n "$SUSFS_HEAD" ] || { bad "branch $BRANCH does not exist on $SUSFS_URL"; exit 1; }
if [ -z "$SUSFS_PIN" ]; then
    echo "  HEAD   ${SUSFS_HEAD:0:8}  (no known-good pin for this branch in config.py yet)"
elif [ "$SUSFS_HEAD" != "$SUSFS_PIN_K" ]; then
    echo "  built  $(short_susfs "$SUSFS_PIN_FULL")"
    echo "  HEAD   ${SUSFS_HEAD:0:8}  ($(git -C "$SUSFS" rev-list --count "$SUSFS_PIN_K..$SUSFS_HEAD" 2>/dev/null || echo '?') new):"
    git -C "$SUSFS" log --oneline --no-decorate "$SUSFS_PIN_K..$SUSFS_HEAD" 2>/dev/null \
        | head -n 15 | sed 's/^/      /'
else
    echo "  HEAD   ${SUSFS_HEAD:0:8}  (same as last build)"
fi

# susfs_same_code <old> <new>: true when everything under kernel_patches/ is
# the same code - only .patch files differ, and only in line numbers, index
# lines and unchanged context lines (a "rebase onto 5.15.X" commit). Such a
# commit adds nothing to the kernel, but its new context can stop fitting our
# sub_level (054b2432 "rebase onto 5.15.211" failed on fs/namespace.c on 220).
susfs_same_code() {
    local old="$1" new="$2" f
    while read -r f; do
        [ -n "$f" ] || continue
        case "$f" in *.patch) ;; *) return 1 ;; esac
        python3 - "$SUSFS" "$old" "$new" "$f" <<'PY' || return 1
import subprocess, sys
repo, old, new, path = sys.argv[1:]
def body(ref):
    r = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    # only the lines the patch adds/removes, per file - not context/@@/index
    out, cur = [], None
    for l in r.stdout.splitlines():
        if l.startswith("+++ ") or l.startswith("--- "):
            if l.startswith("+++ "):
                cur = l[4:].split("\t")[0]
            continue
        if l.startswith(("+", "-")):
            out.append((cur, l))
    return out
a, b = body(old), body(new)
sys.exit(0 if a is not None and a == b else 1)
PY
    done < <(git -C "$SUSFS" diff --name-only "$old" "$new" -- kernel_patches/ 2>/dev/null)
    return 0
}

# Walk back from HEAD through commits that are only re-bases and take the
# oldest one with HEAD's code: the pin itself if nothing real changed, or
# e.g. 2ad29735 instead of 054b2432 when only a re-base came after it.
if [ -n "$SUSFS_PIN" ] && [ "$SUSFS_HEAD" != "$SUSFS_PIN_K" ]; then
    SUSFS_CODE_OF="$SUSFS_HEAD"
    while read -r c; do
        [ -n "$c" ] || continue
        p="$(sha_of "$SUSFS" "$c^")"
        [ -n "$p" ] && susfs_same_code "$p" "$SUSFS_CODE_OF" || break
        SUSFS_HEAD="$p"
    done < <(git -C "$SUSFS" rev-list --first-parent "$SUSFS_PIN_K..$SUSFS_CODE_OF" 2>/dev/null)
    if [ "$SUSFS_HEAD" != "$SUSFS_CODE_OF" ]; then
        echo
        ok "susfs ${SUSFS_CODE_OF:0:8} only re-bases its patches (line numbers / context)."
        if [ "$SUSFS_HEAD" = "$SUSFS_PIN_K" ]; then
            echo "        No new code - keeping the known-good susfs ${SUSFS_HEAD:0:8}."
        else
            echo "        Using ${SUSFS_HEAD:0:8} instead: same code as HEAD, older patch context."
        fi
        echo "        A re-based patch can stop fitting our kernel sub_level."
    fi
fi

# ---- 2b. KPM patcher (SukiSU_patch kpm/) - information only -----------------
# The build uses ONE pinned version of SukiSU_patch's kpm/patch_linux
# (KPM_PATCH_REF in kernel_builder.py). This only tells you when upstream has
# published something newer, so it can be boot-tested. It never changes the
# verdict below and never changes the pin.
# Patcher versions known to make a kernel that doesn't boot on our phone:
KPM_PATCH_BROKEN="7c6289c"   # 2026-10-18 "更新KPM": kpimg version 0, bootloop at POCO logo
kpm_patcher_report() {
    local pin pin_full head newer c short known
    pin="$(sed -n 's/^KPM_PATCH_REF = "\([0-9a-f]*\)".*/\1/p' "$BUILDER_DIR/kernel_builder.py" 2>/dev/null | head -n 1)"
    hdr "KPM patcher (SukiSU_patch kpm/)"
    if [ -z "$SKPATCH" ] || [ -z "$pin" ]; then
        warn "Could not check (no SukiSU_patch copy or no KPM_PATCH_REF in kernel_builder.py)."
        return 0
    fi
    pin_full="$(sha_of "$SKPATCH" "$pin")"
    head="$(git -C "$SKPATCH" log -1 --format=%H origin/main -- kpm/ 2>/dev/null)"
    if [ -z "$pin_full" ] || [ -z "$head" ]; then
        warn "Could not find the pinned patcher ${pin:0:8} or kpm/ history in SukiSU_patch."
        return 0
    fi
    echo "  pinned  $(git -C "$SKPATCH" log -1 --date=short --format='%h   %ad   %s' "$pin_full")"
    if [ "$head" = "$pin_full" ]; then
        ok "No newer KPM patcher upstream."
        return 0
    fi
    newer=0
    while read -r c; do
        [ -n "$c" ] || continue
        short="${c:0:7}"; known=""
        case " $KPM_PATCH_BROKEN " in *" $short "*) known=1 ;; esac
        if [ -n "$known" ]; then
            echo "  newer   $(git -C "$SKPATCH" log -1 --date=short --format='%h   %ad   %s' "$c")   ${D:-}(known broken - bootloop)${N}"
        else
            echo "  ${B}NEW${N}     $(git -C "$SKPATCH" log -1 --date=short --format='%h   %ad   %s' "$c")"
            newer=$((newer + 1))
        fi
    done < <(git -C "$SKPATCH" log --format=%H "$pin_full..origin/main" -- kpm/ 2>/dev/null)
    if [ "$newer" -gt 0 ]; then
        warn "A new KPM patcher was published. The build still uses ${pin:0:7}."
        echo "        Send this output for a look - it needs a test build and"
        echo "        'fastboot boot boot.img' before it can replace the pin."
        echo "        https://github.com/ShirkNeko/SukiSU_patch/commits/main/kpm"
    else
        ok "Only known-broken patcher versions are newer - staying on ${pin:0:7}."
    fi
}
kpm_patcher_report

# ---- 2c. AmneziaWG (android13-5.15 only) ------------------------------------
# Probes master HEAD with the build's own prepare_amneziawg_src() (copy,
# Android fixups, wg_ -> awg_ rename) - no compile. FIT: the build commands
# get --amneziawg-commit <HEAD>. NO FIT: they don't, so the build uses the
# known-good DEFAULT_AMNEZIAWG_REF and AmneziaWG still ships. Never a STOP.
AWG_USE="$PINNED_AWG_REF"; AWG_NEW=0; AWG_VER=""
awg_version_of() {   # $1=ref -> AmneziaWG version from src/version.h
    git -C "$AWGREPO" show "$1:src/version.h" 2>/dev/null \
        | sed -n 's/^#define[[:space:]]*WIREGUARD_VERSION[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}
amneziawg_report() {
    local head pin_full tmp out
    [ "$BRANCH" = "gki-android13-5.15" ] || return 0
    hdr "AmneziaWG (amneziawg-linux-kernel-module)"
    if [ -z "$PINNED_AWG_REF" ]; then
        warn "No DEFAULT_AMNEZIAWG_REF in config.py - nothing to compare against."
        return 0
    fi
    if [ -z "$AWGREPO" ]; then
        warn "Could not reach the repo - the build uses the known-good ${PINNED_AWG_REF:0:8}."
        return 0
    fi
    head="$(sha_of "$AWGREPO" origin/master)"
    pin_full="$(sha_of "$AWGREPO" "$PINNED_AWG_REF")"
    if [ -z "$head" ]; then
        warn "No master branch upstream - the build uses the known-good ${PINNED_AWG_REF:0:8}."
        return 0
    fi
    echo "  built  ${PINNED_AWG_REF:0:8}  $(awg_version_of "$PINNED_AWG_REF")"
    if [ "$head" = "$pin_full" ]; then
        ok "Upstream master is the known-good commit - up to date."
        return 0
    fi
    if [ -n "$pin_full" ] && git -C "$AWGREPO" merge-base --is-ancestor "$pin_full" "$head" 2>/dev/null; then
        echo "  HEAD   ${head:0:8}  $(awg_version_of "$head")  ($(git -C "$AWGREPO" rev-list --count "$pin_full..$head") new):"
        git -C "$AWGREPO" log --oneline --no-decorate "$pin_full..$head" 2>/dev/null \
            | head -n 15 | sed 's/^/      /'
    else
        echo "  HEAD   ${head:0:8}  $(awg_version_of "$head")  (the known-good commit is not in master's history)"
    fi
    if grep -qxF "$head" "$AWG_BROKEN" 2>/dev/null; then
        tried "You marked AmneziaWG ${head:0:8} as broken earlier."
        echo "        Keeping the known-good ${PINNED_AWG_REF:0:8}. To allow it again, delete its line in:"
        echo "        $AWG_BROKEN"
        return 0
    fi
    tmp="$(mktemp -d)"
    if ! git -C "$AWGREPO" archive "$head" src 2>/dev/null | tar -x -C "$tmp" 2>/dev/null \
       || [ ! -d "$tmp/src" ]; then
        rm -rf "$tmp"
        warn "Could not read src/ of ${head:0:8} - keeping the known-good ${PINNED_AWG_REF:0:8}."
        return 0
    fi
    out="$(cd "$BUILDER_DIR" && python3 - "$tmp/src" "$tmp/out" 2>/dev/null <<'AWGPY'
import sys, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, '.')
import kernel_builder as kb
try:
    print("AWG_FIT=" + kb.prepare_amneziawg_src(sys.argv[1], sys.argv[2]))
except kb.AmneziaWGPrepareError as e:
    print("AWG_NOFIT=" + str(e))
AWGPY
)"
    rm -rf "$tmp"
    if printf '%s\n' "$out" | grep -q '^AWG_FIT='; then
        AWG_VER="$(printf '%s\n' "$out" | sed -n 's/^AWG_FIT=//p' | head -n 1)"
        AWG_USE="$head"; AWG_NEW=1
        ok "AmneziaWG ${head:0:8} ($AWG_VER) fits: Android fixups and the wg_ -> awg_ rename apply."
        echo "        Not compiled here - the build is the real test (no warnings in"
        echo "        drivers/net/amneziawg, amneziawg -> applied in PATCH_STATUS.json)."
    elif printf '%s\n' "$out" | grep -q '^AWG_NOFIT='; then
        tried "AmneziaWG ${head:0:8}: $(printf '%s\n' "$out" | sed -n 's/^AWG_NOFIT=//p' | head -n 1)"
        echo "        The build keeps the known-good ${PINNED_AWG_REF:0:8} - AmneziaWG still ships."
        echo "        Send this output for a look (the fixups need an update)."
    else
        warn "Could not run kernel_builder.py (python error) - keeping the known-good ${PINNED_AWG_REF:0:8}."
    fi
}
amneziawg_report

# ---- 3. the real test: apply + run the pipeline's own recovery ---------------
KSU_PATCH_REL="kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch"

WORKTREE=""
PATCHFILE="$(mktemp)"
PATCHOUT="$(mktemp)"
drop_worktree() {
    if [ -n "$WORKTREE" ]; then
        git -C "$SUKI" worktree remove --force "$WORKTREE" >/dev/null 2>&1
        rm -rf "$WORKTREE"
        WORKTREE=""
    fi
}
cleanup() { drop_worktree; rm -f "$PATCHFILE" "$PATCHOUT"; }
trap cleanup EXIT

# patch_test <susfs ref to take the KernelSU patch from>  -> sets VERDICT, DETAIL
patch_test() {
    local ref="$1" out rc failed_files n_failed rec left rej
    VERDICT="STOP"; DETAIL=""; LAST_FAILED=""
    drop_worktree
    if ! git -C "$SUSFS" show "$ref:$KSU_PATCH_REL" > "$PATCHFILE" 2>/dev/null; then
        DETAIL="10_enable_susfs_for_ksu.patch not found in susfs ${ref:0:8}"; return
    fi
    WORKTREE="$(mktemp -d)"; rm -rf "$WORKTREE"
    if ! git -C "$SUKI" worktree add -q --detach "$WORKTREE" "$CAND_SHA" >/dev/null 2>&1; then
        DETAIL="cannot check out SukiSU ${CAND_SHA:0:8}"; return
    fi
    out="$(cd "$WORKTREE" && patch -p1 --fuzz=3 < "$PATCHFILE" 2>&1)"; rc=$?
    printf '%s\n' "$out" > "$PATCHOUT"
    failed_files="$(printf '%s\n' "$out" \
        | awk '/^patching file /{f=$3} /FAILED/{if(f!=""){print f; f=""}}' \
        | sort -u | tr '\n' ' ')"
    n_failed="$(printf '%s\n' "$out" | grep -cE '^Hunk.*FAILED' || true)"
    LAST_FAILED="$failed_files"
    if [ "$rc" -ne 0 ]; then
        echo "  $n_failed hunk(s) failed in: ${failed_files:-?}"
    fi
    # Same code the build runs: the drift recovery (only when hunks failed),
    # then _disable_susfs_preinit() + _fixup_susfs_sucompat_util_h() +
    # _fixup_susfs_pkg_observer_c() + _disable_susfs_mount_hide() on every
    # SUSFS build.
    rec="$(cd "$BUILDER_DIR" && python3 - "$WORKTREE" "$rc" 2>/dev/null <<'PY'
import sys, logging
# Only the recovery's own ERROR lines (why it refused) are passed through,
# as REASON= markers; everything else kernel_builder logs stays quiet.
class _Reason(logging.Handler):
    def emit(self, record):
        print("REASON=" + record.getMessage())
logging.basicConfig(level=logging.ERROR, handlers=[_Reason()])
sys.path.insert(0, '.')
from pathlib import Path
import kernel_builder as kb
b = object.__new__(kb.KernelBuilder)
tree = Path(sys.argv[1])
ok = True if sys.argv[2] == "0" else kb.KernelBuilder._recover_susfs_init_c(b, tree)
if ok and not kb.KernelBuilder._disable_susfs_preinit(b, tree):
    print("REASON=kernel/Kbuild: preinit block in an unknown shape (_disable_susfs_preinit)")
    ok = False
if ok and not kb.KernelBuilder._fixup_susfs_sucompat_util_h(b, tree):
    print("REASON=feature/sucompat.c: userspace_stack_buffer() not reachable (_fixup_susfs_sucompat_util_h)")
    ok = False
if ok and not kb.KernelBuilder._fixup_susfs_pkg_observer_c(b, tree):
    print("REASON=manager/pkg_observer.c: susfs priority call in an unknown place (_fixup_susfs_pkg_observer_c)")
    ok = False
if ok and not kb.KernelBuilder._disable_susfs_mount_hide(b, tree):
    print("REASON=feature/mount_hide: Kbuild/header in an unknown shape (_disable_susfs_mount_hide)")
    ok = False
print("RECOVERY=" + ("True" if ok else "False"))
PY
)"
    # kernel_builder prints things on import - keep only our marker lines
    reasons="$(printf '%s\n' "$rec" | grep '^REASON=' | cut -d= -f2-)"
    rec="$(printf '%s\n' "$rec" | grep -m1 '^RECOVERY=' | cut -d= -f2)"
    if [ "$rec" = "True" ] && [ "$rc" -eq 0 ]; then
        VERDICT="CLEAN"; DETAIL="patch applies fully - no recovery needed"
    elif [ "$rec" = "True" ]; then
        left="$(grep -rn --include='*.c' --include='*.h' ksu_late_loaded "$WORKTREE/kernel" 2>/dev/null | grep -vc '\.orig' || true)"
        rej="$(find "$WORKTREE" -name '*.rej' | wc -l)"
        if [ "$left" = "0" ] && [ "$rej" = "0" ]; then
            VERDICT="RECOVERED"
            DETAIL="pipeline recovery fixed it (same code the build runs)"
        else
            DETAIL="recovery ran but left $left ksu_late_loaded ref(s) / $rej .rej file(s)"
        fi
    elif [ "$rec" = "False" ]; then
        DETAIL="pipeline recovery could not handle this - upstream changed something new"
        if [ -n "$reasons" ]; then
            printf '%s\n' "$reasons" | sed 's/^/    recovery: /'
        fi
    else
        DETAIL="could not run kernel_builder.py (python error)"
    fi
    if [ "$VERDICT" = "CLEAN" ] || [ "$VERDICT" = "RECOVERED" ]; then
        fuzz_report "$PATCHOUT"
        if [ "$FUZZ_BAD" -gt 0 ]; then
            VERDICT="STOP"
            DETAIL="$FUZZ_BAD hunk(s) applied with fuzz but landed outside a function (see above)"
        fi
    fi
}

# fuzz_report  - after a CLEAN/RECOVERED patch_test: look at every hunk that
# `patch` applied with fuzz 2 or 3. Fuzz 3 can drop ALL context lines, and
# then `patch` says "succeeded" while the lines land in the wrong place
# (41083: susfs' pkg_observer.c call went into a comment above the function
# and only the compile failed). Checks the FINAL tree (after the pipeline's
# recovery/fixups), so a fuzzy hunk a fixup already moved shows as fine.
# Code that ends up outside any function = certain compile failure -> STOP.
# A new fuzzy hunk, or one in a file SukiSU changed since the last good
# build -> WARN with where it landed. Same hunk, same file -> counted only.
KNOWN_FUZZ_kernel_manager_pkg_observer_c="_fixup_susfs_pkg_observer_c"
FUZZ_BAD=0; FUZZ_WARN=0
fuzz_report() {
    local outfile="$1" goodpatch rows f num fz age st key known changed line where n_old=0
    FUZZ_BAD=0; FUZZ_WARN=0
    goodpatch="$(mktemp)"
    if [ -n "${PIN_PATCH_REF:-}" ]; then
        git -C "$SUSFS" show "$PIN_PATCH_REF:$KSU_PATCH_REL" > "$goodpatch" 2>/dev/null || : > "$goodpatch"
    fi
    rows="$(python3 - "$WORKTREE" "$outfile" "$PATCHFILE" "$goodpatch" 2>/dev/null <<'PY'
import re, sys, hashlib
# argv: <tree> <patch output file> <candidate patch> [<last good patch>]
# Prints one line per hunk that applied with fuzz >= 2:
#   FUZZ|<file>|<hunk>|<fuzz>|<new or old>|<OUTSIDE|inside <func>|removal|unchecked>
tree, outf, patchf = sys.argv[1], sys.argv[2], sys.argv[3]
goodf = sys.argv[4] if len(sys.argv) > 4 else ""

def hunks(path):
    """{file: [ {'plus': [...], 'sig': str}, ... ]} in patch order."""
    res, cur_file, cur = {}, None, None
    try:
        lines = open(path, errors="surrogateescape").read().splitlines()
    except OSError:
        return res
    for ln in lines:
        if ln.startswith("+++ "):
            p = ln[4:].split("\t")[0].strip()
            cur_file = p[2:] if p.startswith("b/") else p
            res.setdefault(cur_file, [])
            cur = None
        elif ln.startswith("@@") and cur_file is not None:
            cur = {"plus": [], "body": []}
            res[cur_file].append(cur)
        elif cur is not None and ln[:1] in ("+", "-"):
            cur["body"].append(ln)
            if ln[:1] == "+":
                cur["plus"].append(ln[1:])
    for hs in res.values():
        for h in hs:
            h["sig"] = hashlib.sha1("\n".join(h["body"]).encode("utf-8", "surrogateescape")).hexdigest()
    return res

cand, good = hunks(patchf), (hunks(goodf) if goodf else {})

def strip_code(text):
    """Blank out comments, strings and char literals; keep newlines."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i); j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2); j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j])); i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(" " * (j - i)); i = j
        else:
            out.append(c); i += 1
    return "".join(out)

def depth_map(text):
    """Brace depth at the start of each line (1-based) + enclosing function name."""
    clean = strip_code(text).split("\n")
    depth, last_sig, func = 0, "", ""
    depths, funcs = [0], [""]
    for ln in clean:
        depths.append(depth); funcs.append(func if depth > 0 else "")
        if ln.lstrip().startswith("#"):
            continue
        for ch in ln:
            if ch == "{":
                if depth == 0:
                    m = re.findall(r"([A-Za-z_]\w*)\s*\(", last_sig)
                    func = m[-1] if m else "?"
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
        if depth == 0 and ln.strip():
            last_sig = (last_sig + " " + ln.strip())[-300:] if not ln.strip().endswith(";") else ""
    return depths, funcs

out_lines = open(outf, errors="surrogateescape").read().splitlines()
cur = None
for ln in out_lines:
    m = re.match(r"patching file (\S+)", ln)
    if m:
        cur = m.group(1); continue
    m = re.match(r"Hunk #(\d+) succeeded at (\d+) with fuzz (\d+)", ln)
    if not m or cur is None:
        continue
    num, at, fuzz = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if fuzz < 2:
        continue
    h = (cand.get(cur) or [None] * num)[num - 1] if len(cand.get(cur) or []) >= num else None
    sigs = {x["sig"] for x in good.get(cur, [])}
    age = "old" if h is not None and h["sig"] in sigs else "new"
    status = "unchecked"
    if h is not None and cur.endswith((".c", ".h")):
        plus = [p for p in h["plus"] if p.strip() and not p.strip().startswith(("//", "/*", "*", "#"))]
        if not [p for p in h["plus"] if p.strip()]:
            status = "removal"  # removed lines must match exactly, even with fuzz
        elif not plus:
            status = "no-code"  # only includes/comments/blank lines added
        else:
            try:
                text = open(f"{tree}/{cur}", errors="surrogateescape").read()
            except OSError:
                text = None
            if text is not None:
                flines = text.split("\n")
                target = plus[0]
                hits = [i + 1 for i, fl in enumerate(flines) if fl == target]
                if hits:
                    line = min(hits, key=lambda x: abs(x - at))
                    depths, funcs = depth_map(text)
                    indented = target[:1] in (" ", "\t")
                    if depths[line] == 0 and indented and target.rstrip().endswith(";"):
                        status = f"OUTSIDE:{line}"
                    elif depths[line] > 0:
                        status = f"inside {funcs[line] or '?'}:{line}"
                    else:
                        status = f"top-level:{line}"
    print(f"FUZZ|{cur}|{num}|{fuzz}|{age}|{status}")
PY
)"
    rm -f "$goodpatch"
    [ -n "$rows" ] || return 0
    while IFS='|' read -r _ f num fz age st; do
        [ -n "$f" ] || continue
        key="KNOWN_FUZZ_$(printf '%s' "$f" | tr '/.-' '___')"
        known="${!key:-}"
        changed=""
        if [ -n "$PIN_SHA" ] && [ -n "$(git -C "$SUKI" log --oneline -1 "$PIN_SHA..$CAND_SHA" -- "$f" 2>/dev/null)" ]; then
            changed=1
        fi
        line="${st##*:}"; [ "$line" = "$st" ] && line=""
        case "$st" in
            OUTSIDE:*)
                FUZZ_BAD=$((FUZZ_BAD + 1))
                bad "$f hunk #$num (fuzz $fz) landed OUTSIDE any function (line $line) -"
                echo "        patch said \"succeeded\", but this code can't compile there."
                ;;
            *)
                if [ "$age" = "old" ] && [ -z "$changed" ]; then
                    n_old=$((n_old + 1)); continue
                fi
                if [ -n "$known" ]; then
                    echo "  ${C}INFO${N}  $f hunk #$num (fuzz $fz): checked and placed by $known"
                    continue
                fi
                if [ "$st" = "removal" ]; then
                    echo "  ${C}INFO${N}  $f hunk #$num (fuzz $fz): only removes lines (they must match exactly) - fine."
                    continue
                fi
                FUZZ_WARN=$((FUZZ_WARN + 1))
                case "$st" in
                    inside\ *) where="inside ${st#inside }"; where="${where%:*}() at line $line" ;;
                    top-level:*) where="at file level, line $line" ;;
                    no-code) where="(adds only includes/comments)" ;;
                    *) where="(position not checked)" ;;
                esac
                warn "$f hunk #$num applied with fuzz $fz - $where"
                if [ "$age" = "new" ]; then
                    echo "        new hunk in this susfs patch - check it went to the right place."
                else
                    echo "        SukiSU changed this file since the last good build - check it."
                fi
                ;;
        esac
    done <<< "$rows"
    if [ "$n_old" -gt 0 ]; then
        echo "  ${C}INFO${N}  $n_old fuzzy hunk(s), same as in the last good build (file unchanged) - fine."
    fi
}

# drift_report <files>  - who changed each file with a rejected hunk
KNOWN_DRIFT_kernel_core_init_c="_recover_susfs_init_c"
KNOWN_DRIFT_kernel_feature_selinux_hide_c="_recover_susfs_selinux_hide_c"
KNOWN_DRIFT_kernel_runtime_ksud_integration_c="_recover_susfs_ksud_integration_c"
KNOWN_DRIFT_kernel_supercall_dispatch_c="_recover_susfs_dispatch_c"
KNOWN_DRIFT_kernel_hook_setuid_hook_c="_recover_susfs_setuid_hook_c"
drift_report() {
    local f log key known
    [ -n "$1" ] || return 0
    echo
    echo "  ${B}Drift report${N} (SukiSU ${PIN_SHA:0:8} -> ${CAND_SHA:0:8}):"
    for f in $1; do
        key="KNOWN_DRIFT_$(printf '%s' "$f" | tr '/.-' '___')"
        known="${!key:-}"
        echo "    ${C}$f${N}${known:+  (known drift point, handled by $known)}"
        if [ -n "$PIN_SHA" ]; then
            log="$(git -C "$SUKI" log --oneline --no-decorate "$PIN_SHA..$CAND_SHA" -- "$f" 2>/dev/null)"
        else
            log=""
        fi
        if [ -n "$log" ]; then
            echo "      changed in SukiSU since the last good build by:"
            printf '%s\n' "$log" | head -n 10 | sed 's/^/        /'
        else
            if [ "$PATCH_SAME" = 1 ]; then
                echo "      not changed in SukiSU since the last good build, and the susfs"
                echo "      patch is the same - old drift, normally handled by recovery."
            else
                echo "      not changed in SukiSU since the last good build -"
                echo "      the susfs KernelSU patch is what moved for this file."
            fi
        fi
    done
    echo
    echo "  Send this whole output for a look. Files marked 'known drift point'"
    echo "  usually need only a small extension of that recovery function."
}

# Was the susfs KernelSU patch at HEAD the very same one that worked last
# time? Then any drift is on the SukiSU side, and trying older versions of
# that patch can only fit worse.
PATCH_SAME=0
if [ -n "$SUSFS_PIN_FULL" ]; then
    PIN_PATCH_REF="$(susfs_p "$SUSFS_PIN_FULL")"; : "${PIN_PATCH_REF:=$SUSFS_PIN_K}"
    if [ -n "$PIN_PATCH_REF" ] && \
       git -C "$SUSFS" diff --quiet "$PIN_PATCH_REF" "$SUSFS_HEAD" -- "$KSU_PATCH_REL" 2>/dev/null; then
        PATCH_SAME=1
    fi
fi

hdr "Patch test: ${CAND_LABEL} ${CAND_SHA:0:8} + susfs ${SUSFS_HEAD:0:8}"
SUSFS_USE="$SUSFS_HEAD"
patch_test "$SUSFS_HEAD"
case "$VERDICT" in
    CLEAN|RECOVERED) ok "$DETAIL" ;;
    *)               tried "$DETAIL"; drift_report "$LAST_FAILED" ;;
esac
TRY_FALLBACK=1
if [ "$VERDICT" = "STOP" ] && [ "$PATCH_SAME" = 1 ]; then
    TRY_FALLBACK=0
    echo "  The susfs KernelSU patch is the same one that worked last time"
    echo "  (${PIN_PATCH_REF:0:8}), so older versions of it won't help - skipping fallbacks."
fi

# ---- 3b. fallback: older KernelSU patch + HEAD's kernel side -----------------
# susfs upstream sometimes rewrites ONLY the KernelSU patch for tiann/KernelSU
# (e.g. 0c82f6e "Sync with the official KernelSU main repo"), so it stops
# fitting SukiSU, while later commits fix the kernel side only. Walk back
# through the versions of that one file and take the newest that fits - but
# stop at any commit that changed the KernelSU patch AND something else: that
# is a paired API change and must not be split.
if [ "$VERDICT" = "STOP" ] && [ "$TRY_FALLBACK" = 1 ]; then
    mapfile -t KSU_CHANGES < <(git -C "$SUSFS" log --format=%H "$SUSFS_HEAD" -- "$KSU_PATCH_REL" 2>/dev/null | head -n 6)
    skipped=()
    for ((i = 0; i + 1 < ${#KSU_CHANGES[@]}; i++)); do
        c="${KSU_CHANGES[$i]}"
        other="$(git -C "$SUSFS" show --name-only --format= "$c" | grep -vxF "$KSU_PATCH_REL" | grep -c . || true)"
        if [ "$other" != "0" ]; then
            echo
            tried "susfs $(git -C "$SUSFS" log -1 --format='%h "%s"' "$c")"
            echo "        changes the KernelSU patch AND the kernel side together - not safe to split."
            break
        fi
        skipped+=("$c")
        cand="$(sha_of "$SUSFS" "$c^")"     # last commit with the version before $c
        hdr "Fallback: ${CAND_LABEL} ${CAND_SHA:0:8} + KernelSU patch from susfs ${cand:0:8}"
        patch_test "$cand"
        if [ "$VERDICT" = "CLEAN" ] || [ "$VERDICT" = "RECOVERED" ]; then
            ok "$DETAIL"
            SUSFS_USE="$SUSFS_HEAD+$cand"
            echo
            echo "  Split pin: kernel side from susfs ${B}${SUSFS_HEAD:0:8}${N} (HEAD),"
            echo "             KernelSU patch from susfs ${B}${cand:0:8}${N}."
            echo "  Leaving out these KernelSU-patch-only change(s), which don't fit SukiSU:"
            for x in "${skipped[@]}"; do
                git -C "$SUSFS" log -1 --format='      %h %s' "$x"
            done
            break
        fi
        tried "$DETAIL"
    done
fi

# ---- 3c. fallback: new SukiSU + the known-good susfs pin ---------------------
# susfs sometimes reworks its patches in a way that needs new recovery work
# (or that doesn't fit our kernel sub_level yet), while SukiSU itself moved
# only a little. Then the new SukiSU can still be built with the susfs that
# already works on the phone - same SUSFS code as the current kernel. The
# newer susfs waits for a separate look.
SUSFS_HELD=0
if [ "$VERDICT" = "STOP" ] && [ "$PATCH_SAME" = 0 ] && [ -n "$SUSFS_PIN_FULL" ] && \
   [ "$SUSFS_HEAD" != "$SUSFS_PIN_K" ]; then
    PIN_PATCH_REF="$(susfs_p "$SUSFS_PIN_FULL")"; : "${PIN_PATCH_REF:=$SUSFS_PIN_K}"
    hdr "Fallback: ${CAND_LABEL} ${CAND_SHA:0:8} + known-good susfs $(short_susfs "$SUSFS_PIN_FULL")"
    patch_test "$PIN_PATCH_REF"
    if [ "$VERDICT" = "CLEAN" ] || [ "$VERDICT" = "RECOVERED" ]; then
        ok "$DETAIL"
        SUSFS_USE="$SUSFS_PIN_FULL"
        SUSFS_HELD=1
        echo
        echo "  The newer susfs ${SUSFS_HEAD:0:8} doesn't fit yet, so this build keeps the"
        echo "  susfs your current kernel already uses ($(short_susfs "$SUSFS_PIN_FULL")) - same SUSFS code,"
        echo "  nothing changes on the SUSFS side. Only SukiSU moves forward."
        echo "  Send this output if you want the newer susfs looked at."
    else
        tried "$DETAIL"
        PATCH_SAME=1   # same susfs patch as last time: any drift is SukiSU's
        drift_report "$LAST_FAILED"
    fi
fi

SUSFS_NEW=0
[ "$SUSFS_USE" != "$SUSFS_PIN_FULL" ] && SUSFS_NEW=1

# ---- verdict + command -------------------------------------------------------
hdr "What to do"

case "$BRANCH" in
    gki-android13-5.15) EXTRA=""; MENU="menu option 1 (or pick a newer 5.15 sub_level)" ;;
    *)                  EXTRA="--no-ath9k "; MENU="menu option 2 (Custom), values from matrix.json" ;;
esac

if [ "$VERDICT" != "STOP" ] && grep -qF "$CAND_SHA $SUSFS_USE " "$BROKEN" 2>/dev/null; then
    VERDICT="STOP"
    bad "You marked SukiSU ${CAND_SHA:0:8} + susfs $(short_susfs "$SUSFS_USE") as broken earlier."
    echo "        Waiting for a newer commit. To allow it again, delete its line in:"
    echo "        $BROKEN"
    echo
fi

if [ "$VERDICT" = "STOP" ]; then
    bad "Do not build this combination. Send the output above for a look."
    echo
    echo "  The last working build is still:"
    echo "    ./build-kernel.sh ${EXTRA}--ksu-commit $PINNED_KSU_REF \\"
    echo "      --susfs-commit ${SUSFS_PIN:-<pin>} \\"
    echo "      --ksu-version-code $PINNED_KSU_VERSION_CODE"
    exit 1
fi

if [ "$KSU_NEW" = 0 ] && [ "$SUSFS_NEW" = 0 ] && [ "$AWG_NEW" = 0 ]; then
    ok "Nothing new since the last build (${PIN_SHA:0:8} / $(short_susfs "$SUSFS_USE")). You're up to date."
    echo
    echo "  Rebuild command, if you need it (new kernel sub_level etc.):"
else
    ok "Something new and it patches cleanly. ${B}Build this:${N}"
    if [ "$FUZZ_WARN" -gt 0 ]; then
        warn "$FUZZ_WARN fuzzy hunk(s) to look at (WARN lines in the patch test above)."
        echo "        If the build fails in one of those files, that is the reason -"
        echo "        send this output and the build log."
    fi
    [ "$SUSFS_HELD" = 1 ] && \
        echo "        (new SukiSU, susfs stays on $(short_susfs "$SUSFS_USE") - newer susfs ${SUSFS_HEAD:0:8} doesn't fit yet)"
    [ "$AWG_NEW" = 1 ] && [ "$KSU_NEW" = 0 ] && [ "$SUSFS_NEW" = 0 ] && \
        echo "        (only AmneziaWG is new: ${AWG_USE:0:8}, $AWG_VER)"
fi
# AmneziaWG is on by default in build-kernel.sh; the flag only moves the commit.
AWG_ARG=""
[ "$AWG_NEW" = 1 ] && AWG_ARG=" --amneziawg-commit $AWG_USE"

if [ "$CAND_CODE" = "?" ]; then
    warn "Could not compute the version code - read it from the manager instead."
    CAND_CODE="<manager version>"
fi

echo
echo "  ${C}./build-kernel.sh ${EXTRA}--ksu-commit $CAND_SHA \\"
echo "    --susfs-commit $SUSFS_USE \\"
echo "    --ksu-version-code $CAND_CODE${AWG_ARG}${N}"
echo
echo "  $MENU"
if [ "$BRANCH" = "gki-android13-5.15" ]; then
    echo
    echo "  ${Y}Experimental${N} - same build plus Droidspaces --pids-limit (CONFIG_CGROUP_PIDS"
    echo "  via the net_prio slot swap). Test builds only, files get a \"-pids\" marker:"
    echo "  ${C}./build-kernel.sh --ksu-commit $CAND_SHA \\"
    echo "    --susfs-commit $SUSFS_USE \\"
    echo "    --ksu-version-code $CAND_CODE --pids${AWG_ARG}${N}"
    echo
    echo "  ${Y}KPM next${N} - same --pids build with KernelPatch 0.13.9 for KPM"
    echo "  (pinned kptools + kpimg, see KPM_NEXT_* in kernel_builder.py):"
    echo "  ${C}./build-kernel.sh --ksu-commit $CAND_SHA \\"
    echo "    --susfs-commit $SUSFS_USE \\"
    echo "    --ksu-version-code $CAND_CODE --pids --kpm-next${AWG_ARG}${N}"
fi

if [ "$KSU_NEW" = 1 ] || [ "$SUSFS_NEW" = 1 ] || [ "$AWG_NEW" = 1 ]; then
    [ "$CAND_CODE" != "<manager version>" ] && \
        echo "$CAND_SHA $SUSFS_USE $CAND_CODE $(date +%F) $AWG_USE" > "$PENDING"
    echo
    echo "  Before flashing:"
    echo "   1. Manager: install the SukiSU CI build for commit ${CAND_SHA:0:8} and"
    echo "      check it says ${B}$CAND_CODE${N} - kernel and manager must match."
    echo "      https://github.com/SukiSU-Ultra/SukiSU-Ultra/actions/workflows/build-manager.yml"
    echo "   2. In the build log / PATCH_STATUS.json:"
    echo "        SukiSU-Ultra version: $CAND_CODE [...-${CAND_SHA:0:8}@HEAD]"
    echo "        susfs_kernelsu_integration -> applied"
    echo "        image_ikconfig             -> all expected present"
    if [ "$BRANCH" = "gki-android13-5.15" ] && [ -n "$AWG_USE" ]; then
        echo "        amneziawg                  -> applied (AmneziaWG $(awg_version_of "$AWG_USE") @ ${AWG_USE:0:12})"
    fi
    echo "   3. After it boots: manager shows ${CAND_CODE}-${MAIN_UAPI} for both, check-features.sh is clean."
    echo "   4. Run ${B}./check-release.sh${N} again - it will ask if this build works."
    echo "      Answer 1 and it saves these values into config.py by itself."
    echo "      Then push config.py (gitsync push) and release as v${CAND_CODE}."
fi
