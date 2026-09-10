#!/system/bin/sh
# ath9k_htc for vermeer — loads the driver and provides the firmware.
#
# Why post-fs-data and not service.sh: scripts here run in init's mount
# namespace. The firmware is loaded by ueventd, which lives in that same
# namespace. A bind mount from an ordinary su shell is invisible to
# ueventd — this was verified on the device and is why the manual test
# failed while the file was visible to the shell.
#
# Nothing here touches the vendor wireless stack. cfg80211.ko and
# mac80211.ko come from /vendor/lib/modules and stay theirs; we only add
# the ath*.ko next to them.

MODDIR=${0%/*}
LOG=/data/adb/ath9k_htc_vermeer.log

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# Keep only the latest run so it doesn't grow forever
[ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 65536 ] && rm -f "$LOG"
log "--- post-fs-data ---"

# --- firmware ----------------------------------------------------------
# The file is served via Magic Mount from system/vendor/firmware/. Here we
# only fix the SELinux label: ueventd reads in Enforcing mode and expects
# vendor_firmware_file. The bind mount shares the inode, so the label on
# the file inside the module also applies to the path visible under
# /vendor.
FW="$MODDIR/system/vendor/firmware/ath9k_htc/htc_9271-1.4.0.fw"
if [ -f "$FW" ]; then
	chcon u:object_r:vendor_firmware_file:s0 "$FW" 2>/dev/null \
		&& log "firmware: label applied" \
		|| log "firmware: chcon failed (ueventd may reject it)"
	chcon u:object_r:vendor_firmware_file:s0 \
		"$MODDIR/system/vendor/firmware/ath9k_htc" 2>/dev/null
else
	log "ERROR: missing $FW — the adapter won't initialize"
fi

# --- modules -----------------------------------------------------------
# The order is mandatory: ath9k_htc depends on the other three.
# ath9k_hw loads before ath9k_common because common links against it.
for m in ath ath9k_hw ath9k_common ath9k_htc; do
	if lsmod | grep -q "^$m "; then
		log "$m: already loaded, skipping"
		continue
	fi
	KO="$MODDIR/modules/$m.ko"
	if [ ! -f "$KO" ]; then
		log "ERROR: missing $KO"
		break
	fi
	if insmod "$KO" 2>>"$LOG"; then
		log "$m: loaded"
	else
		# Stop on the first failure — the ones after it depend on it
		# and would give misleading errors.
		log "ERROR: insmod $m failed, stopping (see dmesg)"
		break
	fi
done

log "done: $(lsmod | grep -c '^ath') ath modules loaded"
