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
#   4. prints the exact ./build-kernel.sh command to run,
#   5. remembers that suggestion, and on the NEXT run asks whether you built,
#      flashed and tested it - answer 1 and it updates the known-good pins
#      in config.py by itself.
#
# The known-good pins live in ONE place:
#   .github/workflows/scripts/config.py
#     DEFAULT_KSU_REF, DEFAULT_KSU_VERSION_CODE, DEFAULT_SUSFS_PINS
# They are what every unpinned build uses (build-kernel.sh, build.py, both
# Actions workflows), so saving here also moves the defaults for forks/CI.
#
# Read-only for the build workspace. Clones into a cache dir.

set -uo pipefail

BRANCH="gki-android13-5.15"

SUKI_URL="https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"
SUSFS_URL="https://github.com/ShirkNeko/susfs4ksu.git"
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
        if name in ("DEFAULT_KSU_REF", "DEFAULT_KSU_VERSION_CODE", "DEFAULT_SUSFS_PINS"):
            vals[name] = ast.literal_eval(n.value)
print(f"PINNED_KSU_REF={vals.get('DEFAULT_KSU_REF', '')}")
print(f"PINNED_KSU_VERSION_CODE={vals.get('DEFAULT_KSU_VERSION_CODE', '')}")
for b, r in vals.get("DEFAULT_SUSFS_PINS", {}).items():
    print(f"SUSFS {b} {r}")
PY
)" || return 1
    PINNED_KSU_REF="$(printf '%s\n' "$out" | sed -n 's/^PINNED_KSU_REF=//p')"
    PINNED_KSU_VERSION_CODE="$(printf '%s\n' "$out" | sed -n 's/^PINNED_KSU_VERSION_CODE=//p')"
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

# ---- 0. follow-up on the build suggested last time ---------------------------
# When the script prints "Build this:" it saves that exact combination here.
# Next run it asks about THAT combination - not whatever upstream has moved to
# in the meantime - so answering "1" can never save something you didn't build.
PENDING="$CACHE/pending-$BRANCH"
BROKEN="$CACHE/broken-$BRANCH"
touch "$BROKEN"

save_pins() {   # $1=ksu sha  $2=susfs sha  $3=version code  -> edits config.py
    # temp file in the same dir so the final mv is an atomic rename
    local tmp; tmp="$(mktemp "$BUILDER_DIR/.config.py.XXXXXX")" || return 1
    python3 - "$CONFIG_PY" "$tmp" "$BRANCH" "$1" "$2" "$3" <<'PY' || { rm -f "$tmp"; return 1; }
import ast, re, sys
src_path, dst, branch, ksu, susfs, code = sys.argv[1:]
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

line = re.compile(r'^(\s*)"' + re.escape(branch) + r'": "[0-9a-f]*",', re.M)
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
open(dst, "w").write(s)
PY
    chmod --reference="$CONFIG_PY" "$tmp" 2>/dev/null
    mv -f "$tmp" "$CONFIG_PY"
}

if [ -s "$PENDING" ]; then
    read -r P_KSU P_SUSFS P_CODE P_DATE < "$PENDING"
    PIN_NOW="$(git -C "$SUKI" rev-parse -q --verify "$PINNED_KSU_REF^{commit}" 2>/dev/null)"
    if [ "$P_KSU" = "$PIN_NOW" ] && [ "$P_SUSFS" = "${PINNED_SUSFS[$BRANCH]:-}" ]; then
        rm -f "$PENDING"          # already saved (e.g. by hand)
    elif [ "$ASK" = 1 ] && [ -t 0 ]; then
        hdr "Last suggested build ($P_DATE)"
        echo "  SukiSU  ${P_KSU:0:8}   version $P_CODE"
        echo "  susfs   ${P_SUSFS:0:8}   ($BRANCH)"
        echo
        echo "  Did you build, flash and test it?"
        echo "    1) Yes, it works  - save it as the new known-good build"
        echo "    2) Not yet        - ask me again next time"
        echo "    3) It's broken    - never suggest this combination again"
        read -rp "  Choice [1/2/3, Enter = 2]: " ans
        case "$ans" in
            1)
                if save_pins "$P_KSU" "$P_SUSFS" "$P_CODE"; then
                    rm -f "$PENDING"
                    [ "$BRANCH" = "gki-android13-5.15" ] && {
                        PINNED_KSU_REF="$P_KSU"; PINNED_KSU_VERSION_CODE="$P_CODE"; }
                    PINNED_SUSFS[$BRANCH]="$P_SUSFS"
                    ok "Saved as the known-good build in config.py."
                    echo "        Upload .github/workflows/scripts/config.py to GitHub too -"
                    echo "        that also moves the default pins for CI and forks."
                else
                    bad "Could not update config.py - pins unchanged. Edit them by hand."
                fi ;;
            3)
                echo "$P_KSU $P_SUSFS $P_DATE" >> "$BROKEN"
                rm -f "$PENDING"
                warn "Marked as broken. It won't be suggested again (list: $BROKEN)." ;;
            *)
                echo "  OK, asking again next time." ;;
        esac
    fi
fi

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
if [ -z "$SUSFS_PIN" ]; then
    SUSFS_NEW=1
    echo "  HEAD   ${SUSFS_HEAD:0:8}  (no known-good pin for this branch in config.py yet)"
elif [ "$SUSFS_HEAD" != "$(sha_of "$SUSFS" "$SUSFS_PIN")" ]; then
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

if [ "$VERDICT" != "STOP" ] && grep -q "^$CAND_SHA $SUSFS_HEAD " "$BROKEN" 2>/dev/null; then
    VERDICT="STOP"
    bad "You marked SukiSU ${CAND_SHA:0:8} + susfs ${SUSFS_HEAD:0:8} as broken earlier."
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
    [ "$CAND_CODE" != "<manager version>" ] && \
        echo "$CAND_SHA $SUSFS_HEAD $CAND_CODE $(date +%F)" > "$PENDING"
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
    echo "   4. Run ${B}./check-release.sh${N} again - it will ask if this build works."
    echo "      Answer 1 and it saves these values into config.py by itself."
    echo "      Then upload config.py and release as v$CAND_CODE."
fi
