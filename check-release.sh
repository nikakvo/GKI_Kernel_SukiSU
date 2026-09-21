#!/usr/bin/env bash
# check-release.sh - is there something new to build, and exactly how?
#
# Tracks SukiSU-Ultra main (UAPI 4) + susfs4ksu branch HEAD. Without building
# anything it:
#   1. finds the newest SukiSU ref worth building (a UAPI>=4 tag if one exists,
#      otherwise main HEAD) and computes its version code the same way the
#      kernel Kbuild and the manager do,
#   2. finds the newest susfs commit for the branch,
#   3. applies 10_enable_susfs_for_ksu.patch FOR REAL in a throwaway worktree
#      and runs the pipeline's own _recover_susfs_init_c() on it - so the
#      verdict comes from the same code the build will run, not from counting
#      hunks,
#   4. prints the exact ./build-kernel.sh command to run.
#
# Read-only for the build workspace. Clones into a cache dir.

set -uo pipefail

# ---- last build that was flashed and confirmed working -----------------------
# Update these after a new build boots on the phone.
PINNED_KSU_REF="cf87e3f4ddd3f6e5464d85acf56aaa6950e70841"   # SukiSU main, 2026-09-21
PINNED_KSU_VERSION_CODE="40939"
declare -A PINNED_SUSFS=(
    [gki-android13-5.15]="e565931d19256fd821ada01b35263506e7c7a364"
    # 6.1 / 6.6 were last built against SukiSU v4.2.0, not main. Their pins
    # are kept for reference only; the check below always tests branch HEAD.
    [gki-android14-6.1]="4fc9c1898ea66f51847cdbc0d1473ea4ef525a70"
    [gki-android15-6.6]="937215cb3a1b1f333d764c366c7a49972fa8e7a0"
)
BRANCH="gki-android13-5.15"

SUKI_URL="https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"
SUSFS_URL="https://github.com/ShirkNeko/susfs4ksu.git"
CACHE="${GKI_CHECK_CACHE:-$HOME/.cache/gki-check}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILDER_DIR="$SCRIPT_DIR/.github/workflows/scripts"

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --cache)  CACHE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--branch gki-android13-5.15|gki-android14-6.1|gki-android15-6.6] [--cache DIR]"
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
hdr()  { echo; echo "${B}=== $* ===${N}"; }

for c in git patch python3; do
    command -v "$c" >/dev/null || { echo "$c not found" >&2; exit 1; }
done
[ -f "$BUILDER_DIR/kernel_builder.py" ] || {
    echo "kernel_builder.py not found at $BUILDER_DIR" >&2
    echo "Run this script from the repo root (next to build-kernel.sh)." >&2
    exit 1; }

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

uapi_of() {     # $1=ref -> number or nothing
    git -C "$SUKI" show "$1:uapi/supercall.h" 2>/dev/null \
        | grep -m1 -oE "KERNEL_SU_UAPI_VERSION = [0-9]+" | grep -oE "[0-9]+$"
}
sha_of()   { git -C "$1" rev-parse -q --verify "$2^{commit}" 2>/dev/null; }

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

# Prefer a tag if one carries the same UAPI as main - tags don't move.
CAND_REF=""; CAND_LABEL=""
NEWEST_TAG="$(git -C "$SUKI" tag --sort=-creatordate | head -n1)"
TAG_UAPI="$(uapi_of "$NEWEST_TAG")"; : "${TAG_UAPI:=-}"
if [ "$TAG_UAPI" != "-" ] && [ "$MAIN_UAPI" != "?" ] && [ "$TAG_UAPI" -ge "$MAIN_UAPI" ] 2>/dev/null; then
    CAND_REF="$NEWEST_TAG"; CAND_LABEL="tag $NEWEST_TAG"
    ok "Tag ${B}$NEWEST_TAG${N} carries UAPI $TAG_UAPI - building from the tag."
else
    CAND_REF="$MAIN_SHA"; CAND_LABEL="main"
    echo "  Newest tag: $NEWEST_TAG (UAPI $TAG_UAPI) - main is UAPI $MAIN_UAPI, so building from main."
fi
CAND_SHA="$(sha_of "$SUKI" "$CAND_REF")"
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
SUSFS_HEAD="$(sha_of "$SUSFS" "origin/$BRANCH")"
[ -n "$SUSFS_HEAD" ] || { bad "branch $BRANCH does not exist on $SUSFS_URL"; exit 1; }
SUSFS_NEW=0
if [ -n "$SUSFS_PIN" ] && [ "$SUSFS_HEAD" != "$(sha_of "$SUSFS" "$SUSFS_PIN")" ]; then
    SUSFS_NEW=1
    echo "  built  ${SUSFS_PIN:0:8}"
    echo "  HEAD   ${SUSFS_HEAD:0:8}  ($(git -C "$SUSFS" rev-list --count "$SUSFS_PIN..$SUSFS_HEAD" 2>/dev/null || echo '?') new):"
    git -C "$SUSFS" log --oneline --no-decorate "$SUSFS_PIN..$SUSFS_HEAD" 2>/dev/null \
        | head -n 15 | sed 's/^/      /'
else
    echo "  HEAD   ${SUSFS_HEAD:0:8}  (same as last build)"
fi

# ---- 3. the real test: apply + run the pipeline's own recovery ---------------
hdr "Patch test: ${CAND_LABEL} ${CAND_SHA:0:8} + susfs ${SUSFS_HEAD:0:8}"

WORKTREE="$(mktemp -d)"
PATCHFILE="$(mktemp)"
cleanup() {
    git -C "$SUKI" worktree remove --force "$WORKTREE" >/dev/null 2>&1
    rm -rf "$WORKTREE" "$PATCHFILE"
}
trap cleanup EXIT

VERDICT="STOP"; DETAIL=""
if ! git -C "$SUSFS" show \
    "$SUSFS_HEAD:kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch" > "$PATCHFILE" 2>/dev/null; then
    DETAIL="10_enable_susfs_for_ksu.patch not found in susfs ${SUSFS_HEAD:0:8}"
else
    rm -rf "$WORKTREE"
    if ! git -C "$SUKI" worktree add -q --detach "$WORKTREE" "$CAND_SHA" >/dev/null 2>&1; then
        DETAIL="cannot check out SukiSU ${CAND_SHA:0:8}"
    else
        out="$(cd "$WORKTREE" && patch -p1 --fuzz=3 < "$PATCHFILE" 2>&1)"; rc=$?
        failed_files="$(printf '%s\n' "$out" \
            | awk '/^patching file /{f=$3} /FAILED/{if(f!=""){print f; f=""}}' \
            | sort -u | tr '\n' ' ')"
        n_failed="$(printf '%s\n' "$out" | grep -cE '^Hunk.*FAILED' || true)"
        if [ "$rc" -eq 0 ]; then
            VERDICT="CLEAN"; DETAIL="patch applies fully - no recovery needed"
        else
            echo "  $n_failed hunk(s) failed in: ${failed_files:-?}"
            rec="$(cd "$BUILDER_DIR" && python3 - "$WORKTREE" 2>/dev/null <<'PY'
import sys, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, '.')
from pathlib import Path
import kernel_builder as kb
b = object.__new__(kb.KernelBuilder)
print("RECOVERY=" + ("True" if kb.KernelBuilder._recover_susfs_init_c(b, Path(sys.argv[1])) else "False"))
PY
)"
            # kernel_builder prints things on import - keep only our marker line
            rec="$(printf '%s\n' "$rec" | grep -m1 '^RECOVERY=' | cut -d= -f2)"
            if [ "$rec" = "True" ]; then
                left="$(grep -rn --include='*.c' --include='*.h' ksu_late_loaded "$WORKTREE/kernel" 2>/dev/null | grep -vc '\.orig' || true)"
                rej="$(find "$WORKTREE" -name '*.rej' | wc -l)"
                if [ "$left" = "0" ] && [ "$rej" = "0" ]; then
                    VERDICT="RECOVERED"
                    DETAIL="_recover_susfs_init_c fixed it (same code the build runs)"
                else
                    DETAIL="recovery ran but left $left ksu_late_loaded ref(s) / $rej .rej file(s)"
                fi
            elif [ "$rec" = "False" ]; then
                DETAIL="_recover_susfs_init_c could not handle this - upstream changed something new"
            else
                DETAIL="could not run kernel_builder.py (python error)"
            fi
        fi
    fi
fi

case "$VERDICT" in
    CLEAN|RECOVERED) ok "$DETAIL" ;;
    *)               bad "$DETAIL" ;;
esac

# ---- verdict + command -------------------------------------------------------
hdr "What to do"

case "$BRANCH" in
    gki-android13-5.15) EXTRA=""; MENU="menu option 1 (or pick a newer 5.15 sub_level)" ;;
    *)                  EXTRA="--no-ath9k "; MENU="menu option 2 (Custom), values from matrix.json" ;;
esac

if [ "$VERDICT" = "STOP" ]; then
    bad "Do not build this combination. Send the output above for a look."
    echo
    echo "  The last working build is still:"
    echo "    ./build-kernel.sh ${EXTRA}--ksu-commit $PINNED_KSU_REF \\"
    echo "      --susfs-commit ${SUSFS_PIN:-<pin>} \\"
    echo "      --ksu-version-code $PINNED_KSU_VERSION_CODE"
    exit 1
fi

if [ "$KSU_NEW" = 0 ] && [ "$SUSFS_NEW" = 0 ]; then
    ok "Nothing new since the last build (${PIN_SHA:0:8} / ${SUSFS_HEAD:0:8}). You're up to date."
    echo
    echo "  Rebuild command, if you need it (new kernel sub_level etc.):"
else
    ok "Something new and it patches cleanly. ${B}Build this:${N}"
fi

if [ "$CAND_CODE" = "?" ]; then
    warn "Could not compute the version code - read it from the manager instead."
    CAND_CODE="<manager version>"
fi

echo
echo "  ${C}./build-kernel.sh ${EXTRA}--ksu-commit $CAND_SHA \\"
echo "    --susfs-commit $SUSFS_HEAD \\"
echo "    --ksu-version-code $CAND_CODE${N}"
echo
echo "  $MENU"

if [ "$KSU_NEW" = 1 ] || [ "$SUSFS_NEW" = 1 ]; then
    echo
    echo "  Before flashing:"
    echo "   1. Manager: install the SukiSU CI build for commit ${CAND_SHA:0:8} and"
    echo "      check it says ${B}$CAND_CODE${N} - kernel and manager must match."
    echo "      https://github.com/SukiSU-Ultra/SukiSU-Ultra/actions/workflows/build-manager.yml"
    echo "   2. In the build log / PATCH_STATUS.json:"
    echo "        SukiSU-Ultra version: $CAND_CODE [...-${CAND_SHA:0:8}@HEAD]"
    echo "        susfs_kernelsu_integration -> applied"
    echo "        image_ikconfig             -> all expected present"
    echo "   3. After it boots: manager shows ${CAND_CODE}-${MAIN_UAPI} for both, check-features.sh is clean."
    echo "   4. Then update PINNED_* at the top of this script, and release as v$CAND_CODE."
fi
