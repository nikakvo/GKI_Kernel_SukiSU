# GKI SukiSU-Ultra + SUSFS — For Poco F6 Pro

A custom GKI kernel for the **Poco F6 Pro / Redmi K70**, built on Google's
`android13-5.15` Generic Kernel Image with **SukiSU-Ultra** root, **SUSFS**
hiding, and a set of networking and performance options that stock GKI
leaves switched off.

Built and tested on one device by one person. Nothing here is theoretical —
every feature listed below has been verified on a running Poco F6 Pro, and
this page tells you the exact command to check each one yourself.

---

## Compatibility

| | |
|---|---|
| **Device** | Poco F6 Pro / Redmi K70 |
| **SoC** | Snapdragon 8 Gen 2 |
| **Kernel base** | `android13-5.15` GKI, LTS respin |
| **Root** | SukiSU-Ultra (KernelSU-based) |
| **LTO** | Full |

**Your Android version does not matter.** What matters is the GKI base your
ROM ships, and the Poco F6 Pro uses `android13-5.15` regardless of whether
the userspace is Android 13, 14, 15 or 16. This has been running on HyperOS
releases across several Android versions. If you are looking for an
"android13 kernel", this is it — the name refers to the GKI branch, not to
the Android release you are on.

This is a **GKI** kernel. It replaces the boot image only — your vendor
partitions, modules and firmware are untouched. It will not work on devices
that do not ship an `android13-5.15` GKI kernel.

> **Before you flash:** back up your current `boot.img`. If something goes
> wrong, restoring it is the whole recovery plan.

---

## Download

Each release contains two files. You want **one** of them:

| File | Use it when |
|---|---|
| `...-AnyKernel3-....zip` | You already have root. Flash from the SukiSU-Ultra or Magisk app, or from a custom recovery. Easiest option. |
| `...-boot.img` | You are rooting for the first time, or recovering from a bad flash. Flash with fastboot. |

Filenames look like this:

```
android13-5.15.211-2026-06-lto-full-r00-lts-boot.img
android13-5.15.211-2026-06-lto-full-AnyKernel3-r00-lts.zip
```

Read as: GKI branch, kernel sublevel, security patch level, LTO mode, kernel
respin, and `lts` if built from the LTS-merge tag rather than the date-based
one.

**Match the sublevel to your ROM.** Check which GKI sublevel you are on:

```bash
su -c 'zcat /proc/config.gz | grep CONFIG_LOCALVERSION'
```

A `5.15.211` kernel is intended for a ROM shipping around that sublevel.
Flashing a wildly different one usually still boots — that is the point of
GKI — but is not what this was tested against.

Note that `uname -r` is not a reliable check on a kernel that is already
running this build, because SUSFS spoofs it. On a stock kernel it works
fine.

---

## Installing

### With AnyKernel3 (already rooted)

1. Open the SukiSU-Ultra app → **Install from storage** → pick the `.zip`
2. Reboot

Or from recovery: **Install** → pick the `.zip` → reboot.

### With fastboot (first-time root, or recovery)

Your bootloader must already be unlocked.

```bash
adb reboot bootloader
fastboot flash boot_ab android13-5.15.211-2026-06-lto-full-r00-lts-boot.img
fastboot reboot
```

To try it without committing — this does not write anything, and a reboot
puts you back on your old kernel:

```bash
fastboot boot android13-5.15.211-2026-06-lto-full-r00-lts-boot.img
```

Afterwards, install the [SukiSU-Ultra manager app](https://github.com/SukiSU-Ultra/SukiSU-Ultra/releases)
if you do not have it yet.

### If it does not boot

Reflash your backed-up `boot.img` from fastboot. GKI kernels do not touch
your data, so nothing is lost.

---

## Features

- **SukiSU-Ultra** root with KPM (Kernel Patch Module) support
- **SUSFS v2.3.0** — mount, path, kstat and map hiding; uname and cmdline spoofing
- **BBRv3** congestion control, default — plus BBR, CUBIC, BIC, HTCP, Westwood
- **CAKE**, FQ and FQ-CoDel queueing disciplines
- **nftables** with NAT, connlimit, socket and tproxy support
- **IPv6 NAT** — `ip6tables` nat table with MASQUERADE
- **ipset** — all 18 set types, 65534 set limit
- **NTSync** — Wine/Proton synchronisation primitives for Winlator
- **ZRAM** with LZ4KD and LZ4K-Oplus compression
- **Droidspaces** — SysV IPC, POSIX message queues and IPC namespaces
- **Baseband-guard** — modem partition write protection
- **MGLRU** and **PSI** memory management
- **WireGuard**, **CIFS**, **FUSE-BPF**, **BTF/eBPF**
- **ptrace leak fix** and **unicode bypass fix**
- KMI-safe: no `__GENKSYMS__` tricks, no reserve-slot guessing

---

## Detailed explanation

Everything below is checkable on your own device. Most checks read
`/proc/config.gz`, which is the configuration the running kernel was
actually built with — not a claim, the real thing.

Start here to see the whole picture:

```bash
su -c 'zcat /proc/config.gz' > /sdcard/kernel-config.txt
```

### Root and hiding

**SukiSU-Ultra** is a KernelSU fork with KPM support, which lets kernel-side
patch modules load at runtime. **SUSFS** is a separate project that hides
root traces from apps — it hides mounts, paths, file stats and memory maps,
and spoofs `uname` and kernel cmdline.

```bash
su -c 'zcat /proc/config.gz | grep -E "^CONFIG_KSU"'
```

You should see `CONFIG_KSU=y`, `CONFIG_KPM=y`, `CONFIG_KSU_SUSFS=y` and a
list of `CONFIG_KSU_SUSFS_*` options.

`CONFIG_KSU_SUSFS_SUS_SU=n` is deliberate — that mode is legacy and the
kernel-hook approach is used instead.

> **Note on `uname`:** `CONFIG_KSU_SUSFS_SPOOF_UNAME=y` means `uname -r`
> reports a spoofed string, not the real kernel version. This is the
> feature working as intended. To see the real build, use the config check
> above or look in the SukiSU-Ultra app.

### BBRv3 congestion control

BBRv3 is Google's third-generation TCP congestion control. Compared to
CUBIC it generally holds higher throughput on lossy mobile links, and
compared to BBRv1 it is less aggressive toward competing flows.

```bash
su -c 'cat /proc/sys/net/ipv4/tcp_available_congestion_control'
su -c 'cat /proc/sys/net/ipv4/tcp_congestion_control'
```

The first lists everything built in; the second shows the active one, which
should be `bbr3`. To switch temporarily:

```bash
su -c 'sysctl -w net.ipv4.tcp_congestion_control=cubic'
```

### Queueing disciplines — CAKE

CAKE combines fair queueing with active queue management and shaping in one
qdisc. Useful for reducing bufferbloat on a tethered connection.

```bash
su -c 'zcat /proc/config.gz | grep -E "NET_SCH_CAKE|NET_SCH_FQ"'
su -c 'tc qdisc show'
```

### nftables

nftables is the modern replacement for iptables. It is enabled here
**alongside** iptables, not instead of it — Android's `netd` keeps using the
legacy xtables path untouched.

The practical reason to want it: current Debian and Kali ship
`/usr/sbin/iptables` as `xtables-nft-multi`, so `iptables` commands inside a
chroot are translated to nftables and fail outright on a kernel without it.

```bash
su -c 'zcat /proc/config.gz | grep -E "^CONFIG_NF_TABLES|^CONFIG_NFT_"'
```

Android does not ship an `nft` binary, so `nft` will report "not found" from
a normal shell. That is a missing userspace tool, not a missing kernel
feature. Inside a Debian or Kali chroot:

```bash
apt install nftables
nft list ruleset          # empty output with exit 0 = working
iptables -L               # this is iptables-nft; it would fail without NF_TABLES
```

`# Warning: iptables-legacy tables present` is expected and good — it means
Android's own rules are alive in the legacy backend while nftables runs
alongside. To read Android's actual rules, use `iptables-legacy -L -n -v`.

> **Careful:** a chroot shares the phone's network namespace. Rules you add
> inside Kali apply to the **whole device**, not just the chroot. Work in
> your own named table so you can remove it cleanly:
> ```bash
> nft add table inet mytest
> nft delete table inet mytest
> ```

`CONFIG_NF_TABLES_ARP` and `CONFIG_NF_TABLES_BRIDGE` are intentionally off —
neither the arp nor the bridge family is useful on a phone with no bridge
interfaces.

### IPv6 NAT

Stock GKI omits the `ip6tables` nat table because Android does NAT64/464XLAT
through `clatd` and never needs NAT66. It is useful for routing and
tethering setups.

```bash
su -c 'ip6tables -t nat -L'
```

Listing the four chains means it works. On a kernel without it, this errors
out.

### ipset

All 18 set types are built, including the MAC-keyed ones stock GKI omits,
with the set limit raised from 256 to 65534.

```bash
su -c 'zcat /proc/config.gz | grep -E "^CONFIG_IP_SET"'
```

### NTSync

NTSync exposes Windows-style synchronisation primitives to userspace, which
Wine and Proton use instead of emulating them. Relevant if you run Winlator.

```bash
ls -l /dev/ntsync
su -c 'zcat /proc/config.gz | grep NTSYNC'
```

### ZRAM with LZ4KD

LZ4KD and LZ4K-Oplus are compression algorithms tuned for mobile ZRAM —
better ratio than plain LZ4 at similar speed.

```bash
su -c 'cat /sys/block/zram0/comp_algorithm'
su -c 'cat /sys/block/zram0/mm_stat'
```

The active algorithm appears in brackets. Switching requires resetting the
ZRAM device, so it is not something to change on a live system casually.

### Droidspaces

Enables SysV IPC, POSIX message queues and IPC namespaces — needed by
container and virtualisation tooling that expects a normal Linux IPC surface.

Android's GKI leaves these off, and turning them on is not trivial: the
structures involved are kABI-tracked, so the fields have to be placed in
`ANDROID_KABI_RESERVE` slots. Which slots are free differs per respin, so
the build tries three variants and uses whichever fits the exact source
tree. This one used the 6/7/8 slot variant.

```bash
su -c 'ipcs -a'
su -c 'zcat /proc/config.gz | grep -E "SYSVIPC|POSIX_MQUEUE|IPC_NS"'
```

### Baseband-guard

An LSM that blocks writes to modem and bootloader-related partitions, so a
misbehaving root app cannot brick the radio.

```bash
su -c 'zcat /proc/config.gz | grep CONFIG_BBG'
su -c 'cat /sys/kernel/security/lsm'
```

`baseband_guard` should appear in the LSM list.

### Memory management — MGLRU and PSI

MGLRU is a rewritten page reclaim algorithm that generally improves
responsiveness under memory pressure. PSI exposes stall metrics that
userspace daemons use to make eviction decisions.

```bash
su -c 'cat /sys/kernel/mm/lru_gen/enabled'
su -c 'cat /proc/pressure/memory'
```

### WireGuard, CIFS, FUSE-BPF

WireGuard in-kernel means VPN apps use the kernel implementation instead of
the slower userspace one. CIFS lets you mount SMB shares directly. FUSE-BPF
speeds up FUSE filesystem operations, which Android uses heavily for
`/storage`.

```bash
su -c 'zcat /proc/config.gz | grep -E "WIREGUARD|^CONFIG_CIFS|FUSE_BPF"'
```

### KMI safety

Google's GKI enforces a stable kernel module interface so vendor modules keep
loading. Some kernels work around this by hiding new struct fields from the
checksum tool with `#ifndef __GENKSYMS__` — which makes the checksum match
while the actual struct layout still shifts underneath vendor modules that
were compiled against the old one.

That approach was tried here and produced a confirmed bootloop on a real
device. It is not used. Fields go in real reserve slots or the feature does
not ship.

---

## Check everything at once

```bash
su -c 'zcat /proc/config.gz' | grep -E "^CONFIG_(KSU|KPM|NF_TABLES|NFT_|IP6_NF_NAT|IP_SET|NTSYNC|ZRAM|CRYPTO_LZ4K|SYSVIPC|IPC_NS|POSIX_MQUEUE|BBG|LRU_GEN|PSI|WIREGUARD|CIFS|FUSE_BPF|TCP_CONG|NET_SCH)"
```

---

## Build it yourself

Everything needed is in this repository. Builds run on Linux or WSL2.

Full LTO peaks around **27 GB of RAM**, which is why these are built locally
rather than in CI — GitHub runners cannot fit it. Thin LTO builds fine on
less.

```bash
git clone https://github.com/nikakvo/GKI_Kernel_SukiSU
cd GKI_Kernel_SukiSU
./build-kernel.sh --ksu-commit v4.2.0 --susfs-commit <ref>
```

Both pins matter. `susfs4ksu` evolves independently of any SukiSU-Ultra
release, so an unpinned build that worked yesterday can fail tomorrow with
rejected hunks. Pin them together.

**susfs pins are per-branch.** `susfs4ksu` keeps a separate branch for each
GKI version, and each holds only its own `50_add_susfs_in_gki-*.patch`, so a
commit hash is valid for exactly one branch. The build refuses a mismatched
pin rather than checking out the wrong tree.

To see whether it is worth moving your pin forward:

```bash
cd .github/workflows/scripts
python3 check_susfs.py --pin 'gki-android13-5.15=<your-ref>'
```

That reports what landed upstream since your pin and, more usefully,
separates commits touching files this build depends on from the ones that do
not. Only two files can actually break a build:
`kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch` and the per-branch
`50_add_susfs_in_gki-*.patch`.

### Build verification

Two checks run after every build, because a kernel that reports success and
silently lacks a feature is worse than one that fails:

- **Object verification** — confirms the expected `.o` files were actually
  compiled. `CONFIG_KSU_SUSFS=y` once sat in the defconfig for a long time
  while `fs/susfs.o` was never built.
- **Effective config verification** — reads the `.config` the build
  *produced* and compares it against everything requested. Kconfig drops
  undefined symbols and unmet dependencies in complete silence, so reading
  the defconfig back proves nothing.

Results land in `PATCH_STATUS.json` and `BUILD_REPORT.txt` next to the
artifacts.

---

## Credits

- [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) — root implementation
- [susfs4ksu](https://github.com/ShirkNeko/susfs4ksu) (ShirkNeko) — SUSFS
- [WildKernels](https://github.com/WildKernels) — BBRv3 backport, Droidspaces kABI patches, NTSync compat patches
- [AnyKernel3](https://github.com/osm0sis/AnyKernel3) (osm0sis) — flashable zip framework
- Google — the GKI kernel itself

## License

The kernel is GPLv2, as is everything derived from it. The patches in
`.github/workflows/scripts/patches/` and the configuration used to build
these releases are kept in this repository so the binaries above have their
corresponding source available.

Build scripts are provided as-is. Flashing custom kernels carries risk —
back up your boot image.
