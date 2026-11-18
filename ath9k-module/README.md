# ath9k_htc module for vermeer

A SukiSU-Ultra / KernelSU module that makes a **TP-Link TL-WN722N v1**
(Atheros AR9271) USB adapter work over OTG on the Poco F6 Pro / Redmi K70
(`vermeer`) - monitor mode and packet injection, with the phone's built-in
WiFi still working.

This folder holds the **source** of the module. The ready-to-flash
`ath9k_htc_vermeer-<kernel version>.zip` (e.g. `ath9k_htc_vermeer-5.15.225.zip`)
is attached to each kernel release. Use the one with the same version as the
kernel you flash - modules from another kernel version will not load.

## What it does

- Loads `ath`, `ath9k_hw`, `ath9k_common` and `ath9k_htc` at boot, in the
  right dependency order.
- Provides `htc_9271-1.4.0.fw` at `/vendor/firmware/ath9k_htc/` via Magic
  Mount - only that one file is overlaid, the vendor's own firmware files
  are left untouched.
- Refuses to install on a kernel that wasn't built with `CONFIG_ATH9K_HTC=m`,
  so it fails loudly at install time instead of silently after a reboot.

The wireless stack itself (`cfg80211`, `mac80211`) is **not** included - the
device loads Qualcomm's from `/vendor/lib/modules`, and the `ath*.ko` here
link against it. A second copy would break internal WiFi.

## Requirements

- This kernel, built with ath9k support (`--ath9k`, on by default in
  `build-kernel.sh`). The four `ath*.ko` come out of that build.
- A **TL-WN722N v1** specifically. The v2 and v3 are a Realtek chip and will
  not work with this driver.

## Building the flashable zip

`build-kernel.sh` packs it by itself after a successful build and puts it in
`~/gki-workspace/<android>-<kernel>-<sub_level>/`, next to the boot image and
the AnyKernel3 zip. The GitHub Actions builds attach it to their artifacts.

By hand, from this folder:

```
./make-ath9k-module.sh
```

With no arguments it takes the newest `~/gki-workspace/android13-5.15-*/ath9k-modules`.
It packs the four `ath*.ko`, the firmware from the repo and everything in
`ath9k-htc-vermeer/` into `ath9k_htc_vermeer-<kernel version>.zip`. The kernel
version is read from the modules' vermagic; the build stops if the four modules
are not from the same kernel, or if they don't match the build folder's
sub_level. It refuses to include `cfg80211.ko` / `mac80211.ko`.

Custom paths, if your build output lives elsewhere:

```
./make-ath9k-module.sh /path/to/ath9k-modules /path/to/htc_9271-1.4.0.fw
```

## Installing

1. Flash the kernel (built with ath9k).
2. Install `ath9k_htc_vermeer-<kernel version>.zip` from your root manager.
3. Reboot, plug the antenna in over OTG.

Check it loaded:

```
su -c 'lsmod | grep ath'                       # 4 ath modules
su -c 'cat /data/adb/ath9k_htc_vermeer.log'    # ends "4 ath modules loaded"
```

Then, with the antenna plugged in, put it into monitor mode:

```
ip link set wlan1 down
iw dev wlan1 set type monitor
ip link set wlan1 up
```

## Notes

- **The antenna LED will not light.** This is intentional. The LED needs
  `MAC80211_LEDS`, which pulls in LED-trigger symbols the vendor `mac80211`
  doesn't export; enabling it would stop the driver from loading at all. So
  the LED support is dropped - everything else works. Verify with `iw dev`
  or `airodump-ng`, not the LED.
- Intended for WiFi security testing (monitor, capture, injection) on **your
  own networks** - standard Kali / NetHunter workflows, on the phone over OTG.

## Files

- `ath9k-htc-vermeer/module.prop` - module manifest
- `ath9k-htc-vermeer/banner.png` - banner shown in the root manager
- `ath9k-htc-vermeer/customize.sh` - install-time kernel check
- `ath9k-htc-vermeer/post-fs-data.sh` - loads modules + labels firmware at boot
- `ath9k-htc-vermeer/uninstall.sh` - cleanup on removal
- `make-ath9k-module.sh` - packages the flashable zip from a kernel build
