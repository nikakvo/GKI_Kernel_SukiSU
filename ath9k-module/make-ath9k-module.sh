#!/usr/bin/env bash
# Сглобява SukiSU/KernelSU модула от резултатите на билда.
# Пуска се в WSL, от папката, в която е разархивиран ath9k-htc-vermeer/.
set -euo pipefail

MODULES_DIR="${1:-$HOME/gki-workspace/android13-5.15-211/ath9k-modules}"
FW_SRC="${2:-$HOME/GKI_KernelSU_SUSFS-main/.github/workflows/scripts/firmware/ath9k_htc/htc_9271-1.4.0.fw}"
SRC=ath9k-htc-vermeer
OUT=ath9k_htc_vermeer.zip

[ -d "$SRC" ] || { echo "липсва папката $SRC (разархивирай я тук)"; exit 1; }
[ -d "$MODULES_DIR" ] || { echo "липсва $MODULES_DIR"; exit 1; }
[ -f "$FW_SRC" ] || { echo "липсва фърмуерът: $FW_SRC"; exit 1; }

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
cp -a "$SRC/." "$BUILD/"

mkdir -p "$BUILD/modules" "$BUILD/system/vendor/firmware/ath9k_htc"

# Само четирите ath модула. cfg80211.ko и mac80211.ko НЕ се пакетират —
# устройството зарежда вендорските от /vendor/lib/modules, а втори
# екземпляр до тях е точно това, което уби вътрешния WiFi при първия опит.
for m in ath ath9k_hw ath9k_common ath9k_htc; do
	[ -f "$MODULES_DIR/$m.ko" ] || { echo "липсва $m.ko в $MODULES_DIR"; exit 1; }
	cp "$MODULES_DIR/$m.ko" "$BUILD/modules/"
done
for bad in cfg80211.ko mac80211.ko; do
	[ -f "$BUILD/modules/$bad" ] && { echo "ГРЕШКА: $bad не бива да е тук"; exit 1; }
done

cp "$FW_SRC" "$BUILD/system/vendor/firmware/ath9k_htc/"

# Дребно, но спестява час дебъгване: CRLF в post-fs-data.sh кара
# /system/bin/sh да се провали с неясна грешка.
find "$BUILD" -name '*.sh' -exec sed -i 's/\r$//' {} +
chmod 0755 "$BUILD"/*.sh

rm -f "$OUT"
( cd "$BUILD" && zip -qr - . ) > "$OUT"

echo "готово: $OUT"
unzip -l "$OUT" | tail -n +4 | head -20
echo
echo "размер: $(du -h "$OUT" | cut -f1)"
