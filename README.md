# GKI SukiSU-Ultra + SUSFS — For Poco F6 Pro

A custom GKI kernel for the **Poco F6 Pro / Redmi K70**, built on Google's
`android13-5.15` Generic Kernel Image with **SukiSU-Ultra** root, **SUSFS**
hiding, and a set of networking and performance options that stock GKI
leaves switched off.

Built and tested on one device by one person. Every command in the
**Detailed explanation** section below was run on a real Poco F6 Pro running
this kernel — the expected output is what the device actually printed, not
what it should print in theory.

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
the userspace is Android 13, 14, 15 or 16. If you are looking for an
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

**Match the sublevel to your ROM.** To see which GKI sublevel you are on:

```bash
su -c 'zcat /proc/config.gz | grep CONFIG_LOCALVERSION'
```

A `5.15.211` kernel is intended for a ROM shipping around that sublevel.
Flashing a wildly different one usually still boots — that is the point of
GKI — but is not what this was tested against.

Note that `uname -r` is **not** a reliable check once this kernel is
running, because SUSFS spoofs it. On a stock kernel it works fine.

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

`boot_ab` — not `boot` — writes the image to **both** A and B slots. This
device is A/B, and flashing only the active slot leaves the other one on the
stock kernel, which becomes a problem the moment an OTA switches slots.

To try it without committing — this writes nothing, and a reboot puts you
back on your old kernel:

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
- **Magic Mount** support
- **BBRv3** congestion control, default — plus BBR, CUBIC, BIC, HTCP, Westwood
- **CAKE**, FQ and FQ-CoDel queueing disciplines
- **nftables** with NAT, connlimit, socket and tproxy support
- **IPv6 NAT** — `ip6tables` nat table with MASQUERADE
- **ipset** — all 18 set types, 65534 set limit
- **TTL / Hop-Limit** and **connmark** netfilter targets
- **NTSync** — Wine/Proton synchronisation primitives for Winlator
- **ZRAM** with LZ4KD compression (LZ4K, LZ4K-Oplus and zstd also available)
- **Droidspaces** — SysV IPC, POSIX message queues and IPC namespaces
- **Baseband-guard** — modem partition write protection
- **MGLRU** and **PSI** memory management
- **WireGuard**, **CIFS/SMB** with POSIX extensions, **FUSE-BPF**, **BTF/eBPF**
- **ptrace leak fix** and **unicode bypass fix**
- **Full LTO**
- KMI-safe: no `__GENKSYMS__` tricks, no reserve-slot guessing

---

## Detailed explanation

Every check below has been run on the device. Where a command needs a
userspace tool Android does not ship, that is called out rather than left
for you to discover.

To dump the whole configuration first:

```bash
su -c 'zcat /proc/config.gz' > /sdcard/kernel-config.txt
```

### SukiSU-Ultra and KPM

SukiSU-Ultra is a KernelSU fork with KPM support, which lets kernel-side
patch modules load at runtime.

```bash
su -c 'zcat /proc/config.gz | grep -E "^CONFIG_(KSU|KPM)"'
```

Expected — note `CONFIG_KPM` needs the `KPM` alternative in the pattern,
since it does not start with `CONFIG_KSU`:

```
CONFIG_KSU=y
CONFIG_KSU_MANUAL_SU=y
CONFIG_KPM=y
CONFIG_KSU_SUSFS=y
...
```

The SukiSU-Ultra app also shows KPM status on its home screen, which is the
more reliable check — `grep`ping `/proc/kallsyms` for KPM symbols does not
work here, because `CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y` hides them
on purpose.

### SUSFS

SUSFS hides root traces from apps — mounts, paths, file stats and memory
maps — and spoofs `uname` and kernel cmdline.

```bash
su -c 'zcat /proc/config.gz | grep "^CONFIG_KSU_SUSFS"'
```

Nine options, which is the complete set susfs4ksu v2.3.0 defines:

```
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_PATH=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
```

If you have seen older kernels advertise `AUTO_ADD_SUS_BIND_MOUNT`,
`AUTO_ADD_SUS_KSU_DEFAULT_MOUNT`, `TRY_UMOUNT` or
`AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT` — upstream deprecated all four and the
behaviour is now unconditional. They are not missing from this build; the
symbols no longer exist. A build that still lists them in its defconfig is
writing options Kconfig discards without a word.

> **`uname` is spoofed.** `uname -r` reports a fake string by design. Use
> the config dump above, or the SukiSU-Ultra app, to see the real build.

### BBRv3 congestion control

BBRv3 is Google's third-generation TCP congestion control. Compared to
CUBIC it generally holds higher throughput on lossy mobile links, and
compared to BBRv1 it is fairer to competing flows.

```bash
su -c 'cat /proc/sys/net/ipv4/tcp_available_congestion_control'
su -c 'cat /proc/sys/net/ipv4/tcp_congestion_control'
```

```
reno bbr bbr3 bic cubic westwood htcp
bbr3
```

The first line lists everything built in; the second is the active one. To
switch at runtime and switch back:

```bash
su -c 'sysctl -w net.ipv4.tcp_congestion_control=cubic'
su -c 'sysctl -w net.ipv4.tcp_congestion_control=bbr3'
```

Both changes are temporary and reset on reboot. Westwood is worth trying on
lossy links; BBRv3 is the default because it suits most of them.

### CAKE and queueing disciplines

CAKE combines fair queueing, active queue management and shaping in one
qdisc, which reduces bufferbloat under load.

**CAKE is available, not active.** Android attaches `pfifo_fast` and `mq` to
its interfaces, so a plain `tc qdisc show` prints a long list with no `cake`
in it — that is normal and does not mean anything is wrong. To prove the
qdisc works, apply it to the loopback interface and remove it again:

```bash
su -c 'tc qdisc add dev lo root cake && tc qdisc show dev lo && tc qdisc del dev lo root'
```

Seeing a `qdisc cake ...` line means it works. FQ and FQ-CoDel are built in
the same way:

```bash
su -c 'zcat /proc/config.gz | grep -E "NET_SCH_(CAKE|FQ)"'
```

### nftables

nftables is the modern replacement for iptables. It is enabled here
**alongside** iptables, not instead of it — Android's `netd` keeps using the
legacy xtables path untouched.

The practical reason to want it: current Debian and Kali ship
`/usr/sbin/iptables` as `xtables-nft-multi`, so `iptables` commands inside a
chroot are translated to nftables and fail outright on a kernel without it.

```bash
su -c 'zcat /proc/config.gz | grep -E "^CONFIG_(NF_TABLES|NFT_)"'
```

Twenty-three symbols, ending with the IPv4/IPv6 families:

```
CONFIG_NF_TABLES=y
CONFIG_NF_TABLES_INET=y
CONFIG_NFT_COMPAT=y
CONFIG_NFT_NAT=y
CONFIG_NFT_MASQ=y
CONFIG_NFT_SOCKET=y
CONFIG_NFT_TPROXY=y
...
CONFIG_NF_TABLES_IPV4=y
CONFIG_NF_TABLES_IPV6=y
```

**Android ships no `nft` binary**, so `nft` reports "inaccessible or not
found" from a normal shell. That is a missing userspace tool, not a missing
kernel feature. Inside a Debian or Kali chroot:

```bash
apt install nftables
nft list ruleset          # empty output, exit 0 = working
iptables -L               # this is iptables-nft; it fails without NF_TABLES
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
neither family is useful on a phone with no bridge interfaces.

### IPv6 NAT

Stock GKI omits the `ip6tables` nat table because Android does NAT64/464XLAT
through `clatd` and never needs NAT66. It is useful for routing and
tethering setups.

```bash
su -c 'ip6tables -t nat -L'
```

Listing PREROUTING, INPUT, OUTPUT and POSTROUTING means it works. On a
kernel without it, this errors out instead.

### ipset

All 18 set types are built, including the MAC-keyed ones stock GKI omits,
with the set limit raised from the default 256 to 65534.

```bash
su -c 'zcat /proc/config.gz | grep "^CONFIG_IP_SET"'
```

**ipset needs a userspace binary Android does not ship.** Get a static arm64
build from [ipset-arm64](https://github.com/nikakvo/ipset-arm64), then:

```bash
su -c 'ipset create test hash:ip && ipset destroy test'
```

Running without a "Kernel module not found" error means the kernel side is
working.

### TTL / Hop-Limit and connmark

The TTL target lets firewall rules rewrite a packet's TTL (IPv4) or Hop
Limit (IPv6). Carriers often detect tethering by noticing the TTL decrement
that happens when traffic is routed through another device. connmark tags
whole connections rather than individual packets, so later packets in the
same connection can be matched consistently.

```bash
su -c 'iptables -t mangle -A POSTROUTING -j TTL --ttl-set 65 && iptables -t mangle -D POSTROUTING -j TTL --ttl-set 65'
su -c 'iptables -t mangle -A POSTROUTING -j CONNMARK --set-mark 1 && iptables -t mangle -D POSTROUTING -j CONNMARK --set-mark 1'
```

Each pair adds a rule and immediately deletes it. No "No chain/target/match
by that name" error means the target is present.

### NTSync

NTSync exposes Windows-style synchronisation primitives to userspace, which
Wine and Proton use instead of emulating them over futex. Relevant if you
run Winlator.

```bash
ls -l /dev/ntsync
su -c 'zcat /proc/config.gz | grep NTSYNC'
```

```
crw-rw-rw-. 1 root root 10, 127 /dev/ntsync
CONFIG_NTSYNC=y
```

### ZRAM with LZ4KD

LZ4KD and LZ4K-Oplus are compression algorithms tuned for mobile ZRAM —
better ratio than plain LZ4 at similar speed.

```bash
su -c 'cat /sys/block/zram0/comp_algorithm'
su -c 'cat /sys/block/zram0/mm_stat'
```

```
lzo lzo-rle lz4 lz4hc lz4k lz4k_oplus [lz4kd] deflate 842 zstd
853405696 265356378 332423168        0 400699392     7225        0    10340    13460
```

The active algorithm is the one in brackets. In `mm_stat`, the first three
columns are the ones worth reading: **original** data size, **compressed**
size, and **total memory used** including allocator overhead. In the example
above roughly 814 MB of pages are being held in about 253 MB — a little
over 3:1.

Switching the algorithm requires resetting the ZRAM device, so it is not
something to change casually on a live system.

### Droidspaces

Enables SysV IPC, POSIX message queues and IPC namespaces — the foundation
for running a real Linux container with its own init system, rather than a
plain chroot that shares the host's process tree. Use it through the
[Droidspaces app](https://github.com/ravindu644/Droidspaces-OSS), which does
the actual container setup.

Android's GKI leaves these off, and turning them on is not trivial: the
structures involved are kABI-tracked, so the new fields have to occupy
`ANDROID_KABI_RESERVE` slots. Which slots are free differs per respin, so
the build tries three variants and uses whichever fits the exact source
tree. This one used the 6/7/8 slot variant.

```bash
su -c 'zcat /proc/config.gz | grep -E "SYSVIPC|POSIX_MQUEUE|IPC_NS"'
```

```
CONFIG_SYSVIPC=y
CONFIG_SYSVIPC_SYSCTL=y
CONFIG_POSIX_MQUEUE=y
CONFIG_POSIX_MQUEUE_SYSCTL=y
CONFIG_IPC_NS=y
CONFIG_SYSVIPC_COMPAT=y
```

**Android ships no `ipcs` binary**, so that command reports "inaccessible or
not found". Read procfs directly instead — these files only exist when
SysV IPC is compiled in:

```bash
su -c 'cat /proc/sysvipc/shm'
su -c 'cat /proc/sysvipc/sem'
```

For a functional namespace check:

```bash
su -c 'unshare -pf echo namespace-test-ok'
```

### Baseband-guard

An LSM that hooks the kernel write path and blocks unauthorized writes to
the baseband/modem partitions, denying by default and logging every blocked
attempt.

```bash
su -c 'zcat /proc/config.gz | grep CONFIG_BBG'
su -c 'dmesg | grep -c baseband_guard'
```

```
CONFIG_BBG=y
# CONFIG_BBG_BLOCK_BOOT is not set
# CONFIG_BBG_BLOCK_RECOVERY is not set
```

A non-zero dmesg count means it is live — BBG logs a line each time it
evaluates a process's SELinux domain.

`CONFIG_BBG_BLOCK_BOOT` and `CONFIG_BBG_BLOCK_RECOVERY` are **deliberately
off**. Enabling them has caused real conflicts with kernel-zip flashing and
recovery tools on other BBG kernels. Only the core baseband protection is
on.

Do not use `/sys/kernel/security/lsm` for this — securityfs is not mounted
there on Android and the command just returns "No such file or directory".

### MGLRU and PSI

MGLRU replaces the traditional active/inactive LRU lists with multiple
generations based on access recency, which makes reclaim decisions more
accurate and keeps more background apps alive under pressure. PSI exposes
real stall metrics that LMKD uses to decide what to kill.

```bash
su -c 'cat /sys/kernel/mm/lru_gen/enabled'
su -c 'cat /proc/pressure/memory'
```

```
0x0003
some avg10=0.00 avg60=0.00 avg300=0.00 total=1987090
full avg10=0.00 avg60=0.00 avg300=0.00 total=1085668
```

`0x0003` is a bitmask, not a count — any non-zero value means MGLRU is
active. `0x0000` would mean it is compiled in but switched off.

### WireGuard, CIFS/SMB, FUSE-BPF

WireGuard in-kernel means VPN apps use the kernel implementation instead of
the slower userspace one. CIFS lets you mount SMB shares directly, with
POSIX extensions enabled — real UID/GID, symlinks and device nodes are
preserved when the server supports them, instead of the Windows-style
approximation. FUSE-BPF speeds up FUSE operations, which Android uses
heavily for `/storage`.

```bash
su -c 'zcat /proc/config.gz | grep -E "WIREGUARD|^CONFIG_CIFS|FUSE_BPF"'
su -c 'cat /proc/filesystems | grep cifs'
```

`CONFIG_CIFS_POSIX=y` should appear alongside `CONFIG_CIFS_XATTR=y`. Note
that `CONFIG_CIFS_ALLOW_INSECURE_LEGACY=y` in that output comes from stock
GKI — Google enables it — and is a dependency of the POSIX extensions, not
something this build turns on to permit SMB1.

If you have a WireGuard tunnel up, it appears as an interface:

```bash
su -c 'ip link show type wireguard'
```

### Full LTO

LLVM full link-time optimization performs whole-program optimization across
all translation units, which allows more aggressive cross-module inlining
than the thin variant.

```bash
su -c 'zcat /proc/config.gz | grep CONFIG_LTO_CLANG_FULL'
```

### ptrace leak fix

Backports an upstream 5.16 hardening fix for a race where `ptrace_message`
was briefly readable by others before the tracer was notified, or left stale
after detach. There is no `/proc` or `/sys` flag for this — it is an
internal timing fix, not a toggle. On 6.1+ branches it is already upstream
and nothing is patched.

### KMI safety

Google's GKI enforces a stable kernel module interface so vendor modules
keep loading. Some kernels work around this by hiding new struct fields from
the checksum tool with `#ifndef __GENKSYMS__` — which makes the checksum
match while the actual struct layout still shifts underneath vendor modules
compiled against the old one.

That approach was tried here and produced a confirmed bootloop on a real
device. It is not used. Fields go in real reserve slots or the feature does
not ship.

---

## Check everything at once

```bash
su -c 'zcat /proc/config.gz' | grep -E "^CONFIG_(KSU|KPM|NF_TABLES|NFT_|IP6_NF_NAT|IP_SET|NTSYNC|ZRAM|CRYPTO_LZ4K|SYSVIPC|IPC_NS|POSIX_MQUEUE|BBG|LRU_GEN|PSI|WIREGUARD|CIFS|FUSE_BPF|TCP_CONG|NET_SCH|LTO_CLANG)"
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

That reports what landed upstream since your pin and separates commits
touching files this build depends on from the ones that do not. Only two
files can actually break a build:
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
  the defconfig back proves nothing. This is what caught four deprecated
  SUSFS options still being written into the defconfig long after upstream
  removed them.

Results land in `PATCH_STATUS.json` and `BUILD_REPORT.txt` next to the
artifacts.

---

## Credits

- [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) — root implementation
- [susfs4ksu](https://github.com/ShirkNeko/susfs4ksu) (ShirkNeko) — SUSFS
- [WildKernels](https://github.com/WildKernels) — BBRv3 backport, Droidspaces kABI patches, NTSync compat patches
- [Baseband-guard](https://github.com/vc-teahouse/Baseband-guard) (vc-teahouse) — modem write protection LSM
- [Droidspaces](https://github.com/ravindu644/Droidspaces-OSS) (ravindu644) — container userspace
- [AnyKernel3](https://github.com/osm0sis/AnyKernel3) (osm0sis) — flashable zip framework
- Google — the GKI kernel itself

## License

The kernel is GPLv2, as is everything derived from it. The patches in
`.github/workflows/scripts/patches/` and the configuration used to build
these releases are kept in this repository so the binaries above have their
corresponding source available.

Build scripts are provided as-is. Flashing custom kernels carries risk —
back up your boot image.
