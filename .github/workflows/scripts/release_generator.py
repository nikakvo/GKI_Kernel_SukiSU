#!/usr/bin/env python3
import json
import urllib.request
import ssl
import sys
from pathlib import Path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))


class ReleaseGenerator:
    def __init__(self):
        self.matrix_path = Path(__file__).parent.parent / "config" / "matrix.json"
        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def load_matrix(self) -> dict:
        with open(self.matrix_path, 'r') as f:
            return json.load(f)

    def _fetch_json(self, url: str) -> dict:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Python'})
            with urllib.request.urlopen(req, context=self.ssl_ctx) as response:
                return json.loads(response.read())
        except Exception:
            return {}

    def get_ksu_info(self) -> tuple:
        ksu_tag, ksu_commit = "latest", "unknown"
        tags = self._fetch_json("https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/git/refs/tags")
        if tags:
            ksu_tag = tags[-1]['ref'].split('/')[-1]
        ref = self._fetch_json("https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/git/ref/heads/main")
        if ref:
            ksu_commit = ref['object']['sha'][:7]
        return ksu_tag, ksu_commit

    def generate_body(self, lto_mode: str = "thin") -> str:
        lto_feature_line = "- Full LTO" if lto_mode == "full" else "- Thin LTO"
        if lto_mode == "full":
            lto_section = """- **Full LTO** — LLVM Full Link-Time Optimization (LTO) treats the entire kernel as a single translation unit during the final link, giving the compiler visibility across every object file at once for the most aggressive cross-module inlining, dead-code elimination, and devirtualization possible. Compared to Thin LTO, this squeezes out marginally better runtime performance and slightly smaller code size on the phone — the kernel itself runs a bit more efficiently. Only applies to the legacy `build.sh` branches (android12/android13) — Bazel/Kleaf branches (android14-6.1+) keep their own fixed LTO mode regardless (see this release's build summary if the matrix mixes both build methods).
  ```
  su -c "zcat /proc/config.gz | grep CONFIG_LTO_CLANG_FULL"
  ```
  Active if it shows `CONFIG_LTO_CLANG_FULL=y`"""
        else:
            lto_section = """- **Thin LTO** — LLVM Thin Link-Time Optimization (LTO) performs optimization across translation units, giving most of the runtime performance and code-size benefit of Full LTO on the phone, with only a marginal difference between the two once installed.
  ```
  su -c "zcat /proc/config.gz | grep CONFIG_LTO_CLANG_THIN"
  ```
  Active if it shows `CONFIG_LTO_CLANG_THIN=y`"""
        return f"""## Features
- SUSFS v2.3.0
- KPM Support (Kernel Patch Module)
- Manual Syscall Hooks
- Magic Mount Support
- BBR v3 Support
- BBG (Baseband-guard) Support
- ZRAM Support
- LZ4KD Compression Support
- MGLRU Support (Multi-Gen LRU)
- PSI Support (Pressure Stall Information)
- IP Set Support (netfilter IP/network grouping) - [ipset-arm64](https://github.com/nikakvo/ipset-arm64)
- CAKE Queue Discipline Support
- Wireguard Support
- NTSync Support (Winlator/Wine NT synchronization primitives)
- [Droidspaces](https://github.com/ravindu644/Droidspaces-OSS) Support (real container namespaces)
- Additional TCP Congestion Control Algorithms Support (BIC, Westwood, HTCP)
- TTL/Hop-Limit Target Support (netfilter)
- Connection Mark (connmark) Support (netfilter)
- CIFS/SMB Network Filesystem Support
- Ptrace Leak Fix (kernels < 5.16)
{lto_feature_line}

## Detailed explanation

- **SUSFS v2.2.0** — Addon for hiding root using kernel-level patches combined with a userspace module (hides suspicious paths, mount points, spoofs kernel stats/uname/cmdline, and more).

- **KPM Support (Kernel Patch Module)** — SukiSU KPM support is built into the kernel, allowing compatible KPM modules to be loaded at runtime.
  ```
  su -c "cat /proc/kallsyms | grep sukisu_kpm_version"
  ```
  Active if `sukisu_kpm_version` is listed.

- **Manual Syscall Hooks** — Low-level syscall interception method used for root management and detection evasion, offering finer control than standard hooking approaches.

- **Magic Mount Support** — Overlay-based mounting system that lets root modules modify the filesystem without altering the underlying partitions directly, improving compatibility and reducing detection surface.

- **BBR v3 Support (android12/13/14 only)** — Google's newer, improved successor to BBR v1 — better fairness with other flows and less bufferbloat under load. Backported via [WildKernels' kABI-compliant patch](https://github.com/WildKernels/kernel_patches/tree/main/common/bbrv3), selected in place of BBR v1 (not on top of it) via the build's `--bbr-version bbr3` option. Only wired up for `android12-5.10`/`android13-5.15`/`android14-6.1` so far, and depends on the patch applying cleanly on that specific branch/sub_level — check this release's build summary if unsure whether a given file has it; the build falls back to BBR v1 automatically if it doesn't apply.
  ```
  su -c cat /proc/sys/net/ipv4/tcp_congestion_control
  ```
  Active if output is `bbr3` (not `bbr`).

- **BBG (Baseband-guard) Support** — Lightweight LSM ([vc-teahouse/Baseband-guard](https://github.com/vc-teahouse/Baseband-guard)) that hooks the kernel write path to block unauthorized writes to the baseband/modem and other high-value protected partitions/device nodes, denying by default and logging every blocked attempt for traceability. Off unless the build was run with `--bbg`; check this release's build summary for whether a given file has it. Recovery/bootloader-partition protection (`CONFIG_BBG_BLOCK_BOOT`/`CONFIG_BBG_BLOCK_RECOVERY`) is deliberately left disabled regardless, since enabling it has caused real-world conflicts with kernel-zip flashing and recovery tools on other BBG-enabled kernels — only the core baseband protection is on.
  ```
  su -c "zcat /proc/config.gz | grep CONFIG_BBG"
  su -c "dmesg | grep -c baseband_guard"
  ```
  Active if `CONFIG_BBG=y` is shown and the dmesg count is non-zero (BBG logs a line every time it evaluates a process's SELinux domain).

- **LZ4KD Support** — Enhanced LZ4 compression algorithm for ZRAM, offering better compression ratios with minimal CPU overhead — improves effective RAM capacity by compressing swapped-out memory pages.
  ```
  su -c cat /sys/block/zram0/comp_algorithm
  ```
  Active if `[lz4kd]` appears in brackets.

- **MGLRU Support (Multi-Gen LRU, enabled by default)** — Modern memory reclaim algorithm that replaces the traditional active/inactive LRU lists with multiple generations based on page access recency. Results in more accurate reclaim decisions, fewer background apps being killed under memory pressure, and smoother multitasking.
  ```
  su -c cat /sys/kernel/mm/lru_gen/enabled
  ```
  Active if the value is non-zero (e.g. `0x0003`), not `0x0000`.

- **PSI Support (Pressure Stall Information)** — Kernel subsystem that reports real-time memory, CPU, and I/O pressure metrics (`/proc/pressure/*`). Allows the Low Memory Killer Daemon (LMKD) to make smarter kill decisions based on actual system pressure instead of coarse thresholds. Works in tandem with MGLRU.
  ```
  su -c cat /proc/pressure/memory
  ```
  Active if it prints `avg10=... avg60=... avg300=... total=...` instead of an error.

- **IP Set Support (netfilter IP/network grouping)** — Kernel-level support for `ipset`, allowing IP addresses, networks, and ports to be grouped into named sets for fast, efficient `iptables`/`ip6tables` matching. Enables O(1) hash-based lookups instead of linear rule scanning, and dynamic set updates without reloading the full firewall ruleset. *(Requires a separate userspace `ipset` binary — see [ipset-arm64](https://github.com/nikakvo/ipset-arm64), not bundled with this kernel.)*
  ```
  su -c "ipset create test hash:ip && ipset destroy test"
  ```
  Active if it runs with no "Kernel module not found" error.

- **CAKE Queue Discipline Support** — Modern queue management algorithm (`sch_cake`) that reduces bufferbloat and improves latency under load by combining fair queuing, active queue management, and traffic shaping in a single, easy-to-configure qdisc.
  ```
  su -c "tc qdisc add dev lo root cake && tc qdisc show dev lo && tc qdisc del dev lo root"
  ```
  Active if `qdisc show` lists `qdisc cake ...`.

- **Wireguard Support** — Built-in kernel-level support for the WireGuard VPN protocol, offering a lightweight, high-performance, and modern alternative to OpenVPN/IPsec.
  ```
  su -c "zcat /proc/config.gz | grep CONFIG_WIREGUARD"
  ```
  Active if it shows `CONFIG_WIREGUARD=y`.

- **NTSync Support (Winlator/Wine NT synchronization primitives)** — Kernel-level driver (`/dev/ntsync`) emulating Windows NT synchronization primitives (semaphores, mutexes, events) natively, instead of userspace emulation over futex. Improves compatibility and reduces overhead for Wine-based Windows app/game layers such as Winlator. Only available on branches with a compatible backport for that specific kernel version — not every branch/sub_level is guaranteed to have it.
  ```
  su -c ls -la /dev/ntsync
  su -c "zcat /proc/config.gz | grep CONFIG_NTSYNC"
  ```
  Active if `/dev/ntsync` exists (as a character device) and `CONFIG_NTSYNC=y` is shown.

- **Droidspaces Support (android12/13/14 only)** — Enables real Linux namespace isolation (PID, IPC, Mount, User) at the kernel level, the foundation for running a full Linux distro in a genuine isolated container — with its own real init system (systemd, OpenRC) — instead of a plain chroot that just shares the host's process tree. Use it via the [Droidspaces app](https://github.com/ravindu644/Droidspaces-OSS), which handles the actual container setup/management; this kernel just provides the underlying namespace support it needs. Only wired up for `android12-5.10`/`android13-5.15`/`android14-6.1` so far, and depends on a kABI-safe patch applying cleanly on that specific branch/sub_level — check this release's build summary if unsure whether a given file has it.
  ```
  su -c "zcat /proc/config.gz | grep -E 'CONFIG_SYSVIPC|CONFIG_POSIX_MQUEUE|CONFIG_IPC_NS|CONFIG_PID_NS|CONFIG_USER_NS|CONFIG_DEVTMPFS'"
  ```
  Active if all of them show `=y`. For a deeper functional check:
  ```
  su -c "unshare -pf echo namespace-test-ok"
  ```
  Active if it prints `namespace-test-ok` without an error.

- **Additional TCP Congestion Control Algorithms** — Adds BIC, TCP Westwood+, and H-TCP as selectable congestion control algorithms alongside the existing BBR/BBRv1/BBRv3/CUBIC/Reno options — doesn't change the system default (still BBR/BBRv3 via `--bbr-version`), just makes more algorithms available to switch to at runtime for different network conditions (Westwood in particular is tuned for lossy/wireless links, which can suit some mobile network + VPN tunnel combinations better than BBR).
  ```
  su -c cat /proc/sys/net/ipv4/tcp_available_congestion_control
  ```
  Active if `bic`, `westwood`, and `htcp` all appear in the list (switch to one with `su -c "sysctl net.ipv4.tcp_congestion_control=westwood"`).

- **TTL/Hop-Limit Target Support (netfilter)** — Kernel-level `iptables`/`ip6tables` target (`CONFIG_NETFILTER_XT_TARGET_HL`) that lets firewall rules rewrite a packet's TTL (IPv4) or Hop Limit (IPv6) field. Commonly used to normalize the TTL of tethered/hotspot traffic back to what it'd be if it originated directly from the device, since carriers often detect tethering by noticing the TTL decrement that happens when traffic is routed through another device.
  ```
  su -c "iptables -t mangle -A POSTROUTING -j TTL --ttl-set 65 && iptables -t mangle -D POSTROUTING -j TTL --ttl-set 65"
  ```
  Active if both commands run with no "No chain/target/match by that name" error.

- **Connection Mark (connmark) Support (netfilter)** — Kernel-level `iptables`/`ip6tables` target and match (`CONFIG_NETFILTER_XT_CONNMARK`) that lets firewall rules tag entire connections (not just individual packets) with a mark, so later packets belonging to the same connection can be matched and handled consistently. Used for advanced firewall, QoS, and policy-routing setups — often paired with the TTL target above for more robust tethering-passthrough rules.
  ```
  su -c "iptables -t mangle -A POSTROUTING -j CONNMARK --set-mark 1 && iptables -t mangle -D POSTROUTING -j CONNMARK --set-mark 1"
  ```
  Active if both commands run with no "No chain/target/match by that name" error.

- **CIFS/SMB Network Filesystem Support** — Kernel-level SMB3/CIFS client (`CONFIG_CIFS`), letting a Samba or Windows network share be mounted directly (`mount -t cifs //server/share /mnt/point`) instead of relying on an app-based SMB browser. Requires network connectivity to an actual SMB server to mount something real, but the driver itself is always present in the kernel regardless.
  ```
  su -c "cat /proc/filesystems | grep cifs"
  ```
  Active if `cifs` is listed.

- **Ptrace Leak Fix (kernels < 5.16)** — Backports an upstream Linux 5.16 hardening fix that closes a race where `ptrace_message` (e.g. a forked child's PID during a ptrace event) was briefly visible to other readers before the tracer was actually notified, or left stale after detach. Relevant on kernel 5.10/5.15 branches, where this isn't present natively; on 6.1+ branches it's already upstream, so nothing is patched there. There's no `/proc` or `/sys` flag to check this directly — it's a kernel-internal timing/security fix, not a toggle.

{lto_section}
"""

    def save_body(self, output_path: str = "RELEASE_BODY.md", lto_mode: str = "thin"):
        body = self.generate_body(lto_mode)
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w') as f:
            f.write(body)
        print(body)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Generate release body")
    parser.add_argument("output_path", nargs="?", default="RELEASE_BODY.md")
    parser.add_argument("--lto-mode", choices=["thin", "full"], default="thin",
                        help="Which LTO mode this release's builds used (legacy build.sh "
                             "branches only - see kernel_builder.py). Only matters when the "
                             "workflow's --lto-mode input was 'full'; defaults to 'thin' "
                             "otherwise.")
    args = parser.parse_args()
    ReleaseGenerator().save_body(args.output_path, args.lto_mode)
