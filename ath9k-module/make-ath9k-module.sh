#!/usr/bin/env bash
# Сглобява SukiSU/KernelSU модула от резултатите на билда.
# Пуска се в WSL, от папката, в която е разархивиран ath9k-htc-vermeer/.
#
# Без аргументи засича сам най-новия билд в ~/gki-workspace/android13-5.15-*/
# (по най-високо субниво). С аргумент ползва подадената папка с модули:
#   ./make-ath9k-module.sh
#   ./make-ath9k-module.sh ~/gki-workspace/android13-5.15-217/ath9k-modules
set -euo pipefail

WORKSPACE="$HOME/gki-workspace"
FW_SRC="${2:-$HOME/GKI_KernelSU_SUSFS-main/.github/workflows/scripts/firmware/ath9k_htc/htc_9271-1.4.0.fw}"
SRC=ath9k-htc-vermeer

if [ -n "${1:-}" ]; then
	MODULES_DIR="$1"
else
	MODULES_DIR=$(ls -d "$WORKSPACE"/android13-5.15-*/ath9k-modules 2>/dev/null | sort -V | tail -n1 || true)
	[ -n "$MODULES_DIR" ] || { echo "няма нито един android13-5.15-*/ath9k-modules в $WORKSPACE"; exit 1; }
fi

[ -d "$SRC" ] || { echo "липсва папката $SRC (разархивирай я тук)"; exit 1; }
[ -d "$MODULES_DIR" ] || { echo "липсва $MODULES_DIR"; exit 1; }
[ -f "$FW_SRC" ] || { echo "липсва фърмуерът: $FW_SRC"; exit 1; }

# Субнивото от името на папката (android13-5.15-217 -> 217).
BUILD_NAME=$(basename "$(dirname "$MODULES_DIR")")
SUBLEVEL=${BUILD_NAME##*-}

# vermagic от самия .ko - с modinfo, ако го има, иначе направо от файла.
get_vermagic() {
	if command -v modinfo >/dev/null 2>&1; then
		modinfo -F vermagic "$1"
	else
		strings "$1" | sed -n 's/^vermagic=//p' | head -n1
	fi
}

echo "билд:     $BUILD_NAME"
echo "модули:   $MODULES_DIR"

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
cp -a "$SRC/." "$BUILD/"

mkdir -p "$BUILD/modules" "$BUILD/system/vendor/firmware/ath9k_htc"

# Само четирите ath модула. cfg80211.ko и mac80211.ko НЕ се пакетират -
# устройството зарежда вендорските от /vendor/lib/modules, а втори
# екземпляр до тях е точно това, което уби вътрешния WiFi при първия опит.
# Всеки модул трябва да е от същото субниво, иначе kernel-ът го отказва.
for m in ath ath9k_hw ath9k_common ath9k_htc; do
	ko="$MODULES_DIR/$m.ko"
	[ -f "$ko" ] || { echo "липсва $m.ko в $MODULES_DIR"; exit 1; }
	vm=$(get_vermagic "$ko")
	case "$vm" in
		5.15."$SUBLEVEL"-*) ;;
		*) echo "ГРЕШКА: $m.ko е с vermagic '$vm', а билдът е 5.15.$SUBLEVEL"; exit 1 ;;
	esac
	cp "$ko" "$BUILD/modules/"
done
echo "vermagic: $vm"
for bad in cfg80211.ko mac80211.ko; do
	[ -f "$BUILD/modules/$bad" ] && { echo "ГРЕШКА: $bad не бива да е тук"; exit 1; }
done

cp "$FW_SRC" "$BUILD/system/vendor/firmware/ath9k_htc/"

# Дребно, но спестява час дебъгване: CRLF в post-fs-data.sh кара
# /system/bin/sh да се провали с неясна грешка.
find "$BUILD" -name '*.sh' -exec sed -i 's/\r$//' {} +
chmod 0755 "$BUILD"/*.sh

# Субнивото е в името на zip-а, за да не се объркат модули от различни kernel-и.
OUT="ath9k_htc_vermeer-5.15.$SUBLEVEL.zip"
rm -f "$OUT"
( cd "$BUILD" && zip -qr - . ) > "$OUT"

echo
echo "готово: $OUT"
unzip -l "$OUT" | tail -n +4 | head -20
echo
echo "размер: $(du -h "$OUT" | cut -f1)"
