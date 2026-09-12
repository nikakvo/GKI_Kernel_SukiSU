# GKI SukiSU-Ultra + SUSFS Build System — Poco F6 Pro

An automated build system for **GKI kernels** with **SukiSU-Ultra** and **SUSFS**, built and daily-driven on the **Poco F6 Pro / Redmi K70 (`vermeer`)**.

Python-assisted pipeline with automatic GKI respin tracking (including LTS-merge respins, not just date-based ones), exact source pinning, AVB signing, and dependency auto-installation for local builds.

---

## ⚠️ Scope — read this first

This project is developed, built and tested for **one target: `android13-5.15` on the Poco F6 Pro / Redmi K70 (`vermeer`)**. That is the only configuration actually flashed and run.

| Target | Status |
|--------|--------|
| `android13-5.15` on `vermeer` (Poco F6 Pro / K70) | **Built, flashed, tested, daily-driven.** This is the project. |
| Other `android13-5.15` devices | Should boot (standard GKI), but **untested** — your risk. |
| `android12-5.10`, `android14-6.1` | Tracked in the matrix, scripts attempt them, **untested**. |
| `android15-6.6` and newer | **Known broken** — build fails. Left disabled. |

- Current tested release: `android13-5.15.211` (LTS), tag `android13-5.15.211_r00`, kernel string `5.15.211-android13-r00-lts`.
- **Does not** support OnePlus ColorOS 14/15 or non-GKI devices.
- If you build anything other than `android13-5.15` for `vermeer`, you're on your own — **a working boot.img backup is mandatory**.

---

## Credits & Origin

Builds on the work of others in the GKI/KernelSU ecosystem:

- **[ShirkNeko/GKI_KernelSU_SUSFS](https://github.com/ShirkNeko/GKI_KernelSU_SUSFS)** — the build system this was originally forked from; the `build.py`/`kernel_builder.py` architecture traces back to it.
- **[SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra)** — the KernelSU implementation compiled into every kernel.
- **[susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu)** and **[ShirkNeko/SukiSU_patch](https://github.com/ShirkNeko/SukiSU_patch)** — SUSFS kernel patches and supplementary SukiSU-Ultra patches (ZRAM, hooks).
- **[WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches)** — the BBRv3 backport patches (`common/bbrv3`).
- **[ravindu644/Droidspaces-OSS](https://github.com/ravindu644/Droidspaces-OSS)** — the container-runtime app and kABI-compliant kernel patches; [WildKernels/GKI_KernelSU_SUSFS](https://github.com/WildKernels/GKI_KernelSU_SUSFS) was the reference for wiring Droidspaces into a GKI pipeline.

Since forked, this repo has diverged significantly (exact GKI respin pinning including LTS-merge tags/commits, automatic matrix updates, local-build tooling, AVB signing, safe-mode removal, ath9k_htc adapter support) and is maintained as an independent project.

---

## Features & verification

Every feature below ships with the exact command to confirm it's **actually active on your device** — run each in a root shell (`adb shell` then `su`, or a terminal app like Termux). This is the single source of truth for verification; the releases just list the feature names.

> Some features are **opt-in per build** (`--bbg`, `--droidspaces`, `--bbr-version bbr3`, ...) or only wired up on certain branches. If a check below comes back empty, first confirm that build actually enabled it — see the [build's patch-status summary](#build-matrix).

### Root & hiding

**[SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) (KernelSU-based root)** — kernel-level root solution, compiled into every build.

**[SUSFS v2.3.0](https://gitlab.com/simonpunk/susfs4ksu)** — kernel-level patches that assist root-hiding (hides suspicious paths/mount points, spoofs kernel stats/uname/cmdline, and more) together with its userspace module.

**KPM (Kernel Patch Module)** — SukiSU KPM support is built in, so compatible KPM modules can be loaded at runtime.
```bash
su -c "cat /proc/kallsyms | grep sukisu_kpm_version"
```
Active if `sukisu_kpm_version` is listed.

**Magic Mount** — overlay-based mounting that lets root modules modify the filesystem without touching the underlying partitions, improving compatibility and reducing detection surface.

**Safe Mode Removal** — volume-key safe-mode detection permanently patched out (built by default; opt out by omitting `--disable-safemode`). Most users rely on [YABP](https://github.com/Magisk-Modules-Repo/YetAnotherBootloopProtector) instead.

### Networking

**BBR v3** — Google's improved successor to BBR v1: better fairness and less bufferbloat under load. Backported via [WildKernels' kABI-compliant patch](https://github.com/WildKernels/kernel_patches/tree/main/common/bbrv3), selected **in place of** BBR v1. Falls back to BBR v1 automatically if the patch doesn't apply on a given branch/sub_level. See [BBRv3 details](#bbrv3-details) below.
```bash
su -c "cat /proc/sys/net/ipv4/tcp_congestion_control"
```
Active if the output is `bbr3` (not `bbr`).

**Additional TCP Congestion Control (BIC, Westwood+, H-TCP)** — extra selectable algorithms alongside BBR/CUBIC/Reno. Doesn't change the default — just makes more available to switch to at runtime (Westwood suits lossy/wireless links).
```bash
su -c "cat /proc/sys/net/ipv4/tcp_available_congestion_control"
```
Active if `bic`, `westwood`, and `htcp` all appear (switch with `su -c "sysctl net.ipv4.tcp_congestion_control=westwood"`).

**CAKE Queue Discipline** — modern qdisc (`sch_cake`) that reduces bufferbloat and improves latency under load (fair queuing + AQM + shaping in one).
```bash
su -c "tc qdisc add dev lo root cake && tc qdisc show dev lo && tc qdisc del dev lo root"
```
Active if `qdisc show` lists `qdisc cake ...`.

**WireGuard** — built-in kernel-level WireGuard VPN — lightweight, high-performance alternative to OpenVPN/IPsec.
```bash
su -c "zcat /proc/config.gz | grep CONFIG_WIREGUARD"
```
Active if it shows `CONFIG_WIREGUARD=y`.

**IP Set (netfilter IP/network grouping)** — kernel `ipset` support: group IPs/networks/ports into named sets for O(1) hash-matched `iptables`/`ip6tables` rules and dynamic updates without reloading the ruleset. *(Needs a separate userspace `ipset` binary — see [ipset-arm64](https://github.com/nikakvo/ipset-arm64), not bundled.)*
```bash
su -c "ipset create test hash:ip && ipset destroy test"
```
Active if it runs with no "Kernel module not found" error.

**TTL / Hop-Limit Target (netfilter)** — `iptables`/`ip6tables` target (`XT_TARGET_HL`) that rewrites a packet's TTL (IPv4) / Hop Limit (IPv6) — commonly used to normalize tethered/hotspot traffic so carriers can't detect it via the TTL decrement.
```bash
su -c "iptables -t mangle -A POSTROUTING -j TTL --ttl-set 65 && iptables -t mangle -D POSTROUTING -j TTL --ttl-set 65"
```
Active if both commands run with no "No chain/target/match by that name" error.

**Connection Mark / connmark (netfilter)** — `XT_CONNMARK` target and match that tags whole connections (not just packets) so later packets of the same connection are handled consistently. Used for advanced firewall/QoS/policy-routing, often paired with the TTL target above.
```bash
su -c "iptables -t mangle -A POSTROUTING -j CONNMARK --set-mark 1 && iptables -t mangle -D POSTROUTING -j CONNMARK --set-mark 1"
```
Active if both commands run with no "No chain/target/match by that name" error.

**CIFS/SMB Network Filesystem** — kernel SMB3/CIFS client (`CONFIG_CIFS`): mount a Samba/Windows share directly (`mount -t cifs //server/share /mnt/point`) instead of via an app. The driver is always present; mounting something real needs a reachable SMB server.
```bash
su -c "cat /proc/filesystems | grep cifs"
```
Active if `cifs` is listed.

### Memory & performance

**ZRAM + LZ4KD** — enhanced LZ4 compression for ZRAM (sourced from Huawei's codebase): better ratios at minimal CPU cost, effectively increasing usable RAM. Other switchable algorithms: LZ4K, LZ4HC, deflate, 842, lz4k_oplus.
```bash
su -c "cat /sys/block/zram0/comp_algorithm"
```
Active if `[lz4kd]` appears in brackets.

**MGLRU (Multi-Gen LRU, on by default)** — modern memory-reclaim algorithm replacing the active/inactive LRU lists with access-recency generations: more accurate reclaim, fewer background-app kills under pressure, smoother multitasking.
```bash
su -c "cat /sys/kernel/mm/lru_gen/enabled"
```
Active if the value is non-zero (e.g. `0x0003`), not `0x0000`.

**PSI (Pressure Stall Information)** — real-time memory/CPU/I/O pressure metrics (`/proc/pressure/*`) so LMKD makes smarter kill decisions than coarse thresholds. Works together with MGLRU.
```bash
su -c "cat /proc/pressure/memory"
```
Active if it prints `avg10=... avg60=... avg300=... total=...` instead of an error.

**Full / Thin LTO** — LLVM Link-Time Optimization. **Official releases are Full LTO** (built locally); CI/Actions builds use **Thin LTO** (Full peaks ~27 GB RAM, more than a runner has). Both perform well once installed; Full squeezes out marginally better runtime and code size.
```bash
su -c "zcat /proc/config.gz | grep -E 'CONFIG_LTO_CLANG_(FULL|THIN)'"
```
Active if it shows `CONFIG_LTO_CLANG_FULL=y` (release) or `CONFIG_LTO_CLANG_THIN=y` (CI).

### Containers & compatibility

**[Droidspaces]** — real Linux namespace isolation (PID/IPC/Mount/User) at the kernel level: run a full Linux distro in a genuine container with its own init system (systemd, OpenRC), not just a chroot. Managed via the [Droidspaces app](https://github.com/ravindu644/Droidspaces-OSS). See [Droidspaces details](#droidspaces-details) below.
```bash
su -c "zcat /proc/config.gz | grep -E 'CONFIG_SYSVIPC|CONFIG_POSIX_MQUEUE|CONFIG_IPC_NS|CONFIG_PID_NS|CONFIG_USER_NS|CONFIG_DEVTMPFS'"
```
Active if all show `=y`. Deeper functional check:
```bash
su -c "unshare -pf echo namespace-test-ok"
```
Active if it prints `namespace-test-ok` without an error.

**NTSync (Winlator/Wine)** — kernel driver (`/dev/ntsync`) emulating Windows NT sync primitives (semaphores/mutexes/events) natively instead of over futex: better compatibility and lower overhead for Wine-based layers like Winlator. Only on branches with a compatible backport.
```bash
su -c "ls -la /dev/ntsync"
su -c "zcat /proc/config.gz | grep CONFIG_NTSYNC"
```
Active if `/dev/ntsync` exists (character device) and `CONFIG_NTSYNC=y` is shown.

### Security

**BBG (Baseband-guard)** *(`--bbg`)* — lightweight LSM ([vc-teahouse/Baseband-guard](https://github.com/vc-teahouse/Baseband-guard)) hooking the kernel write path to block unauthorized writes to the baseband/modem and other protected partitions, denying by default and logging every blocked attempt. Boot/recovery-partition protection is deliberately left off (it's caused conflicts with kernel-zip flashing elsewhere) — only core baseband protection is on.
```bash
su -c "zcat /proc/config.gz | grep CONFIG_BBG"
su -c "dmesg | grep -c baseband_guard"
```
Active if `CONFIG_BBG=y` is shown and the dmesg count is non-zero.

**Ptrace Leak Fix (kernels < 5.16)** — backports the upstream 5.16 hardening fix that closes a `ptrace_message` race (a child's PID briefly visible to other readers before the tracer is notified, or stale after detach). Applied on 5.10/5.15; already upstream on 6.1+. No `/proc` flag to check — it's an internal timing fix, not a toggle.

**Unicode Fix** — backports a fix to the kernel's UTF-8 normalization subsystem (`fs/unicode/utf8-norm.c`, used by case-insensitive f2fs/ext4 folders). The upstream bug advanced the decode cursor *before* checking whether a decomposition was empty (e.g. a zero-width character), so a crafted filename could make case-insensitive path comparisons behave inconsistently — including the path checks used for root-hiding, hence the "bypass". A plain upstream-kernel fix (not KSU/SUSFS-specific). No `/proc` flag to check — it's an internal correctness fix, not a toggle.

### External hardware

**ath9k_htc — external WiFi adapter (`--ath9k`)** — TP-Link TL-WN722N v1 (Atheros AR9271) over OTG: **monitor mode + packet injection** for WiFi security testing on **your own networks** (Kali/NetHunter-style, on the phone), with built-in WiFi still fully working. Ships as a flashable module. See [ath9k_htc details](#ath9khtc-details) below.
```bash
su -c "zcat /proc/config.gz | grep CONFIG_ATH9K_HTC"
```
Active if it shows `CONFIG_ATH9K_HTC=m`. Full end-to-end check (adapter plugged in, module flashed): `iw dev` lists a `wlan1` interface.

---

## Quick Start

### Local build (recommended)

Handles dependency installation automatically — no manual setup, even on a clean Ubuntu/WSL install.

```bash
git clone https://github.com/nikakvo/GKI_Kernel_SukiSU
cd GKI_Kernel_SukiSU
chmod +x build-kernel.sh cleanup-workspace.sh
./build-kernel.sh
```

The tested releases pin known-good SukiSU-Ultra and SUSFS commits:

```bash
./build-kernel.sh --ksu-commit v4.2.0 --susfs-commit bca0d2333c1a7d717e7278b019d7af7ba1d16005
```

You'll get a menu:
```
1) Default (android13 / 5.15 / 211 / 2026-06)   ← the tested config for vermeer
2) Custom (choose your own versions)             ← anything non-android13-5.15 is untested
3) All versions from matrix.json                 ← every "enabled": true entry, with a pass/fail summary
```

Reclaim disk space between builds (keeps shared repos and the AVB key intact):
```bash
./cleanup-workspace.sh
```

### GitHub Actions

> **CI builds Thin LTO, not Full.** Full LTO peaks ~27 GB RAM, more than a runner has. Actions builds are usable but are **not** the tested release binaries — official releases are Full-LTO, built locally and flashed on-device first.

- **Kernel Build** workflow → build a single version (pick Android/kernel/options).
- **Build Kernels** workflow → build every enabled matrix entry.

### Command-line (manual)

```bash
cd .github/workflows/scripts
pip install PyYAML

# Build the tested default (android13-5.15.211, vermeer)
python3 build.py --android android13 --kernel 5.15 --sub-level 211 --os-patch 2026-06 --kernel-tag android13-5.15.211_r00 --ath9k

# List supported Android/Kernel combinations
python3 build.py --list-configs
```

---

## Exact Source Pinning (`--kernel-tag`)

Google's GKI branches (e.g. `android13-5.15-2025-12`) are **moving branches**, not fixed points — Google periodically pushes new commits and tags each snapshot as a numbered respin (`_r1`, `_r2`, …). Building without pinning grabs whatever the branch HEAD happens to be at sync time, which is **not reproducible** and may be several respins behind the latest security fixes.

`--kernel-tag` checks out an **exact** respin instead. It accepts:

**A date-based respin tag** (classic scheme):
```bash
python3 build.py --android android13 --kernel 5.15 --sub-level 194 --os-patch 2025-12 \
    --kernel-tag android13-5.15-2025-12_r10
```

**A per-sublevel LTS-merge tag** (see [LTS Builds](#lts-builds)):
```bash
python3 build.py --android android13 --kernel 5.15 --sub-level 211 --os-patch 2026-06 \
    --kernel-tag android13-5.15.211_r00
```

**A raw commit SHA** (7–40 hex chars) — when an LTS-merge has landed on Google's `android*-lts` branch but no `_r00` tag is cut yet:
```bash
python3 build.py --android android13 --kernel 5.15 --sub-level 211 --os-patch 2026-06 \
    --kernel-tag 12b3f6828b67824c794e422d5785dba6eb559bb2
```

Find the latest respin/tag for your branch at:
- https://android.googlesource.com/kernel/common/+refs (all tags/branches)
- https://android.googlesource.com/kernel/common/+log/refs/heads/android13-5.15-lts (live LTS log — look for "Merge 5.15.XXX into androidYY-Z.ZZ-lts")
- https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds (official release notes, date-based only)

**If the tag/SHA doesn't exist upstream, the build fails immediately** rather than silently falling back to the moving HEAD — a build that quietly compiles a *different* real sub_level while every filename still claims the one you asked for is far worse than one that refuses to start. The resulting kernel string reflects the pinned respin (e.g. `5.15.211-android13-r00-lts`).

---

## LTS Builds

Google keeps a GKI branch current two ways:

1. **Date-based respins** (`android13-5.15-2026-06_r4`) — periodic official snapshots per security patch level; the classic, fully-certified process.
2. **LTS merges** (`android13-5.15.211_r00`) — once the date-based cadence winds down, Google periodically merges the upstream Linux `5.15.y` **-stable** tree straight into a sibling `android13-5.15-lts` branch and tags the result. Trades full GKI certification for staying current with upstream kernel security fixes.

Both are real, buildable, and built identically here — LTS is just a different tag naming scheme, not a separate build mode. Any LTS-sourced build (dot-style tag or raw SHA) gets a `-lts` marker appended so it's obvious downstream:

- **Filename:** `android13-5.15.211-2026-06-lto-full-r00-lts-boot.img` (vs. `android13-5.15.206-2026-06-r4-boot.img`)
- **On-device kernel version:** `5.15.211-android13-r00-lts` (vs. `5.15.206-android13-r4`)
- SHA-pinned (no tag yet): respin number omitted — `5.15.211-android13-lts`

This is detected automatically from the tag format (a dot before the sub_level, or a bare SHA) — you never flag a build as LTS by hand. `matrix.json` marks these `"lts": true` purely for the CI summary; it has no effect on the build.

---

## Pinning SukiSU-Ultra / SUSFS (`--ksu-commit`, `--susfs-commit`)

By default the build tracks the latest SukiSU-Ultra and susfs4ksu. That's usually what you want — but both are **fast-moving upstream projects**, and sometimes the newest commit doesn't build (an API change lands before the patches catch up, a hook breaks). When that happens, pin a **known-good commit** instead of waiting for an upstream fix. This is exactly how the tested releases are built:

```bash
./build-kernel.sh --ksu-commit v4.2.0 --susfs-commit bca0d2333c1a7d717e7278b019d7af7ba1d16005
```

- **`--ksu-commit <ref>`** — pin SukiSU-Ultra's kernel-side source to a tag (e.g. `v4.2.0`) or commit hash.
- **`--susfs-commit <ref>`** — pin susfs4ksu to a specific commit or tag.

> **`--susfs-commit` is per-branch.** susfs4ksu keeps a **separate branch per GKI version**, each carrying only its own `50_add_susfs_in_gki-<android>-<kernel>.patch`. So a hash is valid for **exactly one** GKI branch — the build refuses a mismatched pin. `bca0d233…` is the `gki-android13-5.15` one; the `android12-5.10` equivalent is `ec785f4`.
>
> To pin different commits per branch in one command, use the `branch=hash` form:
> ```bash
> ./build-kernel.sh --ksu-commit v4.2.0 \
>     --susfs-commit 'gki-android12-5.10=ec785f4,gki-android13-5.15=bca0d2333c1a7d717e7278b019d7af7ba1d16005'
> ```

Pin the two **together** — SUSFS's integration patch targets a specific SukiSU-Ultra source layout, so mixing a pinned SUSFS with a moving SukiSU (or vice versa) can drift out of sync.

---

## Build Matrix

`matrix.json` (`.github/workflows/config/matrix.json`) is the single source of truth for which `sub_level`/`os_patch_level`/`kernel_tag` combinations exist. It is **not maintained by hand**:

```bash
cd .github/workflows/scripts
python3 update_matrix.py --dry-run   # preview
python3 update_matrix.py             # apply
```

This queries Google's `kernel/common` repo directly (`git ls-remote --tags`) and tracks **both** tag schemes independently so a date-based respin and an LTS-merge respin never collide:

- **Date-based tags** — latest respin per month, real `sub_level` resolved from each tag's `Makefile`, keyed by `os_patch_level`.
- **LTS-merge tags** — `sub_level` read straight from the tag (no `Makefile` fetch), keyed by `sub_level`, capped to the 15 highest per family (these tags carry no date to bound the lookback).

New entries are added `"enabled": false` (opt-in); existing entries get their `kernel_tag`/`sub_level` refreshed without touching your `enabled` choices. A SHA-pinned entry is auto-upgraded to the real tag once Google cuts it.

Tracked families: `android12-5.10`, `android13-5.15`, `android14-6.1`, `android15-6.6`, `android16-6.12`, `android17-6.18`. **Only `android13-5.15` is tested.**

```json
{"sub_level": "211", "os_patch_level": "2026-06", "kernel_tag": "android13-5.15.211_r00", "lts": true, "enabled": true}
```

---

## Command-Line Arguments

| Argument | Description | Default |
|------|------|--------|
| `--android`, `-a` | Android version (android12–android17) | android14 |
| `--kernel`, `-k` | Kernel version (5.10/5.15/6.1/6.6/6.12/6.18) | 6.1 |
| `--sub-level`, `-s` | Sub level (e.g. `194`, `211`) | 124 |
| `--os-patch` | OS Patch Level | 2025-02 |
| `--kernel-tag` | Pin an exact GKI respin — date-based tag, LTS-merge tag, or raw commit SHA (see [Exact Source Pinning](#exact-source-pinning---kernel-tag)) | - |
| `--lts` | Mark build as LTS-sourced. Rarely needed — auto-detected from `--kernel-tag` | auto |
| `--ath9k` | Build ath9k_htc + ath9k_common + ath9k_hw + ath as out-of-tree modules for a TL-WN722N v1 (see [ath9k_htc details](#ath9khtc-details)) | False |
| `--revision` | Android 12 revision (certified-boot reference downloads) | - |
| `--ksu-version` | SukiSU-Ultra version (Stable/Dev) | Stable |
| `--ksu-commit` | Pin a SukiSU-Ultra commit/tag | latest |
| `--susfs-commit` | Pin a SUSFS commit (hash or HEAD~N) | latest |
| `--zram` | Enable ZRAM (LZ4KD) | False |
| `--no-kpm` | Disable KPM | False |
| `--bbg` | Enable Baseband-guard | False |
| `--droidspaces` | Enable Droidspaces (android12/13/14 only) | False |
| `--op8e` | Enable OnePlus 8E support | False |
| `--bbr-version` | Congestion control: `none`, `bbr1`, or `bbr3` (bbr3 android12/13/14 only) | bbr1 |
| `--disable-safemode` | Permanently disable volume-key safe-mode detection | False |
| `--no-release` | Don't create a GitHub Release | False |
| `--custom-version` | Custom `CONFIG_LOCALVERSION` string | - |
| `--list-configs` | List supported Android/Kernel combinations | - |
| `--dry-run` | Validate the configuration only, don't build | - |
| `--workspace`, `-w` | Working directory | /tmp/gki-build |

---

## Downloads

Each release attaches:

1. **boot.img** — the ready-made boot image (`android13-5.15.211-2026-06-lto-full-r00-lts-boot.img`). Flash over `fastboot`. AVB-signed (see [AVB Signing](#avb-signing)).
2. **AnyKernel3.zip** — the same kernel packaged for flashing from recovery or a kernel-flasher app, without a PC.
3. **ath9k_htc_vermeer.zip** — companion module for the ath9k adapter. Only needed with a TL-WN722N v1.

Pick **one** of boot.img or AnyKernel3 — they install the same kernel two different ways.

---

## Installation

> **This is a SukiSU-Ultra kernel.** Root is managed by the **[SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) manager app** — install it (or update to it) so you get a root manager after flashing. See the [SUSFS notes](#susfs-userspace) at the end.
>
> **Always keep your current, working `boot.img` backed up before flashing anything.** If a flash goes wrong, see [Emergency Recovery](#emergency-recovery).

### Method 1 — boot.img via fastboot (PC)

Reboot to the bootloader (`adb reboot bootloader`, or Power + Volume Down), then pick one:

**Permanent, both slots** (recommended for daily use):
```bash
fastboot flash boot_ab android13-5.15.211-2026-06-lto-full-r00-lts-boot.img
```

**Try it first, without writing anything** (loads the image once into RAM; a normal reboot goes back to your existing kernel):
```bash
fastboot boot android13-5.15.211-2026-06-lto-full-r00-lts-boot.img
```

> **Tip:** instead of typing the long filename, type `fastboot flash boot_ab ` (with the trailing space) and **drag-and-drop the `.img` file** into the terminal — it fills in the full path for you.
>
> **`fastboot boot` vs `fastboot flash`:** `fastboot boot` (no `flash`) is truly temporary — nothing is written, so a normal reboot goes back to your old kernel. Use it to test before committing. `fastboot flash boot_ab` actually writes the kernel to both slots. If `fastboot boot` just hangs on this device, flash with `flash boot_ab` instead (keep your backup boot.img handy either way).

### Method 2 — AnyKernel3 (no PC)

Flash `AnyKernel3.zip` like any other flashable zip:

- **Custom recovery** (TWRP/OrangeFox) — *Install* → pick the zip → swipe.
- **[HorizonKernelFlasher](https://github.com/libxzr/HorizonKernelFlasher/releases)** or another kernel-flasher app — open the app, select the zip, flash.
- **SukiSU-Ultra manager** — if your current manager supports flashing AnyKernel zips, you can install it from there directly.

Reboot when done.

### After flashing

1. Open the **SukiSU-Ultra manager** — it should show the kernel as rooted.
2. Confirm the kernel string is `5.15.211-android13-r00-lts` (shown in the manager, or `su -c uname -r`).
3. Verify any features you care about with the commands in [Features & verification](#features--verification).

<a id="susfs-userspace"></a>
### SUSFS (userspace)

The **kernel side** of SUSFS is already baked into this build (patched in from [`ShirkNeko/susfs4ksu`](https://github.com/ShirkNeko/susfs4ksu), per GKI branch) — you don't install anything for that part.

For **full SUSFS support** you also need the **userspace module**, which actually drives the hiding (mount cleanup, path spoofing, etc.). Flash [**sidex15/susfs4ksu-module**](https://github.com/sidex15/susfs4ksu-module) in the SukiSU-Ultra manager like any other root module:

- **Release build:** grab the latest zip from the module's [Releases](https://github.com/sidex15/susfs4ksu-module/releases).
- **Latest build:** or the freshest artifact from its [Actions](https://github.com/sidex15/susfs4ksu-module/actions).

Flash it, reboot, and configure the hiding from the SUSFS module / manager.

---

## ath9k_htc details

Adds support for a **TP-Link TL-WN722N v1** (Atheros AR9271) USB adapter over OTG — monitor mode and packet injection — while the phone's built-in WiFi stays fully working. For WiFi security testing on **your own networks** (Kali/NetHunter-style workflows, on the phone instead of a laptop). Enable with `--ath9k` (on by default in `build-kernel.sh`).

### How it works (and why not the obvious way)

GKI ships **no** wireless stack — `gki_defconfig` defines neither `CONFIG_CFG80211` nor `CONFIG_MAC80211`; the real stack is Qualcomm's, loaded as vendor modules from `/vendor/lib/modules`. The naive fix (`CFG80211=y`/`MAC80211=y`) builds a second copy of that symbol surface into the image, which stops the vendor's `qca_cld3`/`kiwi_v2` from loading and **kills internal WiFi**.

Instead, `--ath9k` builds `ath9k_htc` + `ath9k_common` + `ath9k_hw` + `ath` as **out-of-tree modules** (`=m`, never `=y`) linked against the vendor stack. The GKI image's wireless config is left untouched, so internal WiFi is never disturbed. The built `cfg80211.ko`/`mac80211.ko` are deliberately **not** packaged — the device uses Qualcomm's. Post-build, the modules' exported-symbol CRCs are verified against the device's actual vendor `cfg80211`/`mac80211` before release, so a module that couldn't load never ships.

### Using it

The kernel alone isn't enough — the driver needs firmware and needs loading at boot. That's the separate flashable module **`ath9k_htc_vermeer.zip`** (attached to each release; source in [`ath9k-module/`](ath9k-module/)):

1. Flash the kernel (built with `--ath9k`).
2. Install `ath9k_htc_vermeer.zip` in your root manager.
3. Reboot, plug the antenna in over OTG.

See [`ath9k-module/README.md`](ath9k-module/README.md) for details, including why **the antenna LED won't light** (intentional — the vendor `mac80211` doesn't export the LED-trigger symbols, and forcing them would stop the driver loading; everything else works).

> **Adapter must be v1.** The TL-WN722N v2 and v3 are a Realtek chip and will **not** work with this driver.

---

## BBRv3 details

[BBRv3](https://github.com/WildKernels/kernel_patches/tree/main/common/bbrv3) is Google's newer TCP congestion control — an evolution of the widely-used BBRv1. Backport patches are vendored from WildKernels, pre-adjusted for Android kABI compliance. Enable with `--bbr-version bbr3` (or the `BBR congestion control version` choice in either Actions workflow, or `BBR_VERSION="bbr3"` in `build-kernel.sh`).

**Wired up for `android12-5.10`, `android13-5.15`, `android14-6.1` only** — though only `android13-5.15` is tested. (WildKernels also publish an `android15-6.6` variant; not vendored since nothing currently built targets that branch.)

Two small prerequisite patches (`proc_dou8vec_minmax()` and a follow-up data-race fix, both from mainline `-stable`) are applied first if missing — on the sub_levels built here they're almost certainly already present, so this is a silent no-op in practice.

If the main BBRv3 patch doesn't apply cleanly (a branch's `net/ipv4` source has diverged too far), the build **falls back to BBRv1** as the default rather than silently leaving cubic — check the [patch status summary](#build-matrix) (a `FAIL` on `BBRv3` means this happened).

---

## Droidspaces details

[Droidspaces](https://github.com/ravindu644/Droidspaces-OSS) is a lightweight, LXC-like container runtime for Android — real Linux namespace isolation (PID, IPC, Mount) so a full Linux distro can run with its own genuine init system (systemd, OpenRC), instead of a plain chroot that just shares the host's process tree. Enable with `--droidspaces` (or the `Enable Droidspaces` toggle in either Actions workflow, or `DROIDSPACES="1"` in `build-kernel.sh`).

**Wired up for `android12-5.10`, `android13-5.15`, `android14-6.1`** — though only `android13-5.15` is tested. GKI enforces a strict kABI checksum on struct layouts, so naively enabling `CONFIG_SYSVIPC`/`CONFIG_IPC_NS`/`CONFIG_POSIX_MQUEUE` without a matching patch causes an **immediate bootloop**. This flag applies the upstream kABI-safe patch (moving the relevant fields into Android's reserved padding slots) *before* turning those options on — three slot-layout variants are vendored and tried in order (strict, no-fuzz matching), since which `ANDROID_KABI_RESERVE` slots are free isn't identical across every respin; whichever one fits this exact source tree is used. If none fit, the build continues **without** Droidspaces rather than risking a silent kABI break (a `FAIL` on `Droidspaces` in the [patch status summary](#build-matrix) means this happened).

Applied *after* SUSFS/SukiSU-Ultra in the build sequence deliberately — if there's ever a conflict over the same kABI reserve slots, it's this optional feature that loses, never the project's core functionality.

Verify and use it via the [Droidspaces app](https://github.com/ravindu644/Droidspaces-OSS) (Settings → Requirements → Check Requirements).

---

## AVB Signing

Every `boot.img` is signed with `avbtool` using a canonical RSA key so images are consistently signed across local and CI builds:

- **Locally:** if no key is found, one is auto-generated once at `<workspace>/boot_sign_key.pem` and reused for every subsequent build.
- **CI:** set the same key content as the `BOOT_SIGN_KEY` repository secret.

For most users on an unlocked bootloader this is a formality (AVB verification is typically bypassed).

---

## Emergency Recovery

> Use this if the device fails to boot after a bad or incompatible kernel flash.

1. Enter Fastboot mode — hold **Power + Volume Down**, or run `adb reboot bootloader`.
2. Flash a known-good boot image:
   ```bash
   fastboot flash boot_ab <full_boot.img_filename>
   ```

---

## Build System Architecture

```
.github/workflows/
├── config/
│   ├── matrix.json           # Build matrix — kept current by update_matrix.py
│   └── update_matrix.py      # Refreshes matrix.json from Google's kernel/common tags
├── scripts/
│   ├── build.py               # Main build script (CLI entry point)
│   ├── kernel_builder.py      # Core kernel build class
│   ├── config.py              # Configuration definitions and validation
│   ├── matrix_generator.py    # GitHub Actions matrix generator
│   ├── patch_summary.py       # Aggregates per-build patch status into one CI summary table
│   ├── release_generator.py   # Release notes generator (feature list only)
│   ├── extract_artifacts.py   # Collects build artifacts for release
│   ├── telegram_notify.py     # Optional Telegram build notifications
│   ├── ath9k/                 # ath9k module fragment, CRC table, symbol verifier
│   ├── firmware/ath9k_htc/    # htc_9271-1.4.0.fw
│   └── patches/               # kernel patches (bbrv3, ntsync, droidspaces, ath9k LEDS, ...)
├── update-matrix.yml          # Scheduled workflow that runs update_matrix.py
├── kernel-build.yml           # Single-version build workflow
└── build-kernels.yml          # Full matrix build workflow

ath9k-module/                  # Source of the ath9k_htc_vermeer.zip flashable module
build-kernel.sh                # Local interactive build menu (recommended entry point)
cleanup-workspace.sh           # Reclaims disk space between builds
```

### Core Components

| Component | Function |
|------|------|
| `KernelBuilder` | Core build class — clones source, applies patches, compiles, packages |
| `BuildConfig` | Build configuration data class holding all build parameters |
| `update_matrix.py` | Queries Google's kernel/common tags and keeps matrix.json current |
| `matrix_generator.py` | Generates the build matrix for the Actions matrix build |
| `patch_summary.py` | Aggregates per-build patch status into one CI summary table |
| `release_generator.py` | Generates the release feature list |

### Repository Dependencies

| Repository | Purpose |
|------|------|
| [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | SukiSU-Ultra source and setup script |
| [susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu) | SUSFS kernel patches |
| [SukiSU_patch](https://github.com/ShirkNeko/SukiSU_patch) | Additional SukiSU-Ultra patches (ZRAM, hooks) |
| [AnyKernel3](https://github.com/WildPlusKernel/AnyKernel3) | Generic flashable package template |
| [Baseband-guard](https://github.com/vc-teahouse/Baseband-guard) | Baseband security protection |
