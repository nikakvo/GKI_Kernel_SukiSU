# GKI SukiSU-Ultra + SUSFS Build System — Poco F6 Pro

### An automated build system for GKI kernels with SukiSU-Ultra and SUSFS, built and tested for the Poco F6 Pro / Redmi K70 (`vermeer`)

> **Scope — read this first.** This project is developed, built and tested
> for **one target: `android13-5.15` on the Poco F6 Pro / Redmi K70
> (`vermeer`)**. That is the only configuration I actually flash and run.
> Because it produces a standard GKI kernel, it will also boot on other
> devices sharing the same `android13-5.15` GKI base — but I don't test
> those, so treat them as use-at-your-own-risk.
>
> The build scripts *can* target other Android/kernel families
> (`android12-5.10`, `android14-6.1` are tracked in the matrix), and you're
> free to try them — but they are **not tested and not supported**.
> `android15-6.6` and newer are **known to break** (the build failed
> outright when tried) and are left disabled. If you build anything other
> than `android13-5.15` for `vermeer`, you're on your own, and a working
> boot.img backup is mandatory.

> Does not support OnePlus ColorOS 14/15 or non-GKI devices.

> If this is your first time using it, please **read the following carefully** — don't waste other people's time out of laziness!

> Python-assisted build system with automatic GKI respin tracking (including LTS-merge respins, not just the classic date-based ones), exact source pinning, and dependency auto-installation for local builds.

---

## What's tested vs. what merely builds

| Target | Status |
|--------|--------|
| `android13-5.15` on `vermeer` (Poco F6 Pro / K70) | **Built, flashed, tested, daily-driven.** This is the project. |
| Other `android13-5.15` devices | Should boot (standard GKI), but untested — your risk. |
| `android12-5.10`, `android14-6.1` | Tracked in the matrix, scripts attempt them, **untested** — may or may not work. |
| `android15-6.6` and newer | **Known broken** — build fails. Left disabled in the matrix. |

The current tested release is `android13-5.15.211` (LTS), tag
`android13-5.15.211_r00`, kernel string `5.15.211-android13-r00-lts`.


## Credits & Origin

This project builds on the work of others in the GKI/KernelSU ecosystem:

- **[ShirkNeko/GKI_KernelSU_SUSFS](https://github.com/ShirkNeko/GKI_KernelSU_SUSFS)** — the original build system this project was originally forked from. The overall build.py/kernel_builder.py architecture traces back to this project.
- **[SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra)** — the KernelSU implementation this build system compiles into every kernel.
- **[susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu)** and **[ShirkNeko/SukiSU_patch](https://github.com/ShirkNeko/SukiSU_patch)** — SUSFS kernel patches and supplementary SukiSU-Ultra patches (ZRAM, hooks).
- **[WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches)** — the BBRv3 backport patches (`common/bbrv3`) vendored for the [BBRv3 Support](#bbrv3-support) feature.
- **[ravindu644/Droidspaces-OSS](https://github.com/ravindu644/Droidspaces-OSS)** — the container-runtime app and kABI-compliant kernel patches vendored for the [Droidspaces Support](#droidspaces-support) feature. The [WildKernels/GKI_KernelSU_SUSFS](https://github.com/WildKernels/GKI_KernelSU_SUSFS) actions were the reference for how to wire Droidspaces into a GKI build pipeline.

This repository has since diverged significantly from the original fork (exact GKI respin pinning including LTS-merge tags/commits, automatic matrix updates, local-build tooling, AVB signing, safe-mode removal, ath9k_htc adapter support, and more — see below), and is now maintained as an independent project.

---

## Quick Start

### Local build (recommended)

The fastest way to build — handles dependency installation automatically, no manual setup needed.

```bash
git clone https://github.com/nikakvo/GKI_Kernel_SukiSU
cd GKI_Kernel_SukiSU
chmod +x build-kernel.sh cleanup-workspace.sh
./build-kernel.sh
```

The tested default is `android13-5.15.211` (see the scope note above). You'll get a menu:
```
1) Default (android13 / 5.15 / 211 / 2026-06)
2) Custom (choose your own versions)
3) All versions from matrix.json
```

- **Option 1** builds the tested default configuration for `vermeer`.
- **Option 2** lets you pick any Android/kernel/sub_level/os_patch combination, plus optionally pin an exact GKI respin tag (see [Exact Source Pinning](#exact-source-pinning-kernel-tag) below). Anything other than `android13-5.15` is untested — see the scope note at the top.
- **Option 3** builds every `"enabled": true` entry in `matrix.json` sequentially, with a per-build pass/fail summary at the end.

The script auto-installs everything it needs on first run (git, build-essential, ccache, PyYAML, etc.) — no separate setup step required, even on a clean Ubuntu/WSL install.

When you're done, reclaim disk space with:
```bash
./cleanup-workspace.sh
```
(wipes only the per-version build directories — `.repo/`, `common/`, `prebuilts/` — keeps shared repos and the AVB signing key intact)

### GitHub Actions

> **CI builds thin LTO, not full.** Full LTO peaks around 27 GB RAM, more than a GitHub runner has, so Actions builds use thin LTO. These are usable but are **not** the tested release binaries — the official releases are built locally with full LTO and flashed on-device first.

#### Method 1: Build a single version
1. Go to the **Actions** tab
2. Select **Kernel Build**
3. Click **Run workflow**
4. Choose the Android version, kernel version, and build options

#### Method 2: Build all matrix versions
1. Select **Build Kernels**
2. Click **Run workflow**
3. Set the global options (KSU version, ZRAM, KPM, BBR, etc.)

### Command-line (manual)

```bash
cd .github/workflows/scripts
pip install PyYAML

# Build the tested default (android13-5.15.211, vermeer)
python3 build.py --android android13 --kernel 5.15 --sub-level 211 --os-patch 2026-06 --kernel-tag android13-5.15.211_r00 --ath9k

# List supported Android/Kernel combinations
python3 build.py --list-configs
```

There is no `--matrix`/`--all` flag anymore — see [Build Matrix](#build-matrix) below for why.

---

## Exact Source Pinning (`--kernel-tag`)

Google's GKI branches (e.g. `android13-5.15-2025-12`) are **moving branches**, not fixed points — Google periodically pushes new commits to the same branch and tags each snapshot as a numbered "respin" (`_r1`, `_r2`, ... `_r10`, ...). Building without pinning a tag just grabs whatever the branch HEAD happens to be at sync time, which is **not reproducible** and may be several respins behind the latest security fixes.

`--kernel-tag` fetches and checks out an **exact** respin instead of the moving HEAD, guaranteeing byte-for-byte the same source Google certified for that specific release. It accepts two different things, depending on what's available upstream:

**A date-based respin tag** (the classic scheme, e.g. `android13-5.15-2025-12_r10`):
```bash
python3 build.py --android android13 --kernel 5.15 --sub-level 194 --os-patch 2025-12 \
    --kernel-tag android13-5.15-2025-12_r10
```

**A per-sublevel LTS-merge tag** (e.g. `android13-5.15.211_r00`) — see [LTS Builds](#lts-builds) below for what these are:
```bash
python3 build.py --android android13 --kernel 5.15 --sub-level 211 --os-patch 2026-06 \
    --kernel-tag android13-5.15.211_r00
```

**A raw commit SHA** (7-40 hex chars) — for when an LTS-merge has already landed on Google's `android*-lts` branch but no official `_r00` tag has been cut for it yet:
```bash
python3 build.py --android android13 --kernel 5.15 --sub-level 211 --os-patch 2026-06 \
    --kernel-tag 12b3f6828b67824c794e422d5785dba6eb559bb2
```

Find the latest respin/tag for your target branch at:
- https://android.googlesource.com/kernel/common/+refs (all tags, all branches)
- https://android.googlesource.com/kernel/common/+log/refs/heads/android13-5.15-lts (live commit log for the LTS branch — useful when a merge has landed but isn't tagged yet; look for "Merge 5.15.XXX into androidYY-Z.ZZ-lts")
- https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds (official release notes, date-based scheme only)

**If the tag/SHA doesn't actually exist upstream, the build fails immediately with a clear error** rather than silently falling back to the moving branch HEAD — a build that silently compiles a different, real sub_level while every filename still claims to be the one you asked for is far worse than a build that just refuses to start.

The resulting kernel release string reflects the pinned respin (e.g. `5.15.211-android13-r00-lts`) instead of an ambiguous moving-HEAD version.

---

## LTS Builds

Google maintains two parallel ways of keeping a GKI branch (e.g. `android13-5.15`) up to date:

1. **Date-based respins** (`android13-5.15-2026-06_r4`) — periodic official snapshots, each covering a specific month's security patch level. This is the classic, fully-certified GKI release process.
2. **LTS merges** (`android13-5.15.211_r00`) — once a branch's date-based cadence winds down, Google instead periodically merges the upstream Linux `5.15.y` **-stable** tree (maintained by Greg Kroah-Hartman) straight into a sibling `android13-5.15-lts` branch, and eventually tags the result. These trade the full GKI certification process for staying current with upstream kernel security fixes.

Both are real, both are buildable, and this project builds either kind identically — LTS is not a separate build mode, just a different tag naming scheme on Google's end. To make it obvious which is which downstream, any build sourced from an LTS-merge respin (dot-style tag or raw SHA) gets a `-lts` marker appended:

- **Filename:** `android13-5.15.211-2026-06-lto-full-r00-lts-boot.img` (vs. `android13-5.15.206-2026-06-r4-boot.img` for a regular respin)
- **On-device kernel version** (visible in KernelSU/SukiSU-Ultra manager): `5.15.211-android13-r00-lts` (vs. `5.15.206-android13-r4`)
- If pinned by raw commit SHA (no official tag yet), the respin number is simply omitted rather than showing an unreadable hash: `android13-5.15.211-2026-06-lts-boot.img` / `5.15.211-android13-lts`

This is detected automatically from the `kernel_tag`'s own format (a dot immediately before the sub_level number, or a bare SHA) — you never need to flag a build as LTS by hand.

`matrix.json` entries mark these with `"lts": true` (see [Build Matrix](#build-matrix) below) purely for the summary table in CI — it has no effect on the build itself.

---

## Build Matrix

`matrix.json` (`.github/workflows/config/matrix.json`) is the single source of truth for which `sub_level`/`os_patch_level`/`kernel_tag` combinations exist. It is **not maintained by hand** — run:

```bash
cd .github/workflows/scripts
python3 update_matrix.py --dry-run   # preview changes
python3 update_matrix.py             # apply
```

This queries Google's `kernel/common` repository directly (`git ls-remote --tags`) for every tracked Android/kernel family, and tracks **both** tag schemes independently so a date-based respin and an LTS-merge respin can never collide even when they land under the same rough month:

- **Date-based tags** — finds the latest respin per month, resolves the real `sub_level` from each tag's `Makefile`, keyed/matched by `os_patch_level`.
- **LTS-merge tags** — `sub_level` is read straight from the tag itself (no `Makefile` fetch needed), keyed/matched by `sub_level` — capped to the 15 highest sub_levels found per family, since these tags carry no date to bound the lookback window by.

New entries are added as `"enabled": false` (opt-in); existing entries get their `kernel_tag`/`sub_level` refreshed without touching your `enabled` choices. A commit-SHA-pinned entry (see [LTS Builds](#lts-builds)) is left untouched until Google actually cuts the matching official tag, at which point it's automatically upgraded from the SHA to the real tag.

Tracked families: `android12-5.10`, `android13-5.15`, `android14-6.1`, `android15-6.6`, `android16-6.12`, `android17-6.18`. **Only `android13-5.15` is tested** (see scope note at top) — `android12` and `android14` entries are tracked and attempted but unverified; `android15-6.6` and newer are left with no enabled entries because they currently fail to build.

Each entry looks like:
```json
{"sub_level": "211", "os_patch_level": "2026-06", "kernel_tag": "android13-5.15.211_r00", "lts": true, "enabled": true}
```

---

## Command-Line Arguments

| Argument | Description | Default |
|------|------|--------|
| `--android`, `-a` | Android version (android12-android17) | android14 |
| `--kernel`, `-k` | Kernel version (5.10/5.15/6.1/6.6/6.12/6.18) | 6.1 |
| `--sub-level`, `-s` | Sub level version (e.g. `194`, `211`) | 124 |
| `--os-patch` | OS Patch Level | 2025-02 |
| `--kernel-tag` | Pin an exact GKI respin instead of the moving branch HEAD — accepts a date-based tag, an LTS-merge tag, or a raw commit SHA (see [Exact Source Pinning](#exact-source-pinning-kernel-tag)) | - |
| `--lts` | Explicitly mark the build as LTS-sourced. Rarely needed — auto-detected from `--kernel-tag`'s format (see [LTS Builds](#lts-builds)) | False (auto-detected) |
| `--ath9k` | Build ath9k_htc + ath9k_common + ath9k_hw + ath as out-of-tree modules for a TL-WN722N v1 (AR9271) USB adapter (see [ath9k_htc](#ath9k_htc--external-wifi-adapter-support---ath9k)) | False |
| `--revision` | Android 12 revision (used for certified-boot reference downloads) | - |
| `--ksu-version` | SukiSU-Ultra version (Stable/Dev) | Stable |
| `--ksu-commit` | Specify a SukiSU-Ultra commit hash | latest |
| `--susfs-commit` | Specify a SUSFS commit (hash or HEAD~N) | latest |
| `--zram` | Enable ZRAM (LZ4KD) | False |
| `--no-kpm` | Disable KPM | False |
| `--bbg` | Enable Baseband-guard | False |
| `--droidspaces` | Enable Droidspaces container-runtime support (android12/13/14 only — see [Droidspaces Support](#droidspaces-support)) | False |
| `--op8e` | Enable OnePlus 8E support | False |
| `--bbr-version` | Congestion control: `none`, `bbr1`, or `bbr3` (sets as system default) — `bbr3` only on android12/13/14, see [BBRv3 Support](#bbrv3-support) | bbr1 |
| `--disable-safemode` | Permanently disable KernelSU/SukiSU volume-key safe mode detection (most users rely on [YABP](https://github.com/Magisk-Modules-Repo/YetAnotherBootloopProtector) instead) | False |
| `--no-release` | Don't create a GitHub Release | False |
| `--custom-version` | Custom `CONFIG_LOCALVERSION` string | - |
| `--list-configs` | List supported Android/Kernel combinations | - |
| `--dry-run` | Only validate the configuration, don't build | - |
| `--workspace`, `-w` | Working directory | /tmp/gki-build |

---


## ath9k_htc — external WiFi adapter support (`--ath9k`)

Adds support for a **TP-Link TL-WN722N v1** (Atheros AR9271) USB WiFi adapter
over OTG — monitor mode and packet injection — while leaving the phone's
built-in WiFi fully working. Intended for WiFi security testing on **your own
networks** (Kali / NetHunter-style workflows, on the phone instead of a
laptop). Enable with `--ath9k` (on by default in `build-kernel.sh`).

### How it works (and why not the obvious way)

GKI ships **no** wireless stack — `gki_defconfig` defines neither
`CONFIG_CFG80211` nor `CONFIG_MAC80211`; the real stack is Qualcomm's,
loaded as vendor modules from `/vendor/lib/modules`. The naive fix
(`CFG80211=y`/`MAC80211=y`) builds a second copy of that symbol surface into
the image, which stops the vendor's `qca_cld3`/`kiwi_v2` from loading and
**kills internal WiFi**.

Instead, `--ath9k` builds `ath9k_htc` + `ath9k_common` + `ath9k_hw` + `ath`
as **out-of-tree modules** (`=m`, never `=y`) linked against the vendor
stack. The GKI image's wireless config is left untouched, so internal WiFi is
never disturbed. The built `cfg80211.ko`/`mac80211.ko` are deliberately **not**
packaged — the device uses Qualcomm's.

Post-build, the modules' exported-symbol CRCs are verified against the
device's actual vendor `cfg80211`/`mac80211` before release, so a module that
couldn't load never ships.

### Using it

The kernel alone isn't enough — the driver needs firmware and needs loading
at boot. That's handled by a separate flashable module,
**`ath9k_htc_vermeer.zip`** (attached to each release; source in
[`ath9k-module/`](ath9k-module/)).

1. Flash the kernel (built with `--ath9k`).
2. Install `ath9k_htc_vermeer.zip` in your root manager.
3. Reboot, plug the antenna in over OTG.

See [`ath9k-module/README.md`](ath9k-module/README.md) for details, including
the note that **the antenna LED won't light** (intentional — the vendor
`mac80211` doesn't export the LED-trigger symbols, and forcing them would stop
the driver loading; everything else works).

**Adapter must be v1.** The TL-WN722N v2 and v3 are a Realtek chip and will
not work with this driver.


---

## Downloads

1. **AnyKernel3.zip** — ready to flash!
   - Use a flashing tool such as [HorizonKernelFlasher](https://github.com/libxzr/HorizonKernelFlasher/releases) to flash the kernel

2. **boot.img** — download the format matching your kernel
   - Flash via `fastboot flash boot_ab <filename>`
   - Every boot image is AVB-signed with a canonical key kept in sync between local and CI builds (see [AVB Signing](#avb-signing) below).

3. **ath9k_htc_vermeer.zip** — the companion module for the ath9k adapter (see [ath9k_htc](#ath9k_htc--external-wifi-adapter-support---ath9k)). Only needed if you use a TL-WN722N v1.

---

## Supported Features

| Feature | Description |
|------|------|
| [KernelSU / SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | Kernel-level root solution |
| [SUSFS4](https://gitlab.com/simonpunk/susfs4ksu) | Kernel-level patches that assist KSU in hiding root |
| [ath9k_htc](ath9k-module/) | External TL-WN722N v1 (AR9271) WiFi adapter over OTG — monitor + injection (`--ath9k`). Ships as a flashable module. |
| BBR v1 | TCP congestion control algorithm |
| [BBRv3](https://github.com/WildKernels/kernel_patches/tree/main/common/bbrv3) | Newer TCP congestion control (`--bbr-version bbr3`) — android12/13/14 only. See [BBRv3 Support](#bbrv3-support) below. |
| [LZ4KD](https://github.com/ShirkNeko/SukiSU_patch/tree/main/other) | ZRAM compression algorithm sourced from Huawei's codebase |
| [KPM](https://github.com/bmax121/KernelPatch) | Kernel module support |
| [Baseband-guard](https://github.com/vc-teahouse/Baseband-guard) | Baseband security protection |
| [Droidspaces](https://github.com/ravindu644/Droidspaces-OSS) | Container-runtime support (`--droidspaces`) — real namespace isolation for full Linux distros with a working init system, not just chroot. See [Droidspaces Support](#droidspaces-support) below. |
| MGLRU / PSI | Modern memory reclaim + pressure metrics for smarter LMKD decisions |
| IP Set / CAKE | Netfilter IP grouping and bufferbloat-reducing queue discipline |
| Wireguard | Native in-kernel VPN support |
| Safe Mode Removal | Volume-key safe-mode detection permanently patched out (opt out by omitting `--disable-safemode`) |

<details>
<summary>Supported ZRAM algorithms (switchable in Scene)</summary>

LZ4K, LZ4HC, deflate, 842, lz4k_oplus

</details>

---

## BBRv3 Support

[BBRv3](https://github.com/WildKernels/kernel_patches/tree/main/common/bbrv3) is Google's newer TCP congestion control algorithm — an evolution of the widely-used BBRv1 already shipped by default. Backport patches are vendored from WildKernels (also the source of the Droidspaces integration above), pre-adjusted for Android kABI compliance. Enable it with `--bbr-version bbr3` (or the `BBR congestion control version` choice in either GitHub Actions workflow, or `BBR_VERSION="bbr3"` in `build-kernel.sh`).

**Currently wired up for `android12-5.10`, `android13-5.15`, and `android14-6.1` only** — though only `android13-5.15` is tested. (WildKernels also publish an `android15-6.6` variant; not vendored here since nothing currently built targets that branch.)

Two small prerequisite patches (`proc_dou8vec_minmax()` and a follow-up data-race fix, both from mainline `-stable`) are applied first if missing — on the sub_levels this project currently builds they're almost certainly already present via normal upstream updates, so this is expected to be a silent no-op in practice.

If the main BBRv3 patch doesn't apply cleanly (a branch's `net/ipv4` source has diverged too far from what the backport expects), the build **falls back to BBRv1** as the system default rather than silently leaving the kernel on cubic with no explanation — check the [patch status summary](#build-matrix) (a `FAIL` on `BBRv3` means this happened).

Verify after flashing:
```bash
su -c "sysctl net.ipv4.tcp_congestion_control"      # should print "bbr3"
su -c "sysctl net.ipv4.tcp_available_congestion_control"   # should list bbr3 among the options
```

---

## Droidspaces Support

[Droidspaces](https://github.com/ravindu644/Droidspaces-OSS) is a lightweight, LXC-like container runtime for Android — real Linux namespace isolation (PID, IPC, Mount) so a full Linux distro can run with its own genuine init system (systemd, OpenRC), instead of a plain chroot that just shares the host's process tree. Enable it with `--droidspaces` (or the `Enable Droidspaces` toggle in either GitHub Actions workflow, or `DROIDSPACES="1"` in `build-kernel.sh`).

**Currently wired up for `android12-5.10`, `android13-5.15`, and `android14-6.1`** — though only `android13-5.15` is tested. Google's GKI enforces a strict kABI (Kernel Module Interface) checksum on struct layouts, so naively enabling `CONFIG_SYSVIPC`/`CONFIG_IPC_NS`/`CONFIG_POSIX_MQUEUE` without a matching patch causes an **immediate bootloop**. This flag applies the upstream kABI-safe patch (moving the relevant fields into Android's reserved padding slots) before turning those options on — three slot-layout variants are vendored and tried in order (strict, no-fuzz matching) since which `ANDROID_KABI_RESERVE` slots are still free isn't identical across every branch/respin; whichever one actually fits this exact source tree is used. If none of the three fit, the build continues without Droidspaces rather than risking a silent kABI break (check the [patch status summary](#build-matrix) — a `FAIL` on `Droidspaces` means this happened).

Applied *after* SUSFS/SukiSU-Ultra in the build sequence deliberately — if there's ever a real conflict over the same kABI reserve slots, it's this optional feature that loses, never the project's core functionality.

Once built, verify and use it via the [Droidspaces Android app](https://github.com/ravindu644/Droidspaces-OSS) (Settings -> Requirements -> Check Requirements).

---

## AVB Signing

Every `boot.img` is signed with `avbtool` using a canonical RSA key so that images are consistently signed across local and CI (GitHub Actions) builds:

- **Locally:** if no key is found, one is auto-generated once at `<workspace>/boot_sign_key.pem` and reused for every subsequent build.
- **CI:** the same key content should be set as the `BOOT_SIGN_KEY` repository secret.

For most users on an unlocked bootloader this is a formality (AVB verification is typically bypassed).

---

## Emergency Recovery Guide

> **Trigger condition**
> Use this if the device fails to boot due to a bad or incompatible kernel flash

1. Enter Fastboot mode
   - Physical button combo: Power + Volume Down
   - Or ADB command: `adb reboot bootloader`

2. Run the flash command
```bash
fastboot flash boot_ab <full_boot.img_filename>
```

---

## Build System Architecture

```
.github/workflows/
├── config/
│   ├── matrix.json           # Build matrix - kept current by update_matrix.py
│   └── update_matrix.py      # Refreshes matrix.json from Google's kernel/common tags
├── scripts/
│   ├── build.py               # Main build script (CLI entry point)
│   ├── kernel_builder.py      # Core kernel build class
│   ├── config.py              # Configuration definitions and validation
│   ├── matrix_generator.py    # GitHub Actions matrix generator
│   ├── patch_summary.py       # Aggregates per-build patch status into one CI summary table
│   ├── release_generator.py   # Release notes generator
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
| `KernelBuilder` | Core kernel build class — handles cloning source, applying patches, compiling, and packaging |
| `BuildConfig` | Build configuration data class containing all build parameters |
| `update_matrix.py` | Queries Google's kernel/common tags directly and keeps matrix.json current |
| `matrix_generator.py` | Generates the build matrix for the GitHub Actions matrix build |
| `patch_summary.py` | Aggregates per-build patch application status into one summary table in the CI job summary |
| `release_generator.py` | Automatically generates Release notes |

### Repository Dependencies

| Repository | Purpose |
|------|------|
| [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | SukiSU-Ultra source code and setup script |
| [susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu) | SUSFS kernel patches |
| [SukiSU_patch](https://github.com/ShirkNeko/SukiSU_patch) | Additional SukiSU-Ultra patches (ZRAM, hooks) |
| [AnyKernel3](https://github.com/WildPlusKernel/AnyKernel3) | Generic flashable package template |
| [Baseband-guard](https://github.com/vc-teahouse/Baseband-guard) | Baseband security protection |

---
