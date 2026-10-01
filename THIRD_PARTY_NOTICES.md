# Third-party notices

This repository's own files — the build pipeline, workflows, scripts and
documentation — are licensed under the **GNU General Public License v3.0 or
later** (see [LICENSE](LICENSE)), Copyright (c) 2026 Tears Burn (nikakvo).

It was originally forked from
[ShirkNeko/GKI_KernelSU_SUSFS](https://github.com/ShirkNeko/GKI_KernelSU_SUSFS);
the `build.py` / `kernel_builder.py` architecture traces back to it. Credit for
that work belongs to its authors.

The components below are **not** covered by the license above and keep their
own terms.

## Kernel patches — `.github/workflows/scripts/patches/`

These patches modify the Linux kernel, so like the kernel they are licensed
under the **GNU General Public License v2.0** (GPL-2.0-only).

| Patches | Source |
|---|---|
| `bbrv3_*` | [WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches) — backport of Google's BBRv3 |
| `droidspaces_*` | [ravindu644/Droidspaces-OSS](https://github.com/ravindu644/Droidspaces-OSS), kABI variants adapted here |
| `ntsync_*`, `gki_ptrace.patch`, `unicode_bypass_fix_*` | community kernel patches — see the README's Credits |
| `ath9k_htc_no_mac80211_leds_android13-5.15.patch` | this repository |

## Firmware — `.github/workflows/scripts/firmware/ath9k_htc/htc_9271-1.4.0.fw`

The open-source Qualcomm Atheros ath9k_htc firmware, redistributed unmodified
under its own license. Source and license:
[qca/open-ath9k-htc-firmware](https://github.com/qca/open-ath9k-htc-firmware),
also shipped in linux-firmware as `LICENCE.open-ath9k-htc-firmware`.

## Fetched at build time (not stored in this repository)

The Linux kernel source (Android Common Kernel, GPL-2.0), SukiSU-Ultra,
susfs4ksu, AnyKernel3, the toolchains and every other component downloaded
during a build keep their own licenses.

## Released kernel images

The kernel images and AnyKernel3 packages published under Releases are builds
of the Linux kernel and are distributed under the **GPL-2.0**. Their
corresponding source is the Android Common Kernel commit stated in each
release's notes, plus the patches in this repository and the SukiSU-Ultra /
SUSFS commits pinned in `.github/workflows/scripts/config.py` for that release.
