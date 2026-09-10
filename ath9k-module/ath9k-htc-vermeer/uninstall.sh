#!/system/bin/sh
# Runs when the module is removed. The module's own folder (firmware +
# .ko files) is deleted by the root manager automatically; this only
# cleans up what post-fs-data.sh created OUTSIDE that folder.
#
# The loaded modules are not force-removed here. rmmod can fail or hang
# if something still holds them (a monitor interface, an active capture),
# and a failed rmmod at uninstall time is worse than leaving them: on the
# next reboot nothing loads them anyway, so they're gone cleanly then.
# Removal is best-effort and only attempted in reverse dependency order.

LOG=/data/adb/ath9k_htc_vermeer.log

# Best-effort unload, reverse order. Ignore failures — a busy module just
# stays until reboot, which is harmless.
for m in ath9k_htc ath9k_common ath9k_hw ath; do
	if lsmod | grep -q "^$m "; then
		rmmod "$m" 2>/dev/null
	fi
done

# Remove the stray log this module wrote outside its own folder.
rm -f "$LOG"
