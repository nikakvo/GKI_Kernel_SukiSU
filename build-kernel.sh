#!/bin/bash
set -e

# Locates the repo automatically - works whether this script sits inside
# the repo itself, or next to it (e.g. in $HOME alongside
# GKI_KernelSU_SUSFS, GKI_KernelSU_SUSFS-main, or any other folder name;
# git clone vs GitHub's "Download ZIP" produce different folder names).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_repo_dir() {
    # 1) Maybe the script is already sitting inside the repo.
    if [ -d "$SCRIPT_DIR/.github/workflows/scripts" ]; then
        echo "$SCRIPT_DIR"
        return 0
    fi

    # 2) Otherwise scan subdirectories next to the script (depth 1-3) for
    #    the first one that contains .github/workflows/scripts.
    local match
    match="$(find "$SCRIPT_DIR" -maxdepth 3 -type d -path '*/.github/workflows/scripts' 2>/dev/null | head -n1)"
    if [ -n "$match" ]; then
        # strip the trailing /.github/workflows/scripts to get the repo root
        echo "${match%/.github/workflows/scripts}"
        return 0
    fi

    return 1
}

REPO_ROOT="$(find_repo_dir)" || {
    echo "Could not find the GKI_KernelSU_SUSFS repo (looked for a"
    echo "'.github/workflows/scripts' folder under: $SCRIPT_DIR"
    echo "Place build-kernel.sh either inside the repo, or in a parent"
    echo "folder that contains the repo as a subfolder."
    exit 1
}

REPO_DIR="$REPO_ROOT/.github/workflows/scripts"
MATRIX_FILE="$REPO_DIR/../config/matrix.json"
WORKSPACE="$HOME/gki-workspace"
LOGFILE="$HOME/build-$(date +%Y%m%d-%H%M%S).log"

# ---- Default values ----
ANDROID_VERSION="android13"
KERNEL_VERSION="5.15"
SUB_LEVEL="211"
OS_PATCH="2026-06"
KERNEL_TAG="android13-5.15.211_r00"
# Set to "1" if KERNEL_TAG above is an LTS-merge respin (e.g.
# android13-5.15.209_r00, dotted sub_level style) rather than a regular
# date-based one (e.g. android13-5.15-2026-06_r4) - adds a "-lts" marker
# to the output filename so it's clear at a glance where it came from.
IS_LTS=""
# Set to "1" to enable Droidspaces container-runtime support (real
# Linux namespace isolation - see README's "Droidspaces" section).
# Wired up for android12-5.10/android13-5.15/android14-6.1/android15-6.6
# (below-6.12 branches). Only 3 legitimate ANDROID_KABI_RESERVE-based
# kABI patch variants are tried, in order - if none matches this
# sub_level's task_struct layout, Droidspaces is cleanly skipped for
# that build rather than forcing a match (currently happens on some
# android15-6.6 sub_levels - check PATCH_STATUS.json's "droidspaces"
# entry after a build to see whether it actually landed).
# Defaults to enabled - set to "" to build without it.
DROIDSPACES="1"
# Congestion control: "none", "bbr1", or "bbr3" (android12/13/14/15
# so far - see README's "BBRv3" note).
BBR_VERSION="bbr3"
# LTO mode: "thin" (default, faster/lower-RAM) or "full" (slower,
# single-threaded, RAM-heavy link step, marginally better
# perf/code-size). Only applies to android12/android13 (legacy
# build.sh path) - ignored on Bazel branches.
LTO_MODE="full"
# Set to "1" to enable Baseband-guard (blocks unauthorized writes to
# baseband/modem and other protected partitions at the LSM level).
BBG="1"
# Comma-separated vendor module names to block from ever loading
# (CONFIG_DEBLOAT_VENDOR_MODULES). Auto-disables itself outside normal
# boot (recovery/fastbootd), so it never interferes with OTA/flashing.
# Leave empty ("") to disable this feature entirely.
BLACKLIST_MODULES=""
# Set to "1" to permanently patch out KernelSU/SukiSU volume-key safe-mode
# detection. Defaults OFF: SukiSU-Ultra fixed the safe-mode bug upstream
# (see the "Safe Mode Disabled" fix in nikakvo/GKI_KernelSU_SUSFS actions
# history), so this patch is no longer needed for most people - only turn
# it on if you specifically want safe-mode detection permanently disabled
# regardless of what upstream does. Most people should rely on
# YABP (github.com/Magisk-Modules-Repo/YetAnotherBootloopProtector) instead.
DISABLE_SAFEMODE=""
# ZRAM (LZ4KD compression). Was hardcoded on before - now toggleable.
# Defaults to enabled to preserve current behavior.
USE_ZRAM="1"
# MGLRU / PSI / NTSync - all have been unconditionally on until now.
# Toggleable here now for testing; default on to preserve current behavior.
USE_MGLRU="1"
USE_PSI="1"
USE_NTSYNC="1"
# NOTE: there used to be an ALLOW_BAZEL flag here gating whether
# Bazel/Kleaf-only branches (android15-6.6+, some newer android14-6.1
# sub_levels) were allowed to build at all. Removed - kernel_builder.py
# now builds those branches automatically the same as legacy
# build/build.sh branches (is_legacy is auto-detected either way). KMI
# symbol-list enforcement stays fully ON regardless - that was always
# the actual safety net, not the flag - so a genuine violation still
# fails the build loudly instead of producing an unverified Image.

# ============================================================
#  Dependency check / auto-install
# ============================================================
check_dependencies() {
    # System apt packages - based on the GitHub Actions workflows
    # (kernel-build.yml / build-kernels.yml) + standard host build tools
    local apt_packages=(
        git curl wget zip unzip xz-utils openssl pixz
        ccache python3 python3-pip
        build-essential bc bison flex
        libssl-dev libelf-dev rsync
    )
    local missing_apt=()

    for pkg in "${apt_packages[@]}"; do
        dpkg -s "$pkg" &>/dev/null || missing_apt+=("$pkg")
    done

    if [ ${#missing_apt[@]} -gt 0 ]; then
        echo "========================================"
        echo "  Missing dependencies, installing..."
        echo "========================================"
        echo "  ${missing_apt[*]}"
        echo ""
        sudo apt-get update
        sudo apt-get install -y "${missing_apt[@]}"
        echo ""
    fi

    # Python PyYAML module (used by matrix_generator.py and others)
    if ! python3 -c "import yaml" &>/dev/null; then
        echo "Missing Python module PyYAML, installing..."
        pip3 install --user PyYAML 2>/dev/null || pip3 install PyYAML
        echo ""
    fi

    if [ ${#missing_apt[@]} -eq 0 ]; then
        echo "All dependencies are present."
        echo ""
    else
        echo "Dependencies installed."
        echo ""
    fi
}

check_dependencies

# ============================================================
#  Build menu
# ============================================================
echo "========================================"
echo "  GKI KernelSU SUSFS - Build Menu"
echo "========================================"
echo "1) Default (android13 / 5.15 / 211 / 2026-06)"
echo "2) Custom (choose your own versions)"
echo "3) All versions from matrix.json"
echo "========================================"
read -rp "Choose an option [1-3]: " BUILD_OPTION

cd "$REPO_DIR"

if [ "$BUILD_OPTION" == "3" ]; then
    if [ ! -f "$MATRIX_FILE" ]; then
        echo "Could not find matrix.json at: $MATRIX_FILE"
        exit 1
    fi

    echo ""
    echo "Reading configurations from: $MATRIX_FILE"
    echo ""

    # android|kernel|sub_level|os_patch|revision|kernel_tag|lts, one line per configuration
    mapfile -t CONFIGS < <(python3 -c "
import json
with open('$MATRIX_FILE') as f:
    data = json.load(f)
for key, entries in data.items():
    android, kernel = key.split('-', 1)
    for e in entries:
        if not e.get('enabled', True):
            continue
        print(f\"{android}|{kernel}|{e['sub_level']}|{e['os_patch_level']}|{e.get('revision', '')}|{e.get('kernel_tag', '')}|{'1' if e.get('lts') else ''}\")
")

    TOTAL=${#CONFIGS[@]}
    if [ "$TOTAL" -eq 0 ]; then
        echo "matrix.json is empty, nothing to build."
        exit 1
    fi

    echo "Found $TOTAL configuration(s) to build:"
    for line in "${CONFIGS[@]}"; do
        IFS='|' read -r a k s p r t lts <<< "$line"
        echo "  - $a-$k-$s ($p)$( [ -n "$lts" ] && echo ' [LTS]')"
    done
    echo ""
    read -rp "Continue with all $TOTAL build(s)? (y/n) " confirm
    [ "$confirm" == "y" ] || { echo "Cancelled."; exit 0; }

    SUCCESS=0
    FAILED=0
    FAILED_LIST=()
    N=0

    for line in "${CONFIGS[@]}"; do
        IFS='|' read -r a k s p r t lts <<< "$line"
        N=$((N + 1))
        echo ""
        echo "========================================"
        echo "  [$N/$TOTAL] $a-$k-$s ($p)"
        echo "========================================"

        EXTRA_ARGS=()
        [ -n "$r" ] && EXTRA_ARGS+=(--revision "$r")
        # Pin kernel/common to the exact respin tag update_matrix.py recorded
        # for this sub_level/os_patch (instead of the moving branch HEAD),
        # so this matches the known-working respin the matrix was updated
        # from - not whatever Google has pushed to the branch since.
        [ -n "$t" ] && EXTRA_ARGS+=(--kernel-tag "$t")
        [ -n "$lts" ] && EXTRA_ARGS+=(--lts)
        [ -n "$DROIDSPACES" ] && EXTRA_ARGS+=(--droidspaces)
        [ -n "$BBG" ] && EXTRA_ARGS+=(--bbg)
        [ -n "$BLACKLIST_MODULES" ] && EXTRA_ARGS+=(--blacklist-modules "$BLACKLIST_MODULES")
        [ -n "$DISABLE_SAFEMODE" ] && EXTRA_ARGS+=(--disable-safemode)
        [ -n "$USE_ZRAM" ] && EXTRA_ARGS+=(--zram)
        [ -z "$USE_MGLRU" ] && EXTRA_ARGS+=(--no-mglru)
        [ -z "$USE_PSI" ] && EXTRA_ARGS+=(--no-psi)
        [ -z "$USE_NTSYNC" ] && EXTRA_ARGS+=(--no-ntsync)

        if python3 build.py \
            --android "$a" \
            --kernel "$k" \
            --sub-level "$s" \
            --os-patch "$p" \
            --bbr-version "$BBR_VERSION" \
            --lto-mode "$LTO_MODE" \
            --workspace "$WORKSPACE" \
            "${EXTRA_ARGS[@]}" \
            2>&1 | tee -a "$LOGFILE"; then
            SUCCESS=$((SUCCESS + 1))
        else
            FAILED=$((FAILED + 1))
            FAILED_LIST+=("$a-$k-$s")
        fi
    done

    echo ""
    echo "========================================"
    echo "  Summary"
    echo "========================================"
    echo "Total:   $TOTAL"
    echo "Success: $SUCCESS"
    echo "Failed:  $FAILED"
    if [ "$FAILED" -gt 0 ]; then
        echo "Failed configurations:"
        for f in "${FAILED_LIST[@]}"; do
            echo "  - $f"
        done
    fi
    echo "Build log saved to: $LOGFILE"
    exit 0
fi

if [ "$BUILD_OPTION" == "2" ]; then
    echo ""
    echo "Check available versions here:"
    echo "https://zzh20188.github.io/GKI_KernelSU_SUSFS/index.html"
    echo ""

    read -rp "Android Version (e.g. android13): " ANDROID_VERSION
    read -rp "Kernel Version (e.g. 5.15): " KERNEL_VERSION
    read -rp "Sublevel (e.g. 211): " SUB_LEVEL
    read -rp "Security Patch Level (e.g. 2026-06): " OS_PATCH
    echo ""
    echo "Check the exact respin tag here (optional, e.g. android13-5.15.211_r00):"
    echo "https://android.googlesource.com/kernel/common/+refs"
    read -rp "Kernel Tag (Enter to skip - uses the branch's latest HEAD): " KERNEL_TAG

elif [ "$BUILD_OPTION" != "1" ]; then
    echo "Invalid choice. Exiting."
    exit 1
fi

echo ""
echo "Building with:"
echo "  Android:  $ANDROID_VERSION"
echo "  Kernel:   $KERNEL_VERSION"
echo "  Sublevel: $SUB_LEVEL"
echo "  OS Patch: $OS_PATCH"
[ -n "$KERNEL_TAG" ] && echo "  Kernel Tag: $KERNEL_TAG"
echo ""

EXTRA_ARGS=()
[ -n "$KERNEL_TAG" ] && EXTRA_ARGS+=(--kernel-tag "$KERNEL_TAG")
[ -n "$IS_LTS" ] && EXTRA_ARGS+=(--lts)
[ -n "$DROIDSPACES" ] && EXTRA_ARGS+=(--droidspaces)
[ -n "$BBG" ] && EXTRA_ARGS+=(--bbg)
[ -n "$BLACKLIST_MODULES" ] && EXTRA_ARGS+=(--blacklist-modules "$BLACKLIST_MODULES")
[ -n "$DISABLE_SAFEMODE" ] && EXTRA_ARGS+=(--disable-safemode)
[ -n "$USE_ZRAM" ] && EXTRA_ARGS+=(--zram)
[ -z "$USE_MGLRU" ] && EXTRA_ARGS+=(--no-mglru)
[ -z "$USE_PSI" ] && EXTRA_ARGS+=(--no-psi)
[ -z "$USE_NTSYNC" ] && EXTRA_ARGS+=(--no-ntsync)

python3 build.py \
    --android "$ANDROID_VERSION" \
    --kernel "$KERNEL_VERSION" \
    --sub-level "$SUB_LEVEL" \
    --os-patch "$OS_PATCH" \
    --bbr-version "$BBR_VERSION" \
    --lto-mode "$LTO_MODE" \
    --workspace "$WORKSPACE" \
    "${EXTRA_ARGS[@]}" \
    2>&1 | tee "$LOGFILE"

echo ""
echo "Build log saved to: $LOGFILE"
echo "Artifacts in: $WORKSPACE/${ANDROID_VERSION}-${KERNEL_VERSION}-${SUB_LEVEL}/"
