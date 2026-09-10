#!/system/bin/sh
# Install-time check. The modules are compiled against a specific kernel
# and rely on symbols GKI trims when nothing uses them
# (CONFIG_TRIM_UNUSED_KSYMS). On a kernel without ath9k they won't load —
# better to find out now than after a reboot.

SKIPUNZIP=0

ui_print ""
ui_print "  ath9k_htc (AR9271) for vermeer"
ui_print "  kernel: $(uname -r)"
ui_print ""

if [ ! -f /proc/config.gz ]; then
	ui_print "  ! /proc/config.gz missing — can't verify the kernel."
	ui_print "  ! Continuing, but if the modules don't load, start here."
elif zcat /proc/config.gz | grep -q "^CONFIG_ATH9K_HTC=m"; then
	ui_print "  + kernel has CONFIG_ATH9K_HTC=m"
else
	ui_print "  ! This kernel does NOT have CONFIG_ATH9K_HTC=m."
	ui_print "  !"
	ui_print "  ! Without it, usb_get_urb and rfkill_pause_polling are"
	ui_print "  ! missing too — GKI trims them when no in-tree module uses"
	ui_print "  ! them. ath9k_htc.ko will fail to load."
	ui_print "  !"
	ui_print "  ! Flash a kernel built with --ath9k, then install again."
	abort "  ! Aborting."
fi

# The vendor stack must be present — the modules link against it.
if [ -f /vendor/lib/modules/cfg80211.ko ] || lsmod | grep -q "^cfg80211 "; then
	ui_print "  + vendor wireless stack found"
else
	ui_print "  ! cfg80211 not found, neither loaded nor in /vendor/lib/modules."
	ui_print "  ! The modules link against it; without it they won't work."
	abort "  ! Aborting."
fi

set_perm_recursive "$MODPATH" 0 0 0755 0644
set_perm "$MODPATH/post-fs-data.sh" 0 0 0755

ui_print ""
ui_print "  Done. Reboot, plug in the antenna, then:"
ui_print "    ip link set wlan1 down"
ui_print "    iw dev wlan1 set type monitor"
ui_print "    ip link set wlan1 up"
ui_print ""
