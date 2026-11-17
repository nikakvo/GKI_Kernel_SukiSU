#!/usr/bin/env bash
# Packs the ath9k_htc flashable root module (SukiSU / KernelSU) for vermeer
# from a kernel build: the four ath*.ko + the AR9271 firmware + the module
# source in ath9k-htc-vermeer/ (module.prop, banner.png, scripts).
#
# build-kernel.sh and the GitHub Actions workflows run it after a successful
# build. By hand, from this folder (ath9k-module/):
#   ./make-ath9k-module.sh                       # newest ~/gki-workspace/android13-5.15-*/ath9k-modules
#   ./make-ath9k-module.sh ~/gki-workspace/android13-5.15-223/ath9k-modules
#   ./make-ath9k-module.sh <ath9k-modules dir> <htc_9271-1.4.0.fw>
#
# Output: ath9k_htc_vermeer-<kernel version>.zip (e.g. ath9k_htc_vermeer-5.15.223.zip)
# in the current folder.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$HOME/gki-workspace"
SRC="$SCRIPT_DIR/ath9k-htc-vermeer"
FW_SRC="${2:-$SCRIPT_DIR/../.github/workflows/scripts/firmware/ath9k_htc/htc_9271-1.4.0.fw}"

if [ -n "${1:-}" ]; then
	MODULES_DIR="$1"
else
	MODULES_DIR=$(ls -d "$WORKSPACE"/android13-5.15-*/ath9k-modules 2>/dev/null | sort -V | tail -n1 || true)
	[ -n "$MODULES_DIR" ] || { echo "no android13-5.15-*/ath9k-modules found in $WORKSPACE"; exit 1; }
fi

[ -d "$SRC" ] || { echo "missing module source: $SRC"; exit 1; }
[ -f "$SRC/module.prop" ] || { echo "missing $SRC/module.prop"; exit 1; }
[ -d "$MODULES_DIR" ] || { echo "missing $MODULES_DIR"; exit 1; }
[ -f "$FW_SRC" ] || { echo "missing firmware: $FW_SRC"; exit 1; }

# vermagic straight from the .ko - modinfo if it is there, otherwise from the file.
get_vermagic() {
	if command -v modinfo >/dev/null 2>&1; then
		modinfo -F vermagic "$1"
	else
		strings "$1" | sed -n 's/^vermagic=//p' | head -n1
	fi
}

# The kernel version comes from ath.ko itself (5.15.223-android13-... -> 5.15.223),
# so the zip name is right whatever the build folder is called.
[ -f "$MODULES_DIR/ath.ko" ] || { echo "missing ath.ko in $MODULES_DIR"; exit 1; }
VERMAGIC=$(get_vermagic "$MODULES_DIR/ath.ko")
KVER=${VERMAGIC%%-*}
KVER=${KVER%% *}
[[ "$KVER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "ERROR: cannot read the kernel version from ath.ko vermagic '$VERMAGIC'"; exit 1; }

# If the build folder is named <android>-<kernel>-<sublevel> (android13-5.15-223),
# it must be the same kernel as the modules.
BUILD_NAME=$(basename "$(dirname "$MODULES_DIR")")
DIR_SUB=${BUILD_NAME##*-}
if [[ "$DIR_SUB" =~ ^[0-9]+$ ]] && [ "$DIR_SUB" != "${KVER##*.}" ]; then
	echo "ERROR: build folder is $BUILD_NAME but ath.ko is $KVER"
	exit 1
fi

echo "build:    $BUILD_NAME"
echo "modules:  $MODULES_DIR"
echo "kernel:   $KVER"

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
cp -a "$SRC/." "$BUILD/"

mkdir -p "$BUILD/modules" "$BUILD/system/vendor/firmware/ath9k_htc"

# Only the four ath modules. cfg80211.ko and mac80211.ko are NOT packed -
# the phone loads the vendor ones from /vendor/lib/modules, and a second
# copy next to them is exactly what killed internal WiFi the first time.
# Every module must be from the same kernel, or the kernel refuses it.
for m in ath ath9k_hw ath9k_common ath9k_htc; do
	ko="$MODULES_DIR/$m.ko"
	[ -f "$ko" ] || { echo "missing $m.ko in $MODULES_DIR"; exit 1; }
	vm=$(get_vermagic "$ko")
	case "$vm" in
		"$KVER"-*) ;;
		*) echo "ERROR: $m.ko has vermagic '$vm', ath.ko is $KVER"; exit 1 ;;
	esac
	cp "$ko" "$BUILD/modules/"
done
echo "vermagic: $VERMAGIC"
for bad in cfg80211.ko mac80211.ko; do
	[ -f "$BUILD/modules/$bad" ] && { echo "ERROR: $bad must not be in the module"; exit 1; }
done

cp "$FW_SRC" "$BUILD/system/vendor/firmware/ath9k_htc/"

# CRLF in post-fs-data.sh makes /system/bin/sh fail with an unclear error.
find "$BUILD" -name '*.sh' -exec sed -i 's/\r$//' {} +
chmod 0755 "$BUILD"/*.sh
[ -f "$BUILD/banner.png" ] && chmod 0644 "$BUILD/banner.png"

# The kernel version is in the zip name, so modules from different kernels
# can't be mixed up.
OUT="ath9k_htc_vermeer-$KVER.zip"
rm -f "$OUT"
( cd "$BUILD" && zip -qr - . ) > "$OUT"

echo
echo "done: $OUT"
unzip -l "$OUT" | tail -n +4 | head -20
echo
echo "size: $(du -h "$OUT" | cut -f1)"
