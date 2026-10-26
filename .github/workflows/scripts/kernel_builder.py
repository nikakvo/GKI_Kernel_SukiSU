import os
import shutil
import subprocess
import logging
import re
import threading
import time as _time
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field

from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   DEFAULT_KSU_REF, SUSFS_UPSTREAM_URL, SUSFS_BRANCH_REPOS,
                   ANYKERNEL_CONFIG, KERNEL_PATCHES_CONFIG, BBG_CONFIG, TOOLCHAIN_CONFIG,
                   LEGACY_FIXES, OP8E_PATCH_URL)

# LEGACY KPM image patcher (SukiSU_patch kpm/patch_linux, KernelPatch 0.13.0),
# only used with KPM_LEGACY=1 (./build-kernel.sh --kpm-legacy) and only valid
# for SukiSU 41083 and older (no KPM bridge). Pinned to one commit and
# checked by sha256: it used to be fetched from `main` on every build, and the
# 2026-10-18 update (7c6289c, new kpimg + patch_linux) produced a kernel that
# bootloops at the vendor logo. 547ae94 (2026-03-15) is the patcher every
# release up to v41012 was built with. To move it: change both values together,
# then build and boot-test before releasing.
KPM_PATCH_REF = "547ae94bcaec53d030398f857950c64662043a5d"
KPM_PATCH_SHA256 = "1bd00563e9d8fbbd11a16c0c1c59c5add406e6c5c92557def50f93d6f0aebe2d"
KPM_PATCH_URL = f"https://raw.githubusercontent.com/ShirkNeko/SukiSU_patch/{KPM_PATCH_REF}/kpm/patch_linux"

# DEFAULT KernelPatch (0.13.9) since 2026-10-26 (was opt-in --kpm-next).
# KPM_LEGACY=1 / --kpm-legacy switches back to the 547ae94 patcher above;
# KPM_NEXT=0 does the same (old name, still read).
# SukiSU_patch 7c6289c ships a working kpm/kpimg (KernelPatch 0.13.9), but its
# kpm/patch_linux was built with an empty kpimg embedded (SukiSU_patch#13: a
# configure-time check in SukiSU_KernelPatch_patch patch/CMakeLists.txt always
# falls back to a 1-byte placeholder). patch_linux only runs
#   kptools -p -s 123 -i Image -k <kpimg> -o oImage
# so with KPM_NEXT we run exactly that ourselves: kptools built from
# SukiSU_KernelPatch_patch at a pinned commit, kpimg pinned by sha256.
# First pair (kpimg 7c6289c + kptools e60c6cf) was boot-tested on vermeer
# 2026-10-22 and shipped in v41042..v41083.
#
# 2026-10-26: SukiSU 3b58d46f ("unify KernelPatch bridge") and KernelPatch
# a3965c6 ("KPM bridge protocol") replaced the old stub hooks with a
# kernel-to-kernel bridge. The two sides must match, there is no fallback:
# old kpimg + new SukiSU (or the reverse) = KPM does not work. So kpimg and
# kptools moved together to the first build with the bridge:
#   kpimg   SukiSU_patch 1dface3 (compile time 21:46:21 Oct 25 2026, d09)
#   kptools SukiSU_KernelPatch_patch 40ce8be (preset.h MAP_MAX_SIZE 0x1000 -
#           the old kptools must not be used with this kpimg)
# KPM_NEXT_BRIDGE is the bridge both sides were built with; the build checks
# SukiSU's kernel/extension/kpm/bridge.h against it before compiling.
KPM_NEXT_KPIMG_REF = "1dface3879613a62057f2ef441d4a6a3ce214c16"
KPM_NEXT_KPIMG_SHA256 = "67c6dd4637b673b7e230d0fa36bfc2d895a2467cb7137107213a96f2b9781c91"
KPM_NEXT_KPIMG_URL = f"https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU_patch/{KPM_NEXT_KPIMG_REF}/kpm/kpimg"
KPM_NEXT_TOOLS_REPO = "https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch.git"
KPM_NEXT_TOOLS_REF = "40ce8bea970e25fac9926a220730cf5a2331e3d9"
KPM_NEXT_VERSION = "d09"   # KernelPatch 0.13.9: kptools -v and the kpimg must both say this
KPM_NEXT_BRIDGE = {
    "SUKISU_KPM_SUPERCALL_MAGIC": "0x53554b504d000002UL",
    "SUKISU_KPM_SUPERCALL_MAGIC2": "0x4b504d4252494447UL",
    "SUKISU_KPM_SUPERCALL_MAGIC3": "0x53554b494e544552UL",
    "SUKISU_KPM_ABI_VERSION": "1",
}
KPM_BRIDGE_H = "KernelSU/kernel/extension/kpm/bridge.h"   # relative to work_dir

# AmneziaWG (WireGuard fork with DPI obfuscation), built in (=y) next to the
# stock WireGuard. The commit is pinned in config.py (DEFAULT_AMNEZIAWG_REF)
# so check-release.sh can move it like the SukiSU/susfs pins; a one-off
# --amneziawg-commit <full sha> overrides it. Never built unpinned.
# What is changed in the upstream source: see prepare_amneziawg_src().
AMNEZIAWG_REPO = "https://github.com/amnezia-vpn/amneziawg-linux-kernel-module.git"
# Branches the integration has been compile- and boot-tested on. Others are
# skipped (and marked so in PATCH_STATUS.json), never half-built.
AMNEZIAWG_SUPPORTED = {("android13", "5.15")}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class AmneziaWGPrepareError(RuntimeError):
    """The upstream AmneziaWG source is in a shape prepare_amneziawg_src()
    does not know. The message says which step, in plain words."""


# (file, exact anchor, text inserted right after it). Each anchor must occur
# exactly once - anything else means upstream moved and a human looks first.
_AWG_FIXUPS = (
    ("Kbuild",
     "\ninclude $(src)/compat/Kbuild.include\n",
     "# Android GKI 5.15 lacks some helpers upstream 5.15 stable has; detect, do not guess.\n"
     "ifeq ($(shell grep -qw timer_delete_sync $(srctree)/include/linux/timer.h && echo y),)\n"
     "ccflags-y += -DAWG_GKI_NO_TIMER_DELETE_SYNC\n"
     "endif\n"),
    ("compat/compat.h",
     "#define timer_delete_sync(timer) del_timer_sync(timer)\n#endif\n",
     "\n/* Android GKI: detected from the kernel headers by Kbuild (AWG_GKI_*). */\n"
     "#if defined(AWG_GKI_NO_TIMER_DELETE_SYNC) && !defined(timer_delete_sync)\n"
     "#define timer_delete_sync(timer) del_timer_sync(timer)\n"
     "#endif\n"),
    ("compat/compat.h",
     "#define DEV_STATS_ADD(DEV, FIELD, VAL) DEV->stats.FIELD += VAL\n#endif\n",
     "\n/* Android GKI 5.15 has no DEV_STATS_INC/ADD (not taken from 5.15.86 stable). */\n"
     "#ifndef DEV_STATS_INC\n"
     "#define DEV_STATS_INC(DEV, FIELD) ++DEV->stats.FIELD\n"
     "#endif\n"
     "#ifndef DEV_STATS_ADD\n"
     "#define DEV_STATS_ADD(DEV, FIELD, VAL) DEV->stats.FIELD += VAL\n"
     "#endif\n"),
)


def prepare_amneziawg_src(src: Path, dest: Path) -> str:
    """Turns the module repo's src/ into an in-tree driver at dest
    (drivers/net/amneziawg). Shared by the build (add_amneziawg) and by
    check-release.sh's probe of a newer upstream commit, so the two can
    never disagree about what "fits". Returns the AmneziaWG version from
    src/version.h. Raises AmneziaWGPrepareError on any unknown shape.

    1. copy src/ minus tests/ (selftest/ stays: only built with
       AMNEZIAWG_DEBUG, which we keep off),
    2. three self-guarding fixups for Android GKI 5.15, which lacks two
       helpers upstream 5.15 stable has (DEV_STATS_INC/ADD and
       timer_delete_sync) - on a kernel that has them they do nothing,
    3. rename every wg_ identifier to awg_. AmneziaWG is a WireGuard fork
       and keeps WireGuard's global names (95 of them, all wg_*); built in
       next to CONFIG_WIREGUARD=y they are duplicate symbols at link time.
       The module has no EXPORT_SYMBOL, so nothing outside it sees a name
       change, and its netdev type and genl family are "amneziawg" anyway.
    """
    src, dest = Path(src), Path(dest)
    for need in ("Kbuild", "Kconfig", "version.h", "main.c", "compat/compat.h"):
        if not (src / need).is_file():
            raise AmneziaWGPrepareError(f"upstream src/ has no {need} - layout changed")
    m = re.search(r'^#define\s+WIREGUARD_VERSION\s+"([^"]+)"',
                  (src / "version.h").read_text(), re.M)
    if not m:
        raise AmneziaWGPrepareError("src/version.h: no WIREGUARD_VERSION line")
    version = m.group(1)

    if dest.exists():
        shutil.rmtree(dest)
    top = src.resolve()
    shutil.copytree(src, dest, ignore=lambda d, names:
                    ["tests"] if Path(d).resolve() == top else [])

    for rel, anchor, addition in _AWG_FIXUPS:
        f = dest / rel
        text = f.read_text()
        n = text.count(anchor)
        if n != 1:
            raise AmneziaWGPrepareError(
                f"{rel}: the line the Android fixup goes after was found {n} "
                f"time(s), expected exactly 1 ({anchor.strip()!r})")
        f.write_text(text.replace(anchor, anchor + addition, 1))

    sources = sorted(p for p in dest.rglob("*") if p.suffix in (".c", ".h") and p.is_file())
    texts = {p: p.read_text(errors="surrogateescape") for p in sources}
    wg_names = set()
    awg_names = set()
    for t in texts.values():
        wg_names.update(re.findall(r"\bwg_(\w+)", t))
        awg_names.update(re.findall(r"\bawg_(\w+)", t))
    clash = sorted(wg_names & awg_names)
    if clash:
        raise AmneziaWGPrepareError(
            "wg_ -> awg_ rename would merge names upstream keeps apart: "
            + ", ".join(f"wg_{c}/awg_{c}" for c in clash[:5]))
    aawg_before = sum(t.count("aawg_") for t in texts.values())
    renamed = 0
    for p, t in texts.items():
        new, n = re.subn(r"\bwg_", "awg_", t)
        if n:
            renamed += n
            p.write_text(new, errors="surrogateescape")
    after = {p: p.read_text(errors="surrogateescape") for p in sources}
    left = sum(len(re.findall(r"\bwg_", t)) for t in after.values())
    aawg_after = sum(t.count("aawg_") for t in after.values())
    if left or aawg_after != aawg_before or renamed == 0:
        raise AmneziaWGPrepareError(
            f"wg_ -> awg_ rename check failed ({left} wg_ left, "
            f"{aawg_after - aawg_before} aawg_ produced, {renamed} renamed)")
    return version


@dataclass
class BuildResult:
    success: bool
    config: BuildConfig
    message: str = ""
    artifacts: list = field(default_factory=list)
    build_time: Optional[float] = None


class ShellCommand:
    def __init__(self, cwd: Optional[str] = None, env: Optional[dict] = None):
        self.cwd = cwd
        self.env = env or os.environ.copy()

    def run(self, cmd: str, check: bool = True, capture_output: bool = False,
            shell: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        logger.info(f"Executing command: {cmd}")
        try:
            return subprocess.run(cmd, shell=shell, cwd=self.cwd, env=self.env,
                                capture_output=capture_output, text=True, timeout=timeout, check=check)
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {e.stderr or str(e)}")
            raise
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out: {cmd}")
            raise

    def run_with_callback(self, cmd: str, callback: Optional[Callable] = None) -> str:
        logger.info(f"Executing command: {cmd}")
        process = subprocess.Popen(cmd, shell=True, cwd=self.cwd, env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output_lines = []
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if callback:
                callback(line)
        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"Command failed")
        return "\n".join(output_lines)


class KernelBuilder:
    KERNEL_CONFIG_TEMPLATE = """
# === KernelSU Config ===
CONFIG_KSU=y
CONFIG_KPM=y

# === TMPFS Config ===
CONFIG_TMPFS_XATTR=y
CONFIG_TMPFS_POSIX_ACL=y

# === Network Config ===
CONFIG_IP_NF_TARGET_TTL=y
CONFIG_IP6_NF_TARGET_HL=y
CONFIG_IP6_NF_MATCH_HL=y

# === BBR Config ===
CONFIG_TCP_CONG_ADVANCED=y
CONFIG_TCP_CONG_BBR=y
CONFIG_NET_SCH_FQ=y
CONFIG_TCP_CONG_BIC=n
CONFIG_TCP_CONG_WESTWOOD=n
CONFIG_TCP_CONG_HTCP=n

# === Networking Improvements (IP Set / connmark / CAKE / fq_codel) ===
CONFIG_IP_SET=y
CONFIG_IP_SET_MAX=256
CONFIG_IP_SET_BITMAP_IP=y
CONFIG_IP_SET_BITMAP_IPMAC=y
CONFIG_IP_SET_BITMAP_PORT=y
CONFIG_IP_SET_HASH_IP=y
CONFIG_IP_SET_HASH_IPMARK=y
CONFIG_IP_SET_HASH_IPPORT=y
CONFIG_IP_SET_HASH_IPPORTIP=y
CONFIG_IP_SET_HASH_IPPORTNET=y
CONFIG_IP_SET_HASH_NET=y
CONFIG_IP_SET_HASH_NETPORT=y
CONFIG_IP_SET_HASH_NETIFACE=y
CONFIG_IP_SET_LIST_SET=y
CONFIG_NETFILTER_XT_SET=y
CONFIG_NF_CONNTRACK_MARK=y
CONFIG_NETFILTER_XT_TARGET_CONNMARK=y
CONFIG_NETFILTER_XT_MATCH_CONNMARK=y
CONFIG_NET_SCH_CAKE=y
CONFIG_NET_SCH_FQ_CODEL=y

# === SUSFS Config ===
# These are ALL nine Kconfig symbols susfs4ksu v2.3.0 actually defines.
# Verified against kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch
# on the gki-android13-5.15 branch, not copied from an older config.
#
# Four options that used to live here were removed after a device
# check showed them absent from /proc/config.gz on a built kernel:
#   CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT
#   CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT
#   CONFIG_KSU_SUSFS_TRY_UMOUNT
#   CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT
# Upstream deprecated them ("Deprecated add_try_umount cli,
# AUTO_ADD_SUS_KSU_DEFAULT_MOUNT and AUTO_ADD_SUS_BIND_MOUNT") and the
# behaviour is now unconditional, so the symbols no longer exist and
# Kconfig was dropping every one of them in silence. Do not re-add them
# without checking the current 10_enable_susfs_for_ksu.patch first.
#
# CONFIG_KSU_SUSFS_SUS_SU=n was removed for the same reason - that
# symbol is gone too, so the line was a no-op rather than a setting.
# (CONFIG_KSU_SUSFS_SUS_PATH is written separately in
# configure_kernel() because 6.6 needs it off - not repeated here, or
# the defconfig would carry the same symbol twice.)
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y

# === Wireguard Config (forced on regardless of the branch's own
# gki_defconfig default, since not every branch ships it enabled) ===
CONFIG_WIREGUARD=y

# === Additional TCP congestion control algorithms (selectable at
# runtime via `sysctl net.ipv4.tcp_congestion_control`; does NOT change
# the system default, which stays bbr3/bbr1 via --bbr-version - these
# just become available options alongside it). TCP_CONG_ADVANCED is
# already on for branches with BBRv3 wired in, which explicitly turns
# these off despite their own Kconfig defaults - so they need to be
# re-enabled explicitly here, same as BIC/CUBIC/Westwood/HTCP's own
# "default y/m" gets silently overridden otherwise.
CONFIG_TCP_CONG_BIC=y
CONFIG_TCP_CONG_WESTWOOD=y
CONFIG_TCP_CONG_HTCP=y

# === Netfilter TTL/Hop-Limit rewrite + connection marking (tethering
# TTL-passthrough bypass, advanced firewall/QoS/policy-routing setups) ===
CONFIG_NETFILTER_XT_TARGET_HL=y
CONFIG_NETFILTER_XT_CONNMARK=y

# === CIFS/SMB network filesystem client (kernel-level `mount -t cifs`
# support for Samba/Windows network shares) ===
CONFIG_CIFS=y

# === BTF / eBPF / FUSE-BPF (debugging + eBPF tooling, matches
# WildKernels' "BTF / eBPF / FUSE-BPF" feature) ===
# CONFIG_DEBUG_INFO_BTF is already on by default in stock gki_defconfig
# (Google added it years ago for libbpf-tools support) - listed here
# explicitly anyway so it's documented and pinned rather than relying
# on upstream's default never changing. CONFIG_BPF_EVENTS is also
# very likely already on by default (standard Android BPF tracing
# infra - netd, iorap, etc. depend on it) but costs nothing to pin
# explicitly too.
#
# NOTE: CONFIG_FUSE_BPF is deliberately NOT in this unconditional
# template - it only exists from android13 onwards (fs/fuse/backing.c
# does not exist at all on android12-5.10), and writing an undefined
# Kconfig symbol into gki_defconfig is silently dropped with no error.
# It's written conditionally in configure_kernel() instead, and paired
# with a matching conditional check in _verify_expected_objects().
CONFIG_DEBUG_INFO_BTF=y
CONFIG_BPF_EVENTS=y
"""

    # Optional networking additions, enabled together by --extra-net.
    #
    # Everything here was checked against v5.15's own Kconfig files
    # rather than copied from another project, because a symbol that
    # does not exist (or whose dependencies are unmet) is dropped by
    # Kconfig in complete silence - it lands in gki_defconfig, never
    # reaches .config, and the feature simply is not in the kernel.
    # _expected_config_symbols() re-checks every line below against the
    # produced .config so that failure mode cannot go unnoticed.
    #
    # One thing deliberately left out despite other GKI projects
    # shipping it: CONFIG_NFT_FIB* - NFT_FIB_INET has hard `depends on`
    # (not select) chains through NFT_FIB_IPV4/IPV6, and reverse-path
    # matching is not why anyone wants nftables here.
    EXTRA_NET_CONFIG_TEMPLATE = """
# === ipset: the four set types stock GKI leaves out ===
# The MAC-keyed types are the ones worth having - filtering by hardware
# address is not expressible with the ip/net types already enabled.
# IP_SET_MAX defaults to 256; nothing here needs 65534, but the counter
# is just an array bound and raising it costs nothing.
CONFIG_IP_SET_MAX=65534
CONFIG_IP_SET_HASH_IPMAC=y
CONFIG_IP_SET_HASH_MAC=y
CONFIG_IP_SET_HASH_NETNET=y
CONFIG_IP_SET_HASH_NETPORTNET=y

# === IPv6 NAT ===
# Dependencies are already satisfied on a stock GKI build: NF_NAT and
# NETFILTER_ADVANCED come in via IPv4 NAT (iptable_nat), IP6_NF_IPTABLES
# via ip6_tables, and nf_nat_masquerade is already compiled in for the
# IPv4 side. So this adds the ip6tables `nat` table and its MASQUERADE
# target and little else. Google leaves it off because Android does
# NAT64/464XLAT through clatd and never needs NAT66.
CONFIG_IP6_NF_NAT=y
CONFIG_IP6_NF_TARGET_MASQUERADE=y

# === nftables ===
# Coexists with iptables rather than replacing it - both register
# against the same netfilter hooks, and Android's netd keeps using the
# xtables path untouched. The reason to want it is userspace: current
# Debian/Kali ship /usr/sbin/iptables as xtables-nft-multi, so iptables
# commands inside a chroot are translated to nftables and fail outright
# without NF_TABLES in the kernel.
# NF_TABLES_INET selects NF_TABLES_IPV4 and NF_TABLES_IPV6, but they are
# listed explicitly so the dependency NFT_NAT needs is visible here
# rather than implied.
CONFIG_NF_TABLES=y
CONFIG_NF_TABLES_INET=y
CONFIG_NF_TABLES_IPV4=y
CONFIG_NF_TABLES_IPV6=y
CONFIG_NF_TABLES_NETDEV=y
# NFT_COMPAT is what lets nft reuse existing x_tables matches/targets -
# without it, translated iptables rules that reference xt extensions
# still fail.
CONFIG_NFT_COMPAT=y
CONFIG_NFT_CT=y
CONFIG_NFT_COUNTER=y
CONFIG_NFT_LIMIT=y
CONFIG_NFT_LOG=y
CONFIG_NFT_MASQ=y
CONFIG_NFT_NAT=y
CONFIG_NFT_REDIR=y
CONFIG_NFT_REJECT=y
CONFIG_NFT_REJECT_INET=y
CONFIG_NFT_HASH=y
CONFIG_NFT_NUMGEN=y
CONFIG_NFT_QUOTA=y
CONFIG_NFT_CONNLIMIT=y
CONFIG_NFT_SOCKET=y
CONFIG_NFT_TPROXY=y

# === odds and ends ===
# NET_ACT_CONNMARK needs NET_CLS_ACT (already on - act_api/cls_api are
# compiled in stock GKI) and NF_CONNTRACK_MARK (already set above).
CONFIG_NET_ACT_CONNMARK=y
CONFIG_INET_RAW_DIAG=y
# CIFS_POSIX depends on CIFS && CIFS_ALLOW_INSECURE_LEGACY &&
# CIFS_XATTR. It was skipped at first on the assumption that
# CIFS_ALLOW_INSECURE_LEGACY would have to be turned on for it - but a
# /proc/config.gz check on a built kernel showed stock GKI already
# ships that symbol enabled, so the dependency is met without touching
# anything. It adds POSIX extensions (real UID/GID, symlinks, device
# nodes) when mounting a Samba server that supports them; SMB1 is not
# involved either way.
CONFIG_CIFS_POSIX=y
CONFIG_CIFS_XATTR=y
"""

    # Branches that actually have FUSE-BPF (fs/fuse/backing.c). Google
    # introduced it on the android13 branches; android12-5.10's fs/fuse
    # has no backing.c at all, so both the Kconfig symbol and the
    # expected .o are absent there. Keyed by (android_version,
    # kernel_version) so this stays explicit rather than an inequality
    # that silently starts including a branch nobody checked.
    FUSE_BPF_UNSUPPORTED = {("android12", "5.10")}

    ZRAM_CONFIG_5_10 = "CONFIG_ZSMALLOC=y\nCONFIG_ZRAM=y\nCONFIG_MODULE_SIG=n\nCONFIG_CRYPTO_LZO=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
    ZRAM_CONFIG_COMMON = "CONFIG_CRYPTO_LZ4HC=y\nCONFIG_CRYPTO_LZ4K=y\nCONFIG_CRYPTO_LZ4KD=y\nCONFIG_CRYPTO_842=y\nCONFIG_CRYPTO_LZ4K_OPLUS=y\nCONFIG_ZRAM_WRITEBACK=y\n"

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
        self.workspace = Path(workspace)
        self.shell = ShellCommand(cwd=workspace)
        self.env = os.environ.copy()
        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.susfs_dir = self.workspace / "susfs4ksu"
        # Set by _apply_susfs_commit() when the susfs pin has the split
        # form "<kernel-ref>+<ksu-patch-ref>" - see _split_susfs_ref().
        self.susfs_ksu_patch_ref = None
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.kernel_patches_dir = self.workspace / "kernel_patches"
        self.toolchain_dir = self.workspace / "toolchain"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        self.detected_respin = None
        # Tracks the outcome of every patch/feature step so it can be
        # surfaced in a per-build report and, across the whole matrix, in
        # a single summary table in the GitHub Actions job summary -
        # instead of that only being visible by digging through each
        # build's raw log. Values: "applied", "skipped" (not requested /
        # not applicable to this branch), or "failed" (attempted but did
        # not apply cleanly - build may still continue depending on the
        # step).
        self.patch_status: dict = {}
        self._bbrv3_applied = False
        self._amneziawg_version: Optional[str] = None
        self._peak_mem_used_mb: Optional[float] = None
        self._setup_env()

    def _mark(self, name: str, status: str, detail: str = ""):
        self.patch_status[name] = {"status": status, "detail": detail}
        icon = {"applied": "OK", "skipped": "SKIP", "failed": "FAIL"}.get(status, status)
        logger.info(f"[patch-status] {name}: {icon}" + (f" ({detail})" if detail else ""))

    def _write_patch_status(self):
        """Writes PATCH_STATUS.json into work_dir - picked up by the
        workflow's 'Record build result' step and later aggregated across
        the whole matrix into one summary table."""
        import json as _json
        report_path = self.work_dir / "PATCH_STATUS.json"
        is_legacy = (self.work_dir / "build/build.sh").exists()
        data = {
            "config": self.config.config_name,
            "android_version": self.config.android_version,
            "kernel_version": self.config.kernel_version,
            "sub_level": self.config.sub_level,
            "os_patch_level": self.config.os_patch_level,
            "kernel_respin": self.detected_respin or "",
            "is_lts": self.is_lts_build,
            # thin/full only meaningful on the legacy build.sh path - the
            # config always carries a value, but Bazel branches ignore it
            # (mode is fixed upstream there, see kernel_builder.py's
            # build_kernel()), so record that explicitly rather than
            # showing a mode that wasn't actually honored.
            "lto_mode": self._reported_lto_mode(is_legacy),
            "peak_mem_gb": round(self._peak_mem_used_mb / 1024, 1) if self._peak_mem_used_mb else None,
            "patches": self.patch_status,
        }
        report_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        logger.info(f"Patch status written to: {report_path}")

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.shell.env = self.env

    def _run_cmd(self, cmd: str, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, **kwargs)

    def _chdir(self, path: Path):
        os.chdir(path)
        self.shell.cwd = str(path)

    def _resolve_susfs_pin(self) -> Optional[str]:
        """Picks which susfs4ksu ref to pin for THIS build's branch.

        susfs4ksu is not one linear history. It keeps a separate branch
        per GKI version (gki-android12-5.10, gki-android13-5.15, ...),
        and each branch's kernel_patches/ contains ONLY its own
        50_add_susfs_in_gki-<android>-<kernel>.patch. A commit hash is
        therefore meaningful within exactly one branch: checking out an
        android13 commit while building android12 lands on a tree whose
        only SUSFS patch is the android13 one, and the build dies later
        with "SUSFS patch file not found".

        So --susfs-commit accepts either form:

          bare ref          e.g. bca0d2333c1a...
                            applied to whichever branch this build uses,
                            but validated against it first (see
                            _apply_susfs_commit) - a mismatch is a hard
                            error, never a silent foreign checkout.

          per-branch map    e.g. "gki-android12-5.10=ec785f4,
                                  gki-android13-5.15=bca0d23"
                            each branch gets its own pin; branches not
                            listed build from their branch HEAD. This is
                            what makes one matrix run able to pin every
                            branch at once.

          split ref         e.g. 687d2d1+e565931 (also inside a map:
                            gki-android13-5.15=687d2d1+e565931)
                            kernel side (50_add_susfs patch, fs/susfs.c,
                            susfs.h) from the first ref, KernelSU side
                            (KernelSU/10_enable_susfs_for_ksu.patch)
                            from the second. For when upstream rewrites
                            the KernelSU patch for tiann/KernelSU and it
                            stops fitting SukiSU, while the kernel-side
                            fixes after it are still wanted. Both refs
                            must be on this build's susfs branch.
        """
        raw = (self.config.susfs_commit or "").strip()
        if not raw:
            return None
        if "=" not in raw:
            return raw

        pins = {}
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            branch, sep, ref = part.partition("=")
            if not sep or not branch.strip() or not ref.strip():
                raise RuntimeError(
                    f"Could not parse --susfs-commit entry '{part}'. Expected "
                    f"either a bare commit ref, or a comma-separated list of "
                    f"<susfs-branch>=<ref> pairs, e.g. "
                    f"'gki-android12-5.10=ec785f4,gki-android13-5.15=bca0d23'."
                )
            pins[branch.strip()] = ref.strip()

        ref = pins.get(self.config.kernel_branch)
        if ref is None:
            logger.info(
                f"--susfs-commit has no entry for {self.config.kernel_branch} "
                f"(entries given: {', '.join(sorted(pins))}) - building this "
                f"branch from its susfs4ksu branch HEAD instead."
            )
        return ref

    @staticmethod
    def _split_susfs_ref(ref: str) -> tuple:
        """'687d2d1+e565931' -> ('687d2d1', 'e565931'); 'abc' -> ('abc', None)."""
        kernel_ref, sep, ksu_ref = ref.partition("+")
        kernel_ref, ksu_ref = kernel_ref.strip(), ksu_ref.strip()
        if sep and (not kernel_ref or not ksu_ref or "+" in ksu_ref):
            raise RuntimeError(
                f"Could not parse --susfs-commit '{ref}'. The split form is "
                f"<kernel-side ref>+<KernelSU-patch ref>, e.g. 687d2d1+e565931."
            )
        return kernel_ref, (ksu_ref or None)

    def _resolve_susfs_ref_on_branch(self, pin: str, what: str) -> str:
        """rev-parse `pin` in the susfs checkout (cwd) and make sure it is
        on this build's susfs branch. Returns the full SHA."""
        branch = self.config.kernel_branch
        resolved = self._run_cmd(f"git rev-parse --verify '{pin}^{{commit}}'",
                                 check=False, capture_output=True)
        if resolved.returncode != 0:
            raise RuntimeError(
                f"--susfs-commit {what} '{pin}' does not resolve to a commit in "
                f"susfs4ksu ({self.susfs_dir}). Check the hash."
            )
        sha = (resolved.stdout or "").strip()

        on_branch = self._run_cmd(f"git merge-base --is-ancestor {sha} origin/{branch}",
                                  check=False, capture_output=True)
        if on_branch.returncode != 0:
            owners = self._run_cmd(
                f"git branch -r --contains {sha} --format='%(refname:short)'",
                check=False, capture_output=True,
            )
            owner_list = [
                l.strip() for l in (owners.stdout or "").splitlines()
                if l.strip() and "->" not in l
            ]
            where = (f"That commit is on: {', '.join(owner_list)}."
                     if owner_list else
                     "Could not determine which branch that commit belongs to.")
            raise RuntimeError(
                f"--susfs-commit '{pin}' is not on susfs4ksu's "
                f"'{branch}' branch, which is the branch this "
                f"{self.config.android_version}-{self.config.kernel_version} "
                f"build needs.\n"
                f"{where}\n"
                f"susfs4ksu keeps one branch per GKI version and each holds "
                f"only its own 50_add_susfs_in_gki-*.patch, so a commit hash "
                f"is valid for exactly one branch. Checking this one out "
                f"anyway would produce a tree with the wrong SUSFS patch in "
                f"it, so the build stops here instead.\n"
                f"Either pass the equivalent commit on '{branch}', or use "
                f"the per-branch form to cover several branches in one run:\n"
                f"  --susfs-commit '{branch}=<ref>,gki-android13-5.15=<ref>'\n"
                f"Browse the branch at: "
                f"https://github.com/ShirkNeko/susfs4ksu/commits/{branch}"
            )

        return sha

    def _apply_susfs_commit(self):
        if not self.susfs_dir.exists():
            return
        pin = self._resolve_susfs_pin()
        if not pin:
            return
        kernel_pin, ksu_pin = self._split_susfs_ref(pin)

        branch = self.config.kernel_branch
        prev_cwd = self.shell.cwd
        self._chdir(self.susfs_dir)
        try:
            self._run_cmd("git fetch origin", check=False)

            if kernel_pin.startswith("HEAD~"):
                # Relative to the branch that was just checked out by
                # clone_repositories(), so it is branch-correct by
                # construction - nothing to validate.
                self._run_cmd(f"git reset --hard {kernel_pin}", check=False)
                logger.info(f"susfs4ksu pinned to {kernel_pin} on {branch}")
            else:
                sha = self._resolve_susfs_ref_on_branch(kernel_pin, "ref")
                self._run_cmd(f"git checkout {sha}", check=False)
                logger.info(f"susfs4ksu pinned to {sha[:12]} on {branch}")

            if ksu_pin:
                # Resolved AFTER the checkout above, so a HEAD~N here is
                # relative to the pinned kernel-side commit.
                ksu_sha = self._resolve_susfs_ref_on_branch(ksu_pin, "KernelSU-patch ref")
                rel = "kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch"
                has = self._run_cmd(f"git cat-file -e {ksu_sha}:{rel}",
                                    check=False, capture_output=True)
                if has.returncode != 0:
                    raise RuntimeError(
                        f"susfs4ksu {ksu_sha[:12]} has no {rel} - cannot take the "
                        f"KernelSU patch from it."
                    )
                self.susfs_ksu_patch_ref = ksu_sha
                logger.info(
                    f"susfs4ksu split pin: kernel side from the commit above, "
                    f"KernelSU/10_enable_susfs_for_ksu.patch from {ksu_sha[:12]}"
                )
        finally:
            self._chdir(Path(prev_cwd))

    def _susfs_remote_for_branch(self) -> str:
        """Which susfs4ksu remote actually carries this build's branch.

        SUSFS_BRANCH_REPOS overrides per kernel branch; everything else
        uses the configured default fork. See config.py for why this
        exists (ShirkNeko's GitHub fork stops at gki-android15-6.6)."""
        url = SUSFS_BRANCH_REPOS.get(
            self.config.kernel_branch, SUSFS_REPO_CONFIG["repo_url"]
        )
        if url != SUSFS_REPO_CONFIG["repo_url"]:
            logger.info(f"SUSFS for {self.config.kernel_branch} resolved to {url}")
        return url

    def _verify_susfs_branch_layout(self):
        """Confirms the susfs4ksu checkout actually carries the SUSFS
        patch this build needs, before the (multi-minute) kernel source
        sync rather than after it. The mismatch this catches is almost
        always a branch/pin mix-up, and finding that out two minutes
        earlier with a message naming what IS present is worth the two
        lines it costs."""
        patches_dir = self.susfs_dir / "kernel_patches"
        expected = patches_dir / self.config.get_susfs_patch_filename()
        if expected.exists():
            return
        available = sorted(p.name for p in patches_dir.glob("50_add_susfs_in_gki-*.patch")) \
            if patches_dir.exists() else []
        prev_cwd = self.shell.cwd
        self._chdir(self.susfs_dir)
        head = self._run_cmd("git log -1 --format='%h %s'", check=False, capture_output=True)
        self._chdir(Path(prev_cwd))
        raise RuntimeError(
            f"The susfs4ksu checkout does not contain "
            f"{self.config.get_susfs_patch_filename()}, which this "
            f"{self.config.android_version}-{self.config.kernel_version} build "
            f"needs.\n"
            f"susfs4ksu is currently at: {(head.stdout or '').strip() or '(unknown)'}\n"
            f"SUSFS patches present in that checkout: "
            f"{', '.join(available) if available else '(none)'}\n"
            f"Each susfs4ksu branch ships only its own GKI version's patch, so "
            f"this means the checkout is on the wrong branch - usually a "
            f"--susfs-commit pin taken from a different branch."
        )

    def clone_repositories(self):
        logger.info("=== Cloning repositories ===")

        def _clone_susfs_with_fallback(url, branch, exit_code):
            """ShirkNeko's fork only carries branches up to
            gki-android15-6.6. Rather than dying outright when a newer
            branch is missing, retry against upstream (GitLab), which is
            what WildKernels track and where gki-android16-6.12 lives.
            Commit hashes are shared between the two for branches both
            carry, so an existing --susfs-commit pin stays valid."""
            if url == SUSFS_UPSTREAM_URL:
                return None
            logger.warning(
                f"SUSFS branch '{branch}' not available on {url} "
                f"(exit {exit_code}) - retrying against upstream "
                f"{SUSFS_UPSTREAM_URL}"
            )
            cmd = f"git clone {SUSFS_UPSTREAM_URL} {self.susfs_dir.name} -b {branch}"
            return self._run_cmd(cmd, check=False)

        for name, repo_dir, url, branch in [
            ("SUSFS", self.susfs_dir, self._susfs_remote_for_branch(), self.config.kernel_branch),
            ("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG['repo_url'], None),
            ("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG['repo_url'], ANYKERNEL_CONFIG['branch']),
            ("Kernel Patches", self.kernel_patches_dir, KERNEL_PATCHES_CONFIG['repo_url'], None),
        ]:
            if not repo_dir.exists():
                cmd = f"git clone {url}"
                if branch:
                    cmd += f" -b {branch}"
                logger.info(f"Cloning {name}...")
                result = self._run_cmd(cmd, check=False)
                if name == "SUSFS" and result.returncode != 0:
                    retry = _clone_susfs_with_fallback(url, branch, result.returncode)
                    if retry is not None and retry.returncode == 0:
                        logger.info(f"SUSFS branch '{branch}' cloned from upstream instead")
                        result = retry
                if name == "SUSFS" and result.returncode != 0:
                    raise RuntimeError(
                        f"Failed to clone SUSFS branch '{branch}' from {url} "
                        f"(git clone exit code {result.returncode}).\n"
                        f"This branch may not exist yet on this fork - susfs4ksu "
                        f"forks can lag behind upstream for newer Android/kernel "
                        f"combos (e.g. ShirkNeko's fork didn't have "
                        f"gki-android16-6.12 for a while after it existed "
                        f"upstream). Check: {url.replace('.git', '')}/branches\n"
                        f"Failing here instead of continuing into a long kernel "
                        f"repo sync that would fail later anyway."
                    )
            else:
                logger.info(f"{name} already exists, skipping clone")
                if not branch:
                    # SukiSU_patch and kernel_patches are cloned without an
                    # explicit branch, and used to fall through this whole
                    # block untouched - so the very first clone in a
                    # workspace was reused forever. That is not harmless:
                    # SukiSU_patch supplies 69_hide_stuff.patch and the
                    # LZ4KD ZRAM sources, so a months-old checkout means
                    # silently building old lz4kd, or a hide_stuff patch
                    # written against a different susfs4ksu shape. Refresh
                    # to the remote's default branch instead, and log the
                    # resolved commit below either way so the build record
                    # says exactly what went in.
                    self._chdir(repo_dir)
                    self._run_cmd("git fetch origin", check=False)
                    self._run_cmd("git reset --hard @{u}", check=False)
                    self._chdir(self.workspace)
                if branch:
                    # This clone is reused across builds (cleanup-workspace.sh
                    # keeps it on purpose for speed), but the branch this
                    # build needs (e.g. SUSFS's branch depends on the
                    # android/kernel combo) may differ from whatever branch
                    # a previous build last left it on. Make sure it's on
                    # the right one before continuing, instead of silently
                    # using whatever happens to be checked out.
                    self._chdir(repo_dir)
                    fetch_result = self._run_cmd(f"git fetch origin {branch}", check=False)
                    self._run_cmd(f"git checkout {branch}", check=False)
                    self._run_cmd(f"git reset --hard origin/{branch}", check=False)
                    self._chdir(self.workspace)
                    if name == "SUSFS" and fetch_result.returncode != 0:
                        raise RuntimeError(
                            f"Failed to fetch SUSFS branch '{branch}' from {url} "
                            f"(git fetch exit code {fetch_result.returncode}).\n"
                            f"This branch may not exist yet on this fork - susfs4ksu "
                            f"forks can lag behind upstream for newer Android/kernel "
                            f"combos (e.g. ShirkNeko's fork didn't have "
                            f"gki-android16-6.12 for a while after it existed "
                            f"upstream). Check: {url.replace('.git', '')}/branches\n"
                            f"Failing here instead of continuing into a long kernel "
                            f"repo sync that would fail later anyway."
                        )
        self._apply_susfs_commit()
        self._verify_susfs_branch_layout()
        self._log_shared_repo_revisions()
        logger.info("=== Repository cloning complete ===")

    def _log_shared_repo_revisions(self):
        """Records the exact commit each shared support repo is sitting
        on. These live in the workspace and are reused across builds, so
        without this the log says which repos were used but not which
        version of them - and "it built yesterday" stops being a
        reproducible statement."""
        logger.info("Shared repository revisions used for this build:")
        for name, repo_dir in [
            ("susfs4ksu", self.susfs_dir),
            ("SukiSU_patch", self.sukisu_patch_dir),
            ("AnyKernel3", self.anykernel_dir),
            ("kernel_patches", self.kernel_patches_dir),
        ]:
            if not repo_dir.exists():
                logger.info(f"  {name}: (not present)")
                continue
            prev_cwd = self.shell.cwd
            self._chdir(repo_dir)
            rev = self._run_cmd("git log -1 --format='%h %cs %s'", check=False, capture_output=True)
            self._chdir(Path(prev_cwd))
            line = (rev.stdout or "").strip() or "(unknown)"
            logger.info(f"  {name}: {line}")

    def clone_toolchain(self):
        logger.info("=== Cloning toolchain ===")
        if not self.toolchain_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/kernel/prebuilts/build-tools "
                         f"-b {TOOLCHAIN_CONFIG['build_tools_branch']} --depth 1 {self.toolchain_dir}", check=False)
        if not self.mkbootimg_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/platform/system/tools/mkbootimg "
                         f"-b {TOOLCHAIN_CONFIG['mkbootimg_branch']} --depth 1 {self.mkbootimg_dir}", check=False)
        self.env["AVBTOOL"] = str(self.toolchain_dir / "linux-x86/bin/avbtool")
        self.env["MKBOOTIMG"] = str(self.mkbootimg_dir / "mkbootimg.py")
        self.env["UNPACK_BOOTIMG"] = str(self.mkbootimg_dir / "unpack_bootimg.py")
        if "BOOT_SIGN_KEY_PATH" in os.environ:
            self.env["BOOT_SIGN_KEY_PATH"] = os.environ["BOOT_SIGN_KEY_PATH"]
        else:
            self.env["BOOT_SIGN_KEY_PATH"] = str(self._ensure_local_avb_key())
        self.shell.env = self.env
        logger.info("=== Toolchain ready ===")

    def _ensure_local_avb_key(self) -> Path:
        """Reuse a canonical local AVB RSA key for boot.img signing when no
        BOOT_SIGN_KEY_PATH is provided (e.g. local builds outside CI, where
        the GitHub Actions secret BOOT_SIGN_KEY doesn't exist). If the
        canonical key (workspace/boot_sign_key.pem) hasn't been placed there
        yet, generate a throwaway one as a safety net so the build doesn't
        fail - but for consistency with GitHub releases, copy the same key
        you use as the BOOT_SIGN_KEY secret into this exact path once."""
        key_path = self.workspace / "boot_sign_key.pem"
        if not key_path.exists():
            logger.info(f"No BOOT_SIGN_KEY_PATH set and no canonical key found - generating one: {key_path}")
            logger.info("For consistency with GitHub releases, replace this file with your canonical BOOT_SIGN_KEY.")
            self._run_cmd(f"openssl genrsa -out {key_path} 2048", check=False)
        return key_path

    def setup_repo_tool(self):
        logger.info("=== Installing repo tool ===")
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        repo_path = repo_dir / "repo"
        if not repo_path.exists():
            self._run_cmd(f"curl https://storage.googleapis.com/git-repo-downloads/repo > {repo_path}", check=False)
            self._run_cmd(f"chmod a+rx {repo_path}", check=False)
        self.env["REPO"] = str(repo_path)
        self.shell.env = self.env

    def init_and_sync_kernel(self):
        logger.info("=== Initializing and syncing kernel source ===")
        self._chdir(self.work_dir)
        formatted_branch = self.config.formatted_branch

        self._run_cmd(f"$REPO init --depth=1 -u https://android.googlesource.com/kernel/manifest "
                     f"-b common-{formatted_branch} --repo-rev=v2.16", check=False)

        remote = subprocess.run(f"git ls-remote https://android.googlesource.com/kernel/common {formatted_branch}",
                               shell=True, capture_output=True, text=True).stdout.strip()
        if "deprecated" in remote:
            manifest_path = self.work_dir / ".repo/manifests/default.xml"
            with open(manifest_path, "r") as f:
                content = f.read()
            content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
            with open(manifest_path, "w") as f:
                f.write(content)

        self.env["REMOTE_BRANCH"] = remote
        logger.info("Syncing kernel source...")
        self._run_cmd("$REPO --trace sync -c -j$(nproc --all) --no-tags --fail-fast", check=False)

        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError("repo sync failed, common directory does not exist")
        self._apply_legacy_fixes(remote)

        if self.config.kernel_tag:
            self._checkout_kernel_tag(common_dir)

        logger.info("=== Kernel source sync complete ===")

    def _checkout_kernel_tag(self, common_dir: Path):
        """Pin kernel/common to a specific respin (e.g.
        android13-5.15-2025-12_r10) OR a raw commit SHA. repo sync runs
        with --no-tags, so a tag must be fetched explicitly; a SHA is
        fetched directly since it's not a ref at all.

        SHA support exists because Google sometimes lands an LTS-merge
        commit (e.g. "Merge 5.15.211 into android13-5.15-lts") on the
        branch well before cutting the corresponding official
        "androidX-Y.YY.NNN_r00" tag for it - a SHA lets a build pin that
        exact commit immediately instead of waiting on the tag.

        Fails the build loudly if the tag/SHA doesn't exist upstream or
        the checkout otherwise fails - silently falling through here
        would leave the source on whatever repo sync's moving-HEAD
        checkout happened to be (a real, differently-numbered sub_level),
        while every downstream filename/version string still confidently
        labels the build with the WRONG, requested sub_level. A build
        that silently compiles the wrong kernel and calls it the right
        one is much worse than a build that fails clearly.
        """
        ref = self.config.kernel_tag
        self._chdir(common_dir)

        if self._is_commit_sha(ref):
            logger.info(f"=== Pinning kernel source to commit SHA: {ref} ===")
            fetch_result = self._run_cmd(
                f"git fetch --depth=1 https://android.googlesource.com/kernel/common {ref}",
                check=False)
            if fetch_result.returncode != 0:
                self._chdir(self.work_dir)
                raise RuntimeError(
                    f"kernel_tag '{ref}' looks like a commit SHA but could "
                    f"not be fetched from android.googlesource.com/kernel/common "
                    f"- it may not exist, be too short/ambiguous, or not yet "
                    f"be reachable from a branch the server exposes for "
                    f"direct SHA fetch. Refusing to silently continue on "
                    f"the moving branch HEAD."
                )
            result = self._run_cmd("git checkout FETCH_HEAD", check=False)
            self._chdir(self.work_dir)
            if result.returncode != 0:
                raise RuntimeError(
                    f"commit SHA '{ref}' was fetched but 'git checkout "
                    f"FETCH_HEAD' failed. Refusing to silently continue "
                    f"on the moving branch HEAD."
                )
            return result

        tag = ref
        logger.info(f"=== Pinning kernel source to tag: {tag} ===")
        fetch_result = self._run_cmd(
            f"git fetch --depth=1 https://android.googlesource.com/kernel/common "
            f"refs/tags/{tag}:refs/tags/{tag}", check=False)
        if fetch_result.returncode != 0:
            self._chdir(self.work_dir)
            raise RuntimeError(
                f"kernel_tag '{tag}' could not be fetched from "
                f"android.googlesource.com/kernel/common - it likely "
                f"doesn't exist upstream (typo, not published yet, or "
                f"only landed as a commit so far without an official "
                f"tag - in that case pass the commit SHA instead). "
                f"Refusing to silently continue on the moving branch "
                f"HEAD, which would compile a DIFFERENT, real sub_level "
                f"while every artifact filename and on-device version "
                f"string still claims to be '{self.config.sub_level}'. "
                f"Verify the tag at https://android.googlesource.com/kernel/common/+refs "
                f"before retrying."
            )
        result = self._run_cmd(f"git checkout {tag}", check=False)
        self._chdir(self.work_dir)
        if result.returncode != 0:
            raise RuntimeError(
                f"kernel_tag '{tag}' was fetched but 'git checkout {tag}' "
                f"failed - the ref may be corrupt or ambiguous. Refusing "
                f"to silently continue on the moving branch HEAD."
            )
        return result

    @staticmethod
    def _is_commit_sha(value: str) -> bool:
        """True for a bare git commit SHA (short or full, 7-40 hex
        chars) - as opposed to a tag name like
        'android13-5.15-2026-06_r4' or 'android13-5.15.211_r00', which
        always contain non-hex characters ('android', '-', '_')."""
        return bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", value))

    # Matches the newer per-sublevel LTS-merge tag scheme
    # (android13-5.15.209_r00) - as opposed to the older date-based
    # scheme (android13-5.15-2026-06_r4). The dot right before the
    # sub_level number is what distinguishes the two; the dash scheme
    # never has a literal dot there.
    _LTS_DOT_TAG_RE = re.compile(r'^android1[2-7]-\d+\.\d+\.\d+_r\d+$')

    @property
    def is_lts_build(self) -> bool:
        """Whether this build is sourced from an LTS-merge respin,
        for filename/on-device-version purposes (the "-lts" marker).

        Auto-detected from the kernel_tag's own format whenever
        possible - a raw commit SHA or a dot-style tag can ONLY come
        from the LTS scheme, so inferring it here means correctness
        doesn't depend on a human also remembering to flip a separate
        --lts flag/matrix.json field in sync with kernel_tag (which is
        exactly what caused two real builds to come out unmarked -
        --lts / matrix.json's "lts" was left at its default while only
        kernel_tag got updated). config.is_lts_build (the explicit
        flag) is still honored as an override for edge cases, but is no
        longer load-bearing for the common case.
        """
        if self.config.is_lts_build:
            return True
        tag = self.config.kernel_tag or ""
        if self._is_commit_sha(tag):
            return True
        if self._LTS_DOT_TAG_RE.match(tag):
            return True
        return False

    def _write_scmversion(self):
        """Writes the exact desired release suffix directly into
        .scmversion. setlocalversion uses this file's content verbatim
        instead of calling `git describe`, giving full, predictable
        control over the final kernel release string (e.g.
        "5.15.194-android13-r10") regardless of build system (legacy
        build.sh vs Bazel/Kleaf) - the separate CONFIG_LOCALVERSION/
        custom_version mechanisms elsewhere in this file are gated
        inconsistently between the two build paths, so this is the one
        reliable, universal way to control it.

        Runs for every build, not just ones with an explicit
        --kernel-tag: self.detected_respin is populated by
        _detect_kernel_respin() either way (pinned tag or a remote
        lookup against the moving branch HEAD), so a build that just
        tracks HEAD still gets a clean "-r10"-style suffix instead of
        git's default raw commit-hash suffix (e.g. "-8-gd37b0095da55")."""
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return
        if not self.detected_respin and not self.is_lts_build:
            return
        respin_suffix = f"-{self.config.android_version}"
        if self.detected_respin:
            respin_suffix += f"-{self.detected_respin}"
        if self.is_lts_build:
            respin_suffix += "-lts"
        (common_dir / ".scmversion").write_text(respin_suffix)
        logger.info(f".scmversion written: {respin_suffix}")

    def _detect_kernel_respin(self):
        """Determines which respin (e.g. 'r10') the checked-out
        kernel/common commit actually corresponds to, so it can be
        reflected in the final artifact filenames. This matters because
        the same sub_level/os_patch_level combo can get rebuilt weeks
        later against a newer respin without any other visible
        difference in the filename otherwise - users downloading from
        GitHub currently have no way to tell those two builds apart.

        Works whether the build pinned an explicit --kernel-tag or is
        tracking the moving branch HEAD. For the latter case, local git
        tags are NOT available here (init_and_sync_kernel() runs repo
        sync with --no-tags for speed), so instead we ask the *remote*
        directly which respin tag(s) exist for this exact android/
        kernel/os_patch_level combo, and compare their commit SHAs
        against our checked-out HEAD - a single narrow ls-remote query,
        not a full tag fetch."""
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return

        if self.config.kernel_tag:
            if self._is_commit_sha(self.config.kernel_tag):
                # No official respin tag exists yet for a raw-SHA pin by
                # definition, so there's no clean "rNN" number to show.
                # Leave detected_respin unset rather than stuffing the
                # commit hash into every filename and on-device version
                # string - respin_suffix/_write_scmversion already fall
                # back to just "-lts" (still is_lts_build=True) when
                # this is None, which is short and honest without being
                # unreadable. The commit itself is still logged here for
                # anyone who needs to trace exactly what was built.
                logger.info(
                    f"Kernel pinned to commit SHA {self.config.kernel_tag} "
                    f"(no official respin tag yet) - filenames will use "
                    f"just the sub_level/os_patch/-lts, no respin number"
                )
                return
            m = re.search(r'_(r\d+)$', self.config.kernel_tag)
            if m:
                self.detected_respin = m.group(1)
                logger.info(f"Kernel respin (from pinned tag): {self.detected_respin}")
                return

        head_result = subprocess.run(
            "git rev-parse HEAD", shell=True, cwd=common_dir,
            capture_output=True, text=True
        )
        if head_result.returncode != 0 or not head_result.stdout.strip():
            logger.warning("Could not determine kernel respin - artifact filenames will omit it")
            return
        head_sha = head_result.stdout.strip()

        tag_prefix = f"{self.config.android_version}-{self.config.kernel_version}-{self.config.os_patch_level}_r"
        ls_result = subprocess.run(
            f"git ls-remote --tags https://android.googlesource.com/kernel/common '{tag_prefix}*'",
            shell=True, capture_output=True, text=True, timeout=30
        )
        if ls_result.returncode == 0 and ls_result.stdout:
            # Build tag -> commit SHA, preferring the dereferenced
            # (^{}) line when present (the true commit for annotated
            # tags), falling back to the plain line for lightweight tags.
            tag_shas = {}
            for line in ls_result.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) != 2:
                    continue
                sha, ref = parts
                prefix = "refs/tags/"
                if not ref.startswith(prefix + tag_prefix):
                    continue
                name = ref[len(prefix):]
                is_deref = name.endswith("^{}")
                if is_deref:
                    name = name[:-3]
                if is_deref or name not in tag_shas:
                    tag_shas[name] = sha

            for name, sha in tag_shas.items():
                if sha == head_sha:
                    m = re.match(rf'^{re.escape(tag_prefix)}(\d+)$', name)
                    if m:
                        self.detected_respin = f"r{m.group(1)}"
                        logger.info(f"Kernel respin (matched via remote tag lookup): {self.detected_respin}")
                        return

        logger.warning("Could not determine kernel respin - artifact filenames will omit it")

    @property
    def respin_suffix(self) -> str:
        suffix = f"-{self.detected_respin}" if self.detected_respin else ""
        if self.is_lts_build:
            suffix += "-lts"
        return suffix


    def _apply_legacy_fixes(self, remote_branch: str = ""):
        av, kv = self.config.android_version, self.config.kernel_version
        sub = self.config.get_sub_level_int()
        is_deprecated = "deprecated" in remote_branch

        if is_deprecated and av == "android13" and kv == "5.15" and sub and sub < 123:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android13-5.15-below-123']['url']} -o fix.patch && patch -p1 < fix.patch", check=False)
            self._chdir(self.work_dir)

        if av == "android12" and kv == "5.10" and sub and sub < 136:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android12-5.10-below-136']['url']} | patch -p1", check=False)
            self._chdir(self.work_dir)

    def add_kernel_supatch(self):
        if not self.config.support_op8e:
            return
        logger.info("=== Adding OnePlus 8E support patch ===")
        drivers_dir = self.work_dir / "common/drivers"
        if not drivers_dir.exists():
            return
        self._chdir(drivers_dir)
        self._run_cmd(f"curl -LSs {OP8E_PATCH_URL} -o hmbird_patch.c", check=False)
        if (drivers_dir / "hmbird_patch.c").exists():
            with open(drivers_dir / "Makefile", "a") as f:
                f.write("obj-y += hmbird_patch.o\n")

    def add_kernelsu(self):
        """Pulls in SukiSU-Ultra's kernel-side driver source.

        self.config.kernelsu_commit is a git ref - a tag (e.g. 'v4.1.3'),
        branch, or commit hash all work identically here, since it's
        used directly in a raw.githubusercontent.com URL and a plain
        'git checkout'. Note this pins the driver SOURCE only - it has
        no bearing on the Manager APP's displayed version, which is a
        separate artifact (see the README's "Pinning SukiSU-Ultra"
        section for why 'Manager version (40856-2)' specifically can't
        be pinned this way).

        Fails loudly if the ref doesn't exist, for the same reason
        _checkout_kernel_tag() does: silently falling back to whatever
        setup.sh's default branch happens to be would build a real, but
        DIFFERENT, KernelSU/SukiSU-Ultra version while the build still
        claims to be the one that was asked for.
        """
        logger.info("=== Adding KernelSU ===")
        self._chdir(self.work_dir)
        setup_url = (f"https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/{self.config.kernelsu_commit}/kernel/setup.sh"
                    if self.config.kernelsu_commit else KSU_REPO_CONFIG["setup_script"])
        # setup.sh's argument is <commit-or-tag>, NOT a mode. See
        # DEFAULT_KSU_REF in config.py for the full story on why passing
        # "builtin" here silently built SukiSU-Ultra's divergent `builtin`
        # branch and broke SUSFS patching.
        ksu_ref = self.config.kernelsu_commit or DEFAULT_KSU_REF
        result = self._run_cmd(f"curl -LSsf {setup_url} | bash -s {ksu_ref}", check=False)
        if self.config.kernelsu_commit and result.returncode != 0:
            raise RuntimeError(
                f"kernelsu_commit '{self.config.kernelsu_commit}' could not be "
                f"fetched from raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra "
                f"- it likely doesn't exist (typo, or not a real tag/branch/commit). "
                f"Refusing to silently fall back to the default setup script. "
                f"Check available tags at https://github.com/SukiSU-Ultra/SukiSU-Ultra/tags"
            )
        # Verify unconditionally - not just when --ksu-commit was passed.
        # setup.sh swallows a failed checkout (`git checkout "$1" && echo ...
        # || echo "[-] Checkout default branch"`), so a bad ref leaves the
        # tree on whatever branch the clone happened to land on and the
        # script still exits 0. That is exactly how the `builtin` breakage
        # went unnoticed: the build reported success while tracking a
        # branch nobody asked for.
        ksu_dir = self.work_dir / "KernelSU"
        if not ksu_dir.exists():
            raise RuntimeError(
                f"setup.sh didn't produce a KernelSU/ directory - can't verify "
                f"which SukiSU-Ultra ref we're building. Refusing to continue."
            )
        self._chdir(ksu_dir)
        checkout_result = self._run_cmd(f"git checkout {ksu_ref}", check=False)
        if checkout_result.returncode != 0:
            self._chdir(self.work_dir)
            raise RuntimeError(
                f"SukiSU-Ultra ref '{ksu_ref}' was fetched via setup.sh but "
                f"'git checkout {ksu_ref}' failed inside KernelSU/ - the ref may "
                f"not exist or not be reachable from the clone setup.sh made. "
                f"Refusing to silently continue on whatever ref setup.sh left it "
                f"on. Available tags: "
                f"https://github.com/SukiSU-Ultra/SukiSU-Ultra/tags"
            )
        # capture_output=True is required here: ShellCommand.run() defaults
        # to False, which streams straight to the console and leaves
        # .stdout as None - the command runs and prints fine, but we get
        # nothing back to log or record.
        landed = self._run_cmd("git rev-parse HEAD", check=False, capture_output=True)
        described = self._run_cmd("git describe --tags --always", check=False, capture_output=True)
        self._chdir(self.work_dir)
        logger.info(
            f"SukiSU-Ultra pinned to {ksu_ref} -> "
            f"{(described.stdout or '').strip()} "
            f"({(landed.stdout or '').strip()[:12]})"
        )
        self._mark("sukisu_ref", "applied",
                   f"{ksu_ref} -> {(described.stdout or '').strip()}")
        if not self._fixup_sukisu_kpm_uapi_include(ksu_dir):
            raise RuntimeError(
                "SukiSU-Ultra's kernel/extension/kpm/kpm.c uses the KPM uapi "
                "constants but no header gives them, and extension/kpm/kpm.h is "
                "in a shape _fixup_sukisu_kpm_uapi_include() doesn't know. "
                "A KPM build would not compile. Stopping."
            )

    def _fixup_sukisu_kpm_uapi_include(self, ksu_dir) -> bool:
        # Upstream SukiSU-Ultra bug, not SUSFS related. edf86467 ("unify
        # kernel extensions under a single hub", 2026-10) moved kernel/kpm/
        # to kernel/extension/kpm/ and rewrote kpm.h without the
        # `#include "uapi/supercall.h"` the old kpm/kpm.h had. kpm.c uses
        # SUKISU_KPM_*, CMD_KPM_CONTROL* and struct ksu_kpm_cmd from that
        # header and includes nothing else that pulls it in, so every
        # CONFIG_KPM=y build fails with "use of undeclared identifier
        # 'SUKISU_KPM_LOAD'" (verified on pristine SukiSU decfaa5b, no SUSFS).
        # The rewrite also dropped the CMD_KPM_CONTROL / CMD_KPM_CONTROL_MAX
        # defines (1 / 10) that kpm.c still uses. Put back exactly what the
        # old header gave kpm.c; the defines are #ifndef-guarded so they can
        # never clash if upstream re-adds them somewhere else.
        # No-op on older SukiSU (no extension/kpm/) and once upstream fixes it.
        kpm_c = ksu_dir / "kernel/extension/kpm/kpm.c"
        kpm_h = ksu_dir / "kernel/extension/kpm/kpm.h"
        if not kpm_c.exists() or not kpm_h.exists():
            return True
        marker = '/* KSU_KPM_UAPI_FIXUP: '
        inc = '#include "uapi/supercall.h"\n'
        c_text, h_text = kpm_c.read_text(), kpm_h.read_text()
        # Upstream fixed it in 36cff30c (2026-10-19) by moving the KPM
        # constants to uapi/extension.h and including that from kpm.h.
        # If any uapi header that kpm.h/kpm.c already includes defines
        # SUKISU_KPM_LOAD, there is nothing to restore.
        uapi_dir = ksu_dir / "uapi"
        if not uapi_dir.is_dir():
            uapi_dir = ksu_dir / "kernel/include/uapi"
        for m in re.finditer(r'#include "uapi/([\w.]+)"', h_text + c_text):
            hdr = uapi_dir / m.group(1)
            if hdr.is_file() and 'SUKISU_KPM_LOAD' in hdr.read_text():
                return True
        need_inc = 'SUKISU_KPM_' in c_text and inc not in c_text and inc not in h_text
        need_def = (
            'CMD_KPM_CONTROL' in c_text
            and '#define CMD_KPM_CONTROL ' not in c_text
            and '#define CMD_KPM_CONTROL ' not in h_text
        )
        if marker in h_text or not (need_inc or need_def):
            return True
        anchor = '#include <linux/types.h>\n'
        if h_text.count(anchor) != 1:
            return False
        add = (
            marker + 'restored from the pre-edf86467 kernel/kpm/kpm.h, '
            'see the build pipeline */\n'
        )
        if need_inc:
            add += inc
        if need_def:
            add += (
                '#ifndef CMD_KPM_CONTROL\n#define CMD_KPM_CONTROL 1\n#endif\n'
                '#ifndef CMD_KPM_CONTROL_MAX\n#define CMD_KPM_CONTROL_MAX 10\n#endif\n'
            )
        kpm_h.write_text(h_text.replace(anchor, anchor + add, 1))
        logger.info(
            "kernel/extension/kpm/kpm.h: restored #include \"uapi/supercall.h\" and "
            "the CMD_KPM_CONTROL defines - SukiSU-Ultra's extension hub (edf86467) "
            "dropped them and kpm.c no longer compiles with CONFIG_KPM=y "
            "(see _fixup_sukisu_kpm_uapi_include)."
        )
        self._mark("sukisu_kpm_uapi_include", "applied", "upstream SukiSU KPM header fix")
        return True

    # SukiSU's Kbuild derives the number it reports like this:
    #
    #   GITHUB_COMMITS := $(shell curl -sI ".../commits?sha=main..." | ... rel="last")
    #   LOCAL_COUNT    := $(if $(GITHUB_COMMITS),$(GITHUB_COMMITS),$(git_commit_count))
    #   KSU_VERSION    := 40000 + $(LOCAL_COUNT) - 2815
    #
    # Note what that means: the number does NOT come from the source being
    # compiled. It is main's live commit count at the moment of the build,
    # fetched over the network. The same tag therefore produces a different
    # number every day - v4.2.0 built on 2026-09-01 reported 40901, the same
    # tag built on 2026-09-13 reported 40922.
    #
    # That matters because the released manager for a tag checks for EXACT
    # equality (manager v4.2.0, HomeUiState.kt):
    #
    #   showVersionMismatchWarning = ksuVersion.toLong() != currentManagerVersionCode
    #
    # so a kernel built today from v4.2.0 no longer pairs with the manager
    # released from v4.2.0. Pinning the source was not enough; the number
    # has to be pinned too, or the build is not reproducible in the only
    # sense that matters to the device.
    VERSION_BASE = 40000
    VERSION_OFFSET = 2815

    def _pin_ksu_version_code(self):
        """Makes SukiSU report a fixed version number instead of curling for it."""
        wanted = self.config.ksu_version_code
        if not wanted:
            self._mark("ksu_version_code", "skipped", "not requested (live GitHub count)")
            return

        local_count = wanted - self.VERSION_BASE + self.VERSION_OFFSET
        if local_count <= 0:
            raise RuntimeError(
                f"--ksu-version-code {wanted} is below SukiSU's VERSION_BASE "
                f"({self.VERSION_BASE}); that cannot correspond to any commit count."
            )

        candidates = [
            self.work_dir / "KernelSU/kernel/Kbuild",
            self.work_dir / "KernelSU/kernel/Makefile",
        ]
        target = next((c for c in candidates if c.exists()), None)
        if target is None:
            raise RuntimeError(
                "--ksu-version-code was set but neither KernelSU/kernel/Kbuild "
                "nor KernelSU/kernel/Makefile exists - cannot pin the version."
            )

        text = target.read_text()
        pattern = re.compile(
            r"^(LOCAL_COUNT\s*:=).*$", re.MULTILINE)
        if not pattern.search(text):
            raise RuntimeError(
                f"Could not find the LOCAL_COUNT assignment in {target}. "
                f"SukiSU's version machinery has changed shape - refusing to "
                f"guess, because a wrong edit here produces a kernel that "
                f"silently misreports its own version."
            )
        text = pattern.sub(rf"\1 {local_count}", text, count=1)
        target.write_text(text)

        logger.info(
            f"Pinned SukiSU version code to {wanted} "
            f"(LOCAL_COUNT={local_count}) in {target.name} - the build will no "
            f"longer curl GitHub for main's live commit count."
        )
        self._mark("ksu_version_code", "applied",
                   f"{wanted} (LOCAL_COUNT={local_count})")

    # Upstream Linux changed ptrace_notify()'s signature in 5.16 to pass
    # the event message as an explicit argument instead of stashing it in
    # current->ptrace_message before the tracer is actually notified -
    # closing a race where that field (e.g. a forked child's PID) is
    # briefly visible to other readers before/after the real notify.
    # Kernels below 5.16 (5.10, 5.15) don't have this fix upstream, so we
    # backport it here. This is a no-op skip - not a failure - on kernels
    # that already have it natively.
    def _fix_bbg_genheaders_path(self, common_dir: Path):
        """Deliberately does nothing. Kept so the reason is not re-discovered.

        security/baseband-guard/Makefile tries to pre-generate
        $(objtree)/security/selinux/flask.h and av_permissions.h by
        building scripts/selinux/genheaders with $(HOSTCC) at parse
        time. Under build/build.sh the PATH is narrowed, the host clang
        cannot find `ld`, and the whole thing fails noisily:

            clang-14: error: unable to execute command:
                      Executable "ld" doesn't exist!
            make[3]: ./genheaders: No such file or directory
            mv: bad './flask.h': No such file or directory

        That noise is HARMLESS and must stay. security/selinux/Makefile
        generates both headers itself, as a normal build rule, well
        before any BBG object compiles - so the kernel's own authoritative
        copies are what everything is built against.

        This method used to "fix" the failure by wrapping the HOSTCC call
        in a PATH-extended env (mirroring what BBG already does for git).
        It worked - and that was the problem. BBG's copies then won the
        race and landed in objtree first. flask.h and av_permissions.h
        define the numeric SELinux class and permission IDs; a copy
        produced by a different invocation is not guaranteed to match
        what the rest of the kernel is compiled against.

        Measured, on identical kernel source (013ca9e4), identical
        SukiSU-Ultra (v4.2.0) and an identical SUSFS pin: the build
        WITHOUT this fix booted on a real device; the build WITH it did
        not. It was the only change between them that alters compiled
        output - the other two deltas in that build were a no-op on 5.15
        (the selinux_hide guards sit inside
        `#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)`) and a
        read-only verification step.

        Do not re-enable this. If the log noise ever needs to go, the
        safe direction is to stop BBG from generating at all (so the
        kernel's rule remains the only producer), never to make BBG's
        generation succeed.
        """
        self._mark("bbg_genheaders_path", "skipped",
                   "intentionally not patched - kernel's own flask.h must win")

    def apply_ptrace_leak_fix(self):
        if self.config.kernel_version not in ("5.10", "5.15"):
            logger.info("Skipping ptrace leak fix - already upstream on this kernel version")
            self._mark("ptrace_leak_fix", "skipped", "already upstream on this kernel version")
            return
        logger.info("=== Applying ptrace leak fix (kernels < 5.16) ===")
        patch_file = Path(__file__).parent / "patches" / "gki_ptrace.patch"
        ok, detail = self._apply_patch_with_dry_run(patch_file, self.work_dir / "common")
        if not ok:
            logger.warning(
                f"ptrace leak fix not applied ({detail}) - kernel source may "
                f"have diverged from what this patch expects. The tree was "
                f"left untouched; continuing without it."
            )
            self._mark("ptrace_leak_fix", "failed", detail)
        else:
            self._mark("ptrace_leak_fix", "applied", detail)

    @staticmethod
    def _version_tuple(v: str):
        return tuple(int(x) for x in v.split("."))

    def _has_fuse_bpf(self) -> bool:
        """Whether this branch actually has FUSE-BPF (fs/fuse/backing.c).
        See FUSE_BPF_UNSUPPORTED for why this is an explicit set."""
        return (self.config.android_version, self.config.kernel_version) not in self.FUSE_BPF_UNSUPPORTED

    def _apply_patch_with_dry_run(self, patch_file: Path, target_dir: Path,
                                  fuzz_levels=(0, 3), extra_args: str = "") -> tuple:
        """Applies a patch only after a --dry-run at the same fuzz level
        confirms every hunk lands.

        This matters more than it looks: `patch` is not atomic. Given a
        patch where some hunks match and some don't, it applies the ones
        that do, writes .rej files for the rest, and exits non-zero. A
        caller that only checks the exit code and then logs "did not
        apply cleanly - continuing without it" is therefore describing
        the wrong thing: the tree is not untouched, it's half-patched.
        For a soft-fail feature that's the difference between "this
        hardening tweak is missing" and "this source file is now in a
        state neither upstream nor the patch author ever tested".

        Fuzz levels are tried in order and the FIRST one that dry-runs
        clean is the one actually applied - never apply-then-retry.
        fuzz=0 (exact context) is preferred so a clean apply is reported
        as exactly that; fuzz=3 is the tolerant fallback for branches
        whose surrounding context has drifted. Callers patching
        kABI-tracked struct layouts must pass fuzz_levels=(0,) - a fuzzy
        match there can silently land on the wrong offsets (see
        apply_droidspaces_support()).

        Returns (ok: bool, detail: str) - detail is suitable for passing
        straight to _mark().
        """
        if not patch_file.exists():
            return False, f"{patch_file.name} missing"
        prev_cwd = self.shell.cwd
        self._chdir(target_dir)
        try:
            for fuzz in fuzz_levels:
                cmd_base = f"patch -p1 -F {fuzz} {extra_args}".rstrip()
                dry = self._run_cmd(f"{cmd_base} --dry-run < {patch_file}",
                                    check=False, capture_output=True)
                if dry.returncode != 0:
                    continue
                applied = self._run_cmd(f"{cmd_base} < {patch_file}", check=False)
                if applied.returncode != 0:
                    # Dry-run passed but the real apply didn't - should be
                    # impossible, so say so loudly rather than treating it
                    # as an ordinary "didn't match".
                    return False, (f"{patch_file.name} passed --dry-run at fuzz={fuzz} "
                                   f"but failed on the real apply")
                return True, (patch_file.name if fuzz == 0
                              else f"{patch_file.name} (fuzz={fuzz})")
            tried = "/".join(str(f) for f in fuzz_levels)
            return False, f"{patch_file.name} did not apply cleanly (tried fuzz {tried}) - tree left untouched"
        finally:
            self._chdir(Path(prev_cwd))

    def _revert_patch(self, patch_file: Path, target_dir: Path, fuzz: int = 3):
        """Reverses a patch this pipeline applied moments earlier. Used
        when a multi-patch feature applies its first patch successfully
        and then fails on a later one - without this the tree keeps a
        half-installed feature (e.g. an ntsync driver source file with
        no Kconfig/Makefile wiring) that nothing later accounts for."""
        prev_cwd = self.shell.cwd
        self._chdir(target_dir)
        try:
            self._run_cmd(f"patch -p1 -R -F {fuzz} < {patch_file}", check=False)
        finally:
            self._chdir(Path(prev_cwd))

    def apply_unicode_fix(self):
        """Backports a fix to the kernel's generic Unicode/UTF-8
        normalization subsystem (fs/unicode/utf8-norm.c - used by
        case-insensitive f2fs/ext4 folders, CONFIG_UNICODE). Despite
        living in WildKernels' kernel_patches repo alongside their
        KernelSU-Next-specific patches, this one isn't KSU/SUSFS-
        specific at all - it's a plain fix to stock upstream kernel
        code, so it applies the same regardless of which KSU fork a
        pipeline uses.

        The upstream bug: utf8byte()'s decompose step advanced the
        cursor pointer/length *before* checking whether the
        decomposition it just looked up was empty (e.g. a zero-width
        character decomposes to nothing) - by the time it checked for
        an empty result, it was already inspecting the wrong position.
        A crafted filename using a zero-width or similarly-decomposing
        codepoint could exploit that ordering bug to make case-
        insensitive path comparisons behave inconsistently - including
        path checks used for root-hiding - hence "bypass".

        Two variants exist because utf8lookup()'s signature changed
        upstream at Linux 5.16 (u8c->data -> u8c->um/u8c->n). The
        threshold below matches WildKernels' own action.yml exactly -
        it version-sorts kernel_version against "5.16", not the "6.1"
        in the patch filenames (those names are just imprecise).

        Soft-fail like BBRv3/ptrace_leak_fix: nothing else in this
        pipeline's SUSFS/KernelSU integration depends on this landing,
        so a failure here shouldn't abort the whole build - just means
        this one hardening tweak is missing, visible either way in
        PATCH_STATUS.json.
        """
        logger.info("=== Applying Unicode normalization bypass fix ===")
        below_5_16 = self._version_tuple(self.config.kernel_version) < self._version_tuple("5.16")
        patch_name = "unicode_bypass_fix_below-5.16.patch" if below_5_16 else "unicode_bypass_fix_5.16-plus.patch"
        patch_file = Path(__file__).parent / "patches" / patch_name
        ok, detail = self._apply_patch_with_dry_run(patch_file, self.work_dir / "common")
        if not ok:
            logger.warning(
                f"Unicode bypass fix not applied ({detail}) - "
                f"fs/unicode/utf8-norm.c may have diverged from what "
                f"this patch expects. The tree was left untouched; "
                f"continuing without it."
            )
            self._mark("unicode_bypass_fix", "failed", detail)
        else:
            self._mark("unicode_bypass_fix", "applied", detail)

    # NTSync (drivers/misc/ntsync.c) emulates Windows NT synchronization
    # primitives in-kernel - useful for Winlator/Wine. It's mainline as of
    # Linux 6.14, so this backports it via two patches from kernel_patches:
    # a base driver patch (same for every branch) plus a small per-branch
    # compat patch that wires it into that branch's Kconfig/Makefile.
    # Skips (doesn't fail the build) if no compat patch exists yet for
    # this specific android/kernel combo.
    def apply_ntsync_patches(self):
        logger.info("=== Applying NTSync driver patches ===")
        patches_dir = Path(__file__).parent / "patches"
        base_patch = patches_dir / "ntsync_base.patch"
        compat_patch = patches_dir / f"ntsync_compat_{self.config.android_version}-{self.config.kernel_version}.patch"
        if not base_patch.exists() or not compat_patch.exists():
            logger.warning(
                f"NTSync patches not found for "
                f"{self.config.android_version}-{self.config.kernel_version} "
                f"(looked for {compat_patch.name}) - skipping"
            )
            self._mark("ntsync", "skipped", "no compat patch for this branch")
            return
        common_dir = self.work_dir / "common"
        applied_so_far = []
        details = []
        for patch_file, label in [(base_patch, "driver source"), (compat_patch, "Kconfig/Makefile wiring")]:
            ok, detail = self._apply_patch_with_dry_run(patch_file, common_dir)
            if not ok:
                # Roll back anything already applied for this feature.
                # Without this, a failure on the compat patch leaves
                # drivers/misc/ntsync.c sitting in the tree with nothing
                # in Kconfig/Makefile referencing it - a half-installed
                # feature that later steps have no way to notice.
                for done in reversed(applied_so_far):
                    self._revert_patch(done, common_dir)
                logger.warning(
                    f"NTSync {label} patch not applied ({detail}) - "
                    f"reverted the NTSync patches already applied and "
                    f"continuing without NTSync"
                )
                self._mark("ntsync", "failed", f"{label}: {detail}")
                return
            applied_so_far.append(patch_file)
            details.append(detail)
        self._mark("ntsync", "applied", "; ".join(details))

    def add_bbg(self):
        if not self.config.use_bbg:
            self._mark("baseband_guard", "skipped", "not requested")
            return
        logger.info("=== Adding Baseband-guard ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            self._mark("baseband_guard", "failed", "common/ missing")
            return
        self._chdir(common_dir)
        self._run_cmd(f"wget -O- {BBG_CONFIG['setup_script']} | bash", check=False)
        self._fix_bbg_genheaders_path(common_dir)
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if config_file.exists():
            with open(config_file, "a") as f:
                f.write("CONFIG_BBG=y\n")
        kconfig_file = common_dir / "security/Kconfig"
        if kconfig_file.exists():
            with open(kconfig_file, "r") as f:
                content = f.read()
            # Mirrors the project's own officially-documented CI one-liner
            # (github.com/vc-teahouse/Baseband-guard README, "如果你正在使用
            # Github Action云编译" section):
            #   sed -i '/^config LSM$/,/^help$/{ /^[[:space:]]*default/ {
            #     /baseband_guard/! s/selinux/selinux,baseband_guard/ } }'
            #     security/Kconfig
            # Scoped strictly between the `config LSM` and `help` lines so it
            # can't accidentally touch an unrelated config block that happens
            # to contain the word "selinux" elsewhere in the file.
            lines = content.split('\n')
            in_lsm_block = False
            for i, line in enumerate(lines):
                if line.strip() == 'config LSM':
                    in_lsm_block = True
                elif in_lsm_block and line.strip() == 'help':
                    in_lsm_block = False
                elif in_lsm_block and re.match(r'^\s*default', line):
                    if 'selinux' in line and 'baseband_guard' not in line:
                        lines[i] = line.replace('selinux', 'selinux,baseband_guard', 1)
            content = '\n'.join(lines)
            with open(kconfig_file, "w") as f:
                f.write(content)
        self._mark("baseband_guard", "applied")

    # NOTE: a GENKSYMS-bypass regex fallback used to live here
    # (_try_genksyms_regex_fallback). Removed after a confirmed real-device
    # bootloop: '#ifndef __GENKSYMS__' only hides new fields from genksyms'
    # CRC symbol-versioning tool - it does NOT reserve/account for their
    # bytes in the real compiled struct layout, unlike ANDROID_KABI_RESERVE
    # slots (see the 3 slot patches below, which correctly reuse pre-
    # reserved padding and are genuinely kABI-safe). Every field declared
    # after the injected ones in struct task_struct silently shifts to a
    # different real offset, while our kmi_symbol_list_strict_mode /
    # kmi_symbol_list_violations checks only validate the *exported
    # symbol* list - they don't diff internal struct layouts, so this
    # class of break is invisible at build time and only surfaces as
    # vendor .ko modules (built against stock offsets) reading/writing
    # the wrong memory on a real device. Don't reintroduce a
    # non-reserved-slot struct modification for a kABI-tracked type,
    # regardless of how "clean" the build-time checks look.

    def apply_droidspaces_support(self):
        """Enables Droidspaces (github.com/ravindu644/Droidspaces-OSS)
        container-runtime support: real Linux namespace isolation
        (PID/IPC/Mount) so a full Linux distro can run its own init
        system (systemd/OpenRC) as an actual isolated container,
        instead of a plain chroot that just shares the host's process
        tree.

        GKI enforces a strict kABI checksum on struct layouts. Just
        flipping on CONFIG_SYSVIPC/CONFIG_IPC_NS/CONFIG_POSIX_MQUEUE in
        defconfig shifts struct task_struct's layout and causes an
        IMMEDIATE BOOTLOOP - these patches instead move the relevant
        fields into Android's already-reserved kABI padding slots
        (ANDROID_KABI_RESERVE(N) -> ANDROID_KABI_USE(N, ...)) so the
        checksum doesn't move.

        Upstream ships 4 variants of the SYSVIPC patch: 3 claiming a
        different trio of ANDROID_KABI_RESERVE slots (6/7/8, 3/4/5, or
        1/2/3 - which slots are free isn't the same across every
        respin), plus a 4th "bypass" variant that sidesteps reserve
        slots entirely by hiding the new fields behind
        '#ifndef __GENKSYMS__'. ONLY THE 3 RESERVE-SLOT VARIANTS ARE
        USED HERE. The bypass variant is deliberately not tried - it
        hides the fields from the checksum tool without reserving their
        bytes in the real struct, which caused a confirmed real-device
        bootloop (see the note above the sysvipc_variants list). Rather
        than hardcoding a guess, this tries the 3 in order (dry-run
        first, strict context matching - fuzzy matching here risks a
        false-positive "clean" apply against the WRONG slots, which
        would silently break kABI in a different way) and applies the
        first one that actually fits this exact source tree.

        Only wired up for kernel_version < 6.12 (android12-5.10,
        android13-5.15, android14-6.1, android15-6.6 - the branches
        this project currently builds; all four use the same
        ANDROID_KABI_RESERVE slot-swap approach, verified identical to
        WildKernels' upstream droidspaces/fix_sysvipc_kabi_*.patch
        files). 6.12+ needs a structurally different patch (a
        __kabi_ignored union + __attribute__((packed)) trick instead of
        reserve slots) that isn't vendored here yet.
        """
        if not self.config.use_droidspaces:
            self._mark("droidspaces", "skipped", "not requested")
            return
        if self.config.kernel_version not in ("5.10", "5.15", "6.1", "6.6"):
            logger.warning(
                f"Droidspaces support requested but not implemented yet for "
                f"kernel {self.config.kernel_version} (only 5.10/5.15/6.1/6.6 "
                f"are wired up) - skipping"
            )
            self._mark("droidspaces", "skipped", f"kernel {self.config.kernel_version} not supported yet")
            return

        logger.info("=== Applying Droidspaces container-runtime support ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            self._mark("droidspaces", "failed", "common/ missing")
            return

        patch_dir = Path(__file__).parent / "patches"
        # Only the 3 ANDROID_KABI_RESERVE reserve-slot variants - these
        # reuse padding bytes Google pre-allocated specifically so
        # vendors can repurpose them without shifting any other field's
        # offset, so they're genuinely kABI-safe. A GENKSYMS-bypass
        # variant (adds real new fields hidden from genksyms' CRC tool
        # instead of using reserved padding) used to be tried as a 4th
        # fallback here - removed after it caused a confirmed real-device
        # bootloop (see the note above _try_genksyms_regex_fallback's old
        # location for the full mechanism). Don't re-add a non-reserved-
        # slot struct modification here.
        sysvipc_variants = [
            # Branch-specific exact-context variants come first. These exist
            # because the generic patches' TRAILING context assumes the
            # "New fields for task_struct should be added above here"
            # comment sits immediately after ANDROID_KABI_RESERVE(8). On
            # android15-6.6 a CONFIG_RV block sits there instead, so the
            # generic slots678 hunk only matched at fuzz 2 - and fuzz is
            # exactly what we refuse to use here (see below). The
            # regenerated variant performs the identical transformation
            # (slots 6/7/8, slot 1 left alone) and was verified to produce
            # a byte-identical result to the fuzz-2 apply, but matches at
            # -F 0. Non-matching branches skip it harmlessly.
            f"droidspaces_sysvipc_kabi_slots678_{self.config.android_version}-{self.config.kernel_version}.patch",
            "droidspaces_sysvipc_kabi_slots678.patch",  # upstream's default choice for every below-6.12 branch
            "droidspaces_sysvipc_kabi_slots345.patch",
            "droidspaces_sysvipc_kabi_slots123.patch",
        ]
        applied_variant = None
        self._chdir(common_dir)
        for variant in sysvipc_variants:
            patch_file = patch_dir / variant
            if not patch_file.exists():
                continue
            # -F 0 always, no exceptions. A fuzzy "clean" apply on a
            # struct-layout patch can insert the new field block at a
            # structurally wrong place (not just a wrong reserve slot)
            # even when patch(1) reports success - confirmed the hard
            # way once already on the (now-removed) bypass variant. A
            # clean failure here (droidspaces: failed, config options
            # never added) is the correct/safe outcome when nothing
            # matches - never trade that for a fuzzy "success".
            dry_run = self._run_cmd(f"patch -p1 -F 0 --dry-run < {patch_file}", check=False)
            if dry_run.returncode == 0:
                self._run_cmd(f"patch -p1 -F 0 < {patch_file}", check=False)
                applied_variant = variant
                break
        self._chdir(self.work_dir)

        if not applied_variant:
            logger.warning(
                "Droidspaces SYSVIPC kABI patch did not cleanly match any "
                "of the 3 known ANDROID_KABI_RESERVE slot layouts on this "
                "branch - task_struct may have diverged further upstream. "
                "may have diverged further upstream. Continuing WITHOUT "
                "Droidspaces support (defconfig options are not added "
                "either, to avoid a bootloop from enabling CONFIG_SYSVIPC "
                "without the matching kABI fix)."
            )
            self._mark("droidspaces", "failed", "no sysvipc kabi variant applied cleanly")
            return
        logger.info(f"Droidspaces SYSVIPC kABI patch applied ({applied_variant})")

        # 5.10 needs one more patch for POSIX_MQUEUE specifically (a
        # different struct - user_struct, not task_struct - so no slot
        # conflict with the patch above).
        mqueue_detail = ""
        if self.config.kernel_version == "5.10":
            self._chdir(common_dir)
            mqueue_patch = patch_dir / "droidspaces_posix_mqueue_5_10.patch"
            result = self._run_cmd(f"patch -p1 -F 0 < {mqueue_patch}", check=False)
            self._chdir(self.work_dir)
            if result.returncode != 0:
                logger.warning(
                    "Droidspaces POSIX_MQUEUE kABI patch (5.10-specific) "
                    "did not apply cleanly - POSIX_MQUEUE may still break "
                    "kABI on this branch. Continuing anyway since the main "
                    "SYSVIPC patch succeeded; POSIX_MQUEUE just won't be "
                    "safely enabled."
                )
                mqueue_detail = "; posix_mqueue patch failed"

        # Required + recommended defconfig options - see
        # https://github.com/ravindu644/Droidspaces-OSS/blob/main/Documentation/Kernel-Configuration.md
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning(f"gki_defconfig not found at {config_file} - Droidspaces configs not added")
            self._mark("droidspaces", "failed", f"sysvipc variant {applied_variant} applied, but gki_defconfig missing")
            return

        droidspaces_configs = [
            "# Droidspaces container runtime support",
            "CONFIG_SYSVIPC=y",
            "CONFIG_POSIX_MQUEUE=y",
            "CONFIG_IPC_NS=y",
            "CONFIG_PID_NS=y",
            "CONFIG_DEVTMPFS=y",
            "CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=y",
            "CONFIG_USER_NS=y",
            "CONFIG_NETFILTER_XT_TARGET_REJECT=y",
            "CONFIG_NETFILTER_XT_TARGET_LOG=y",
            "CONFIG_NETFILTER_XT_MATCH_RECENT=y",
            "CONFIG_IP_SET=y",
            "CONFIG_IP_SET_HASH_IP=y",
            "CONFIG_IP_SET_HASH_NET=y",
            "CONFIG_NETFILTER_XT_SET=y",
            "CONFIG_TMPFS_POSIX_ACL=y",
            "CONFIG_TMPFS_XATTR=y",
            "CONFIG_BINFMT_MISC=y",
            "CONFIG_BINFMT_SCRIPT=y",
            "CONFIG_BINFMT_ELF=y",
            # --net=macvlan. kABI-safe: a self-contained driver, struct
            # net_device only uses the IFF_MACVLAN* priv_flags, which exist
            # unconditionally (GKI already ships CONFIG_IPVLAN=y the same way).
            "CONFIG_MACVLAN=y",
            # Deliberately NOT enabled (Droidspaces lists them as optional):
            # - CONFIG_CGROUP_PIDS (--pids-limit): adds a cgroup subsystem, so
            #   CGROUP_SUBSYS_COUNT grows and the subsys[] / e_csets[] arrays
            #   in struct css_set and struct cgroup (both in the KMI) change
            #   size, shifting every field after them for vendor modules.
            #   Opt-in workaround: --droidspaces-pids (apply_droidspaces_pids_slot).
            # - CONFIG_CFS_BANDWIDTH (--cpus): adds fields to struct cfs_rq,
            #   which is embedded in struct rq, so the layout of struct rq
            #   changes - vendor scheduler modules (WALT etc.) read it directly.
        ]
        with open(config_file, "a") as f:
            f.write("\n" + "\n".join(droidspaces_configs) + "\n")
        self._mark("droidspaces", "applied", f"sysvipc variant: {applied_variant}{mqueue_detail}")

    # Stock android13-5.15 GKI cgroup subsystems, in id order. The slot
    # swap below is only safe while this is exactly what the defconfig
    # enables - checked against the real tree on every build.
    _PIDS_SLOT_DONOR = "net_prio"

    @staticmethod
    def _cgroup_subsys_ids(header_text: str, enabled) -> list:
        """Subsystem names in id order, as enum cgroup_subsys_id lays them
        out: every 'SUBSYS(x)' in cgroup_subsys.h whose guarding
        '#if IS_ENABLED(CONFIG_X)' is on. enabled(sym) -> bool."""
        import re as _re
        out = []
        for sym, name in _re.findall(
                r'^#if IS_ENABLED\((CONFIG_\w+)\)\s*\nSUBSYS\((\w+)\)',
                header_text, flags=_re.M):
            if enabled(sym):
                out.append(name)
        return out

    @staticmethod
    def _defconfig_enabled(lines: list):
        """enabled(sym) for a defconfig: the LAST assignment wins, like
        Kconfig. Absent or '# ... is not set' -> False."""
        state = {}
        for line in lines:
            line = line.strip()
            if line.startswith("CONFIG_") and "=" in line:
                k, _, v = line.partition("=")
                state[k] = v.strip() in ("y", "m")
            elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
                state[line.split()[1]] = False
        return lambda sym: state.get(sym, False)

    def apply_droidspaces_pids_slot(self):
        """Opt-in (--droidspaces-pids): CONFIG_CGROUP_PIDS for Droidspaces'
        --pids-limit without breaking the GKI kABI.

        A plain CONFIG_CGROUP_PIDS=y adds an 8th cgroup subsystem, which
        grows the subsys[]/e_csets[] arrays in struct css_set and struct
        cgroup and shifts every later field for vendor modules. Instead
        pids takes net_prio's slot: stock 5.15 GKI enables cpuset, cpu,
        cpuacct, io, memory, freezer, net_prio (ids 0-6) and pids sorts
        right after net_prio, so CONFIG_CGROUP_NET_PRIO=n +
        CONFIG_CGROUP_PIDS=y keeps the count at 7 and every other id
        unchanged. net_prio is a cgroup v1 controller Android doesn't
        mount, and no vendor KMI symbol list uses its symbols (xiaomi's
        list DOES use freezer_cgrp_subsys - never pick freezer as donor).

        The patch keeps everything net_prio adds to KMI structs:
        net_device::priomap, sock_cgroup_data::prioidx and the full
        struct netprio_map type. While genksyms reads enum
        cgroup_subsys_id, id 6 keeps its stock name net_prio_cgrp_id, so
        CRCs stay stock. This is NOT the rejected GENKSYMS bypass: no
        field is added or hidden, only an enum name with the same value.
        Verified on android13-5.15 (5.15.194 tree): struct sizes/offsets
        of css_set, cgroup, cgroup_root, task_struct, sock, net_device
        identical, and all 11596 genksyms CRCs of kernel/ mm/ net/ fs/
        block/ lib/ security/ ... identical to stock except
        net_prio_cgrp_subsys_{enabled,on_dfl}_key replaced by the pids
        ones.

        Fail-safe: anything unexpected (other branch, different
        subsystem set, patch not matching at -F 0, NET_PRIO line not
        found) -> skipped/failed and nothing is changed.
        """
        if not getattr(self.config, "use_droidspaces_pids", False):
            self._mark("droidspaces_pids", "skipped", "not requested")
            return
        if self.patch_status.get("droidspaces", {}).get("status") != "applied":
            self._mark("droidspaces_pids", "skipped", "needs Droidspaces applied first")
            return
        if (self.config.android_version, self.config.kernel_version) != ("android13", "5.15"):
            self._mark("droidspaces_pids", "skipped",
                       f"only verified for android13-5.15, not "
                       f"{self.config.android_version}-{self.config.kernel_version}")
            return

        logger.info("=== Droidspaces pids: net_prio -> pids cgroup slot swap ===")
        common_dir = self.work_dir / "common"
        header = common_dir / "include/linux/cgroup_subsys.h"
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        patch_file = (Path(__file__).parent / "patches" /
                      f"droidspaces_cgroup_pids_netprio_slot_"
                      f"{self.config.android_version}-{self.config.kernel_version}.patch")
        for f in (header, config_file, patch_file):
            if not f.exists():
                self._mark("droidspaces_pids", "failed", f"{f.name} missing")
                return

        lines = config_file.read_text().splitlines()
        donor_sym = "CONFIG_CGROUP_NET_PRIO"
        if sum(1 for l in lines if l.strip() == f"{donor_sym}=y") != 1:
            self._mark("droidspaces_pids", "failed",
                       f"expected exactly one '{donor_sym}=y' line in gki_defconfig")
            return

        htext = header.read_text()
        stock = self._cgroup_subsys_ids(htext, self._defconfig_enabled(lines))
        swapped_lines = ([l for l in lines if l.strip() != f"{donor_sym}=y"]
                         + [f"# {donor_sym} is not set", "CONFIG_CGROUP_PIDS=y"])
        swapped = self._cgroup_subsys_ids(htext, self._defconfig_enabled(swapped_lines))
        logger.info(f"cgroup subsystems, stock:   {stock}")
        logger.info(f"cgroup subsystems, swapped: {swapped}")
        donor = self._PIDS_SLOT_DONOR
        ok = (donor in stock and "pids" not in stock
              and len(stock) == len(swapped)
              and swapped[stock.index(donor)] == "pids"
              and all(a == b for i, (a, b) in enumerate(zip(stock, swapped))
                      if i != stock.index(donor)))
        if not ok:
            self._mark("droidspaces_pids", "failed",
                       f"cgroup subsystem layout is not the verified one "
                       f"(stock {stock}, swapped {swapped}) - nothing changed")
            return

        self._chdir(common_dir)
        dry = self._run_cmd(f"patch -p1 -F 0 --dry-run < {patch_file}", check=False)
        if dry.returncode != 0:
            self._chdir(self.work_dir)
            self._mark("droidspaces_pids", "failed",
                       "slot-swap patch does not match this tree at -F 0 - nothing changed")
            return
        res = self._run_cmd(f"patch -p1 -F 0 --no-backup-if-mismatch < {patch_file}", check=False)
        self._chdir(self.work_dir)
        if res.returncode != 0:
            raise RuntimeError("droidspaces pids slot-swap patch failed after a clean dry-run")

        out = []
        for l in lines:
            if l.strip() == f"{donor_sym}=y":
                out.append("# Droidspaces pids slot swap: net_prio gives its cgroup id to pids")
                out.append(f"# {donor_sym} is not set")
            else:
                out.append(l)
        out += ["", "# Droidspaces --pids-limit (takes net_prio's cgroup id, kABI-neutral)",
                "CONFIG_CGROUP_PIDS=y"]
        config_file.write_text("\n".join(out) + "\n")
        self._mark("droidspaces_pids", "applied",
                   f"pids takes cgroup id {stock.index(donor)} from {donor}")

    def apply_bbrv3_patches(self):
        """Backports BBRv3 (github.com/WildKernels/kernel_patches, common/bbrv3)
        - Google's newer TCP congestion control algorithm - to GKI
        branches that don't ship it upstream, with the necessary
        Android kABI compliance adjustments already folded in by the
        WildKernels backport itself (no reserve-slot juggling needed
        here, unlike Droidspaces - BBRv3 doesn't touch struct layouts
        that are kABI-tracked).

        Only wired up for android12-5.10, android13-5.15, android14-6.1,
        android15-6.6 so far (the branches this project builds).
        WildKernels also publish an android16-6.12 variant, not vendored
        here yet since nothing currently built uses that branch.

        Two small prerequisite patches (proc_dou8vec_minmax() and its
        follow-up data-race fix) are handled first by
        _apply_bbrv3_prereqs(): each one is either detected as already
        present, applied as a whole, or left alone - never half-applied.
        Every branch built here already has both via -stable, so in
        practice they are detected as present and nothing is touched.

        The main patch is tried at fuzz=0 first, then fuzz=3 as a
        fallback (dry-run for both, only ever actually applying the
        first one that comes back clean - never apply-then-retry, see
        the comment inline). Fuzz is safe here (unlike Droidspaces'
        kABI patch, which deliberately never uses fuzz): this only
        touches ordinary net/ipv4/*.c function bodies, not kABI-tracked
        struct layouts, so a fuzzy match still applies the exact same
        code change - it's just more tolerant of a few lines of
        surrounding context having drifted on this particular branch.

        Sets self._bbrv3_applied so configure_kernel() knows whether to
        write CONFIG_DEFAULT_BBR3=y - never blindly set that from
        self.config.bbr_version alone, since the Kconfig symbol
        wouldn't exist if this patch didn't actually land.
        """
        self._bbrv3_applied = False
        if self.config.bbr_version != "bbr3":
            self._mark("bbrv3", "skipped", "not requested")
            return
        fb = f"{self.config.android_version}-{self.config.kernel_version}"
        if fb not in ("android12-5.10", "android13-5.15", "android14-6.1", "android15-6.6"):
            logger.warning(f"BBRv3 requested but not implemented yet for {fb} - skipping")
            self._mark("bbrv3", "skipped", f"{fb} not supported yet")
            return

        logger.info("=== Applying BBRv3 TCP congestion control backport ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            self._mark("bbrv3", "failed", "common/ missing")
            return

        patch_dir = Path(__file__).parent / "patches"
        self._chdir(common_dir)

        # Prerequisites - all-or-nothing, see _apply_bbrv3_prereqs().
        prereq_note = self._apply_bbrv3_prereqs(patch_dir, common_dir)
        if prereq_note is None:
            self._chdir(self.work_dir)
            self._mark("bbrv3", "failed",
                       "proc_dou8vec_minmax() missing and its prereq patch does not apply")
            return

        main_patch = patch_dir / f"bbrv3_{fb}.patch"
        if not main_patch.exists():
            self._chdir(self.work_dir)
            self._mark("bbrv3", "failed", "main patch file missing")
            return

        # Dry-run first at strict (no fuzz), then - only if that fails -
        # at a small amount of fuzz, and only actually apply whichever
        # level first comes back clean. Never apply-then-retry, since
        # reverting a partially-applied patch mid-build would risk
        # undoing the other, already-applied patches sitting in the
        # same uncommitted working tree (ptrace/ntsync/SUSFS/SukiSU/
        # Droidspaces etc. all landed earlier in this same build with
        # no git commit in between).
        #
        # Fuzz is safe to allow here (unlike Droidspaces' kABI patch,
        # which deliberately stays strict): this patch only touches
        # ordinary net/ipv4/*.c function bodies and call sites, not
        # kABI-tracked struct layouts - fuzz only relaxes how much
        # surrounding context is allowed to differ, never what the
        # actual change itself is, so a fuzzy match here still applies
        # the exact same code change, just more tolerant of this
        # branch's source having drifted a line or two around it.
        applied_fuzz = None
        for fuzz in (0, 3):
            dry_run = self._run_cmd(f"patch -p1 -F {fuzz} --dry-run < {main_patch}", check=False)
            if dry_run.returncode == 0:
                applied_fuzz = fuzz
                break
        if applied_fuzz is None:
            self._chdir(self.work_dir)
            logger.warning(
                "BBRv3 patch did not apply cleanly even with fuzz - this "
                "branch's net/ipv4 source may have diverged significantly "
                "from what the WildKernels backport expects. Continuing "
                "WITHOUT BBRv3 (falling back to whatever --bbr-version "
                "would otherwise select)."
            )
            self._mark("bbrv3", "failed", "main patch did not apply cleanly (tried fuzz 0 and 3)")
            return

        result = self._run_cmd(f"patch -p1 -F {applied_fuzz} < {main_patch}", check=False)
        self._chdir(self.work_dir)
        if result.returncode != 0:
            # Dry-run and real apply disagreeing is unusual but not
            # impossible (e.g. a hunk whose match depends on state left
            # by a PRECEDING hunk in the same file) - treat it the same
            # as any other failure: fail safe, don't half-apply.
            logger.warning(
                "BBRv3 patch passed --dry-run but failed on the real "
                "apply - continuing WITHOUT BBRv3 (falling back to "
                "whatever --bbr-version would otherwise select)."
            )
            self._mark("bbrv3", "failed", "dry-run succeeded but real apply failed")
            return

        self._bbrv3_applied = True
        notes = [] if applied_fuzz == 0 else [f"applied with fuzz={applied_fuzz}"]
        if prereq_note:
            notes.append(prereq_note)
        self._mark("bbrv3", "applied", "; ".join(notes))

    # proc_dou8vec_minmax() is the one thing BBRv3 needs from the prereqs;
    # its EXPORT line is how we tell the function is really there.
    _DOU8VEC_EXPORT = "EXPORT_SYMBOL_GPL(proc_dou8vec_minmax);"

    def _apply_bbrv3_prereqs(self, patch_dir, common_dir):
        """Applies the two BBRv3 prerequisite patches all-or-nothing.

        The old code ran `patch -N -F 0` directly. -N only skips hunks it
        recognises as already applied one by one, so on trees that
        already have proc_dou8vec_minmax() (every branch built here, via
        -stable) some hunks still "succeeded" against a different spot:
        a second copy of the sysctl_check_table_array() check in
        fs/proc/proc_sysctl.c and a second -ENOSYS stub in kernel/sysctl.c
        (inside the !CONFIG_PROC_SYSCTL branch, so GKI still compiled),
        plus .rej/.orig files left in the tree.

        Now, per patch:
          - already present (reverse dry-run is clean, or for the main
            prereq the exported function is already in kernel/sysctl.c)
            -> nothing is touched;
          - otherwise, the whole patch dry-runs clean -> apply it;
          - otherwise -> leave the tree alone and say so.
        Returns a short note for patch-status ("" when everything was
        already present), or None if proc_dou8vec_minmax() is still
        missing afterwards - BBRv3 would not compile, so it is skipped.
        """
        sysctl_c = common_dir / "kernel/sysctl.c"

        def dou8vec_present():
            return sysctl_c.exists() and self._DOU8VEC_EXPORT in sysctl_c.read_text()

        prereqs = [
            ("bbrv3_prereq_sysctl_dou8vec_minmax.patch", dou8vec_present),
            ("bbrv3_prereq_sysctl_dou8vec_minmax_races.patch", None),
        ]
        notes = []
        for name, present_check in prereqs:
            p = patch_dir / name
            if not p.exists():
                notes.append(f"{name} missing")
                continue
            # -f, not --batch: with --batch, `patch -R` on a patch that is
            # NOT applied says "Unreversed patch detected! Ignoring -R"
            # and dry-runs it forward, which would look like "present".
            # Both probes are silenced: they are expected to fail on one
            # side, and a stray "N out of M hunks FAILED" in the build log
            # reads like a real problem. The decision is logged below.
            reverse = self._run_cmd(
                f"patch -p1 -R -f -F 0 --dry-run -s < {p} > /dev/null 2>&1", check=False)
            if reverse.returncode == 0 or (present_check and present_check()):
                logger.info(f"BBRv3 prereq {name}: already in this kernel - not applied")
                continue
            forward = self._run_cmd(
                f"patch -p1 -N -F 0 --dry-run --batch -s < {p} > /dev/null 2>&1", check=False)
            if forward.returncode != 0:
                logger.warning(
                    f"BBRv3 prereq {name}: neither present nor cleanly "
                    f"applicable - left the tree untouched")
                notes.append(f"{name} not applied")
                continue
            res = self._run_cmd(f"patch -p1 -N -F 0 --batch --no-backup-if-mismatch < {p}", check=False)
            if res.returncode != 0:
                logger.warning(f"BBRv3 prereq {name}: dry-run clean but real apply failed")
                notes.append(f"{name} failed")
                continue
            notes.append(f"{name} applied")

        if not dou8vec_present():
            logger.warning(
                "BBRv3: proc_dou8vec_minmax() is not in kernel/sysctl.c and "
                "its prereq patch could not be applied - skipping BBRv3 "
                "rather than building something that cannot link.")
            return None
        return "; ".join(notes)

    # fs/namespace.c: on SOME specific branches/sub_levels, the SUSFS
    # patch was written against a version of this file that lacks
    # "#include <trace/hooks/blk.h>" (Google added it later), so the
    # patch's context window doesn't match unless we remove it first and
    # restore it after. This must stay gated to the exact
    # android/sub_level combinations where that's true (confirmed against
    # WildKernels/GKI_KernelSU_SUSFS's own build pipeline) - on other
    # branches (e.g. android15-6.6) the file already matches the patch's
    # expected context as-is, and removing the include only breaks a
    # hunk that would otherwise apply cleanly.
    _NAMESPACE_C_BLK_INCLUDE_FIX_RANGES = {
        ("android13", "5.15"): 197,
        ("android14", "6.1"): 157,
    }

    def _namespace_c_blk_include_fix_applies(self) -> bool:
        threshold = self._NAMESPACE_C_BLK_INCLUDE_FIX_RANGES.get(
            (self.config.android_version, self.config.kernel_version)
        )
        if threshold is None:
            return False
        sub_level = self.config.get_sub_level_int()
        return sub_level is not None and sub_level >= threshold

    def _preprocess_namespace_c_susfs_include(self) -> bool:
        if not self._namespace_c_blk_include_fix_applies():
            return False
        namespace_c = self.work_dir / "common/fs/namespace.c"
        if not namespace_c.exists():
            return False
        content = namespace_c.read_text()
        include_line = "#include <trace/hooks/blk.h>\n"
        if include_line not in content:
            return False
        # Newer susfs patches (054b2432+, re-based onto 5.15.211) already
        # carry this include in their fs/namespace.c context. Removing it
        # then breaks hunk #1 instead of fixing it - leave the file alone.
        patch_file = self.work_dir / "common" / self.config.get_susfs_patch_filename()
        if patch_file.exists():
            ptxt = patch_file.read_text(errors="surrogateescape")
            ns = ptxt.find("diff --git a/fs/namespace.c")
            if ns != -1:
                nxt = ptxt.find("\ndiff --git ", ns + 1)
                section = ptxt[ns:nxt if nxt != -1 else len(ptxt)]
                if "\n " + include_line in section:
                    logger.info(
                        "fs/namespace.c: SUSFS patch already expects "
                        "'trace/hooks/blk.h' in its context - include left in place"
                    )
                    return False
        namespace_c.write_text(content.replace(include_line, "", 1))
        logger.info(
            "fs/namespace.c: temporarily removed 'trace/hooks/blk.h' include "
            "so the SUSFS patch context matches (restored after patching)"
        )
        return True

    def _restore_namespace_c_susfs_include(self):
        namespace_c = self.work_dir / "common/fs/namespace.c"
        if not namespace_c.exists():
            return
        content = namespace_c.read_text()
        if "#include <trace/hooks/blk.h>" in content:
            return
        anchor = '#include "internal.h"\n'
        if anchor in content:
            namespace_c.write_text(content.replace(anchor, anchor + "#include <trace/hooks/blk.h>\n", 1))
            logger.info("fs/namespace.c: restored 'trace/hooks/blk.h' include after SUSFS patch")

    # fs/namei.c: the SUSFS patch adds set_nameidata(nd, old_dfd,
    # fake_filename, NULL) - 4 args - unconditionally, but only 5.10
    # kernels (android12-5.10, android13-5.10) still have the 3-param
    # set_nameidata(p, dfd, name) - there's no 4th/root param on that
    # branch. On android13-5.15+ set_nameidata legitimately HAS a 4th
    # param, so the same call text is correct there and must NOT be
    # touched - this must stay gated to exactly the 5.10 branches
    # (confirmed against WildKernels/GKI_KernelSU_SUSFS's own build
    # pipeline, which gates it the same way) rather than a blind
    # string-match across all kernel versions.
    def _fix_namei_c_set_nameidata_arity(self):
        if not (self.config.kernel_version == "5.10"
                and self.config.android_version in ("android12", "android13")):
            return
        namei_c = self.work_dir / "common/fs/namei.c"
        if not namei_c.exists():
            return
        content = namei_c.read_text()
        broken_call = "set_nameidata(nd, old_dfd, fake_filename, NULL)"
        fixed_call = "set_nameidata(nd, old_dfd, fake_filename)"
        if broken_call not in content:
            return
        count = content.count(broken_call)
        namei_c.write_text(content.replace(broken_call, fixed_call))
        logger.info(
            f"fs/namei.c: fixed {count} set_nameidata() call(s) with a stray "
            f"4th argument the function doesn't declare on 5.10 kernels"
        )

    # android16-6.12: two known source-vs-patch drift issues, confirmed
    # against WildKernels/GKI_KernelSU_SUSFS's own build pipeline. Same
    # remove-before/restore-after pattern as the namespace.c blk.h fix
    # above - gated to the exact sub_level thresholds where each is
    # needed, not applied blindly to every android16-6.12 build.
    def _preprocess_android16_fake_patches(self) -> dict:
        applied = {"exec_dma_buf": False, "task_mmu_vma_rename": False}
        if not (self.config.android_version == "android16" and self.config.kernel_version == "6.12"):
            return applied
        sub_level = self.config.get_sub_level_int()
        if sub_level is None:
            return applied

        if sub_level >= 58:
            exec_c = self.work_dir / "common/fs/exec.c"
            if exec_c.exists():
                content = exec_c.read_text()
                include_line = "#include <linux/dma-buf.h>\n"
                if include_line in content:
                    exec_c.write_text(content.replace(include_line, "", 1))
                    applied["exec_dma_buf"] = True
                    logger.info(
                        "fs/exec.c: temporarily removed 'linux/dma-buf.h' include "
                        "(android16-6.12 >=58, restored after patching)"
                    )

        if sub_level >= 69:
            task_mmu_c = self.work_dir / "common/fs/proc/task_mmu.c"
            if task_mmu_c.exists():
                content = task_mmu_c.read_text()
                if "vma_data_pages" in content:
                    task_mmu_c.write_text(content.replace("vma_data_pages", "vma_pages"))
                    applied["task_mmu_vma_rename"] = True
                    logger.info(
                        "fs/proc/task_mmu.c: temporarily renamed vma_data_pages -> "
                        "vma_pages (android16-6.12 >=69, restored after patching)"
                    )

        return applied

    def _restore_android16_fake_patches(self, applied: dict):
        if applied.get("exec_dma_buf"):
            exec_c = self.work_dir / "common/fs/exec.c"
            if exec_c.exists():
                content = exec_c.read_text()
                if "#include <linux/dma-buf.h>" not in content:
                    head, sep, rest = content.partition("#include ")
                    if sep:
                        line_end = rest.find("\n") + 1
                        content = head + sep + rest[:line_end] + "#include <linux/dma-buf.h>\n" + rest[line_end:]
                        exec_c.write_text(content)
                        logger.info("fs/exec.c: restored 'linux/dma-buf.h' include")

        if applied.get("task_mmu_vma_rename"):
            task_mmu_c = self.work_dir / "common/fs/proc/task_mmu.c"
            if task_mmu_c.exists():
                content = task_mmu_c.read_text()
                task_mmu_c.write_text(content.replace("vma_pages", "vma_data_pages"))
                logger.info("fs/proc/task_mmu.c: restored vma_pages -> vma_data_pages")

    # mm/mmap.c: some SUSFS patch hunks call vm_flags_clear() - a VMA
    # helper Google added to kernel/common at different os_patch_levels
    # per branch (same story as VMA_PAD_START/page-size-migration: a
    # later Google addition that older os_patch_levels in our build
    # matrix predate). When missing, this fails with "implicit
    # declaration of function 'vm_flags_clear'". We fall back to a
    # direct vm_flags &= ~flags definition, but only if it's genuinely
    # not declared anywhere upstream (checked broadly across include/,
    # not just one hardcoded header) to avoid a redefinition error on
    # sub_levels where it already exists.
    def _fix_vm_flags_clear_compat(self):
        common_dir = self.work_dir / "common"
        mmap_c = common_dir / "mm/mmap.c"
        if not mmap_c.exists():
            return
        content = mmap_c.read_text()
        if "vm_flags_clear(" not in content or "VM_FLAGS_CLEAR_COMPAT_DEFINED" in content:
            return

        include_dir = common_dir / "include"
        if include_dir.exists():
            for header in include_dir.rglob("*.h"):
                try:
                    if "vm_flags_clear" in header.read_text(errors="ignore"):
                        return  # already declared upstream, nothing to do
                except OSError:
                    continue

        fallback = (
            "\n#ifndef VM_FLAGS_CLEAR_COMPAT_DEFINED\n"
            "#define VM_FLAGS_CLEAR_COMPAT_DEFINED\n"
            "static inline void vm_flags_clear(struct vm_area_struct *vma, unsigned long flags)\n"
            "{\n"
            "\tvma->vm_flags &= ~flags;\n"
            "}\n"
            "#endif\n"
        )
        lines = content.split("\n")
        include_indices = [i for i, l in enumerate(lines) if l.startswith("#include")]
        insert_at = (max(include_indices) + 1) if include_indices else 0
        lines.insert(insert_at, fallback)
        mmap_c.write_text("\n".join(lines))
        logger.info(
            "mm/mmap.c: added vm_flags_clear() compat fallback (not "
            "declared upstream for this sub_level)"
        )

    def apply_susfs_patches(self):
        logger.info("=== Applying SUSFS patches ===")
        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        if not susfs_patch.exists():
            raise RuntimeError(
                f"SUSFS patch file not found: {susfs_patch}\n"
                f"The susfs4ksu checkout (at {self.susfs_dir}) may be on the "
                f"wrong branch (expected '{self.config.kernel_branch}'), or "
                f"susfs4ksu has renamed/moved this file upstream. Check: "
                f"https://github.com/ShirkNeko/susfs4ksu/tree/{self.config.kernel_branch}/kernel_patches\n"
                f"This is a hard stop - continuing without this patch produces "
                f"a kernel that fails to link (undefined susfs_* symbols)."
            )
        self._run_cmd(f"cp {susfs_patch} {common_dir}/", check=False)
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs/"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux/"),
        ]:
            if src.exists():
                self._run_cmd(f"cp -r {src}/* {dst}", check=False)

        removed_blk_include = self._preprocess_namespace_c_susfs_include()
        android16_applied = self._preprocess_android16_fake_patches()

        patch_file = common_dir / self.config.get_susfs_patch_filename()
        self._chdir(common_dir)
        result = self._run_cmd(f"patch -p1 --fuzz=3 < {patch_file}", check=False)
        self._chdir(self.work_dir)

        if removed_blk_include:
            self._restore_namespace_c_susfs_include()
        self._restore_android16_fake_patches(android16_applied)

        self._fix_namei_c_set_nameidata_arity()
        self._fix_vm_flags_clear_compat()
        self._fixup_susfs_exec_c_no_preinit(common_dir)

        if result.returncode != 0:
            self._mark("susfs", "failed", f"patch exit code {result.returncode}")
            raise RuntimeError(
                f"SUSFS patch failed to apply cleanly: {patch_file} "
                f"(patch exit code {result.returncode}). The kernel source "
                f"may have diverged from what this SUSFS patch expects - "
                f"check the build log above for rejected hunks."
            )
        self._mark("susfs", "applied")

    def _fixup_susfs_exec_c_no_preinit(self, common_dir) -> bool:
        """Newer susfs (3ebb8a6+) wires SukiSU/KernelSU module preinit into
        fs/exec.c, guarded by #ifndef KSU_NO_PREINIT. Our SUSFS builds compile
        preinit out of drivers/kernelsu (-DKSU_NO_PREINIT, see
        _disable_susfs_preinit), so preinit/preinit.o is not built and
        exec.c's calls to ksu_setup_preinit_file() /
        ksu_preinit_handle_post_execveat() would be undefined at link time.
        Give fs/exec.o the same define. No-op for older susfs (no such
        guard in exec.c) and idempotent."""
        exec_c = Path(common_dir) / "fs/exec.c"
        makefile = Path(common_dir) / "fs/Makefile"
        if not exec_c.exists() or not makefile.exists():
            return False
        if self._keep_susfs_preinit():
            return True  # preinit is built, exec.c may call into it
        if "#ifndef KSU_NO_PREINIT" not in exec_c.read_text(errors="surrogateescape"):
            return True
        mk = makefile.read_text()
        line = "CFLAGS_exec.o += -DKSU_NO_PREINIT\n"
        if line in mk:
            return True
        if not mk.endswith("\n"):
            mk += "\n"
        mk += ("# KSU_SUSFS_NO_PREINIT: preinit is compiled out of drivers/kernelsu\n"
               "# in SUSFS builds, keep exec.c from calling into it.\n" + line)
        makefile.write_text(mk)
        logger.info("fs/Makefile: fs/exec.o built with -DKSU_NO_PREINIT "
                    "(susfs preinit hooks compiled out, see _fixup_susfs_exec_c_no_preinit)")
        self._mark("susfs_exec_no_preinit", "applied")
        return True

    _SELINUX_HIDE_GUARD_FIXES = [
        (
            "    if (security_dump_masked_av_fn)\n"
            "        security_dump_masked_av_fn(policydb, scontext, tcontext, tclass, masked, \"bounds\");\n",
            "    security_dump_masked_av_fn(policydb, scontext, tcontext, tclass, masked, \"bounds\");\n",
        ),
        (
            "    if (context_struct_compute_av_fn) {\n"
            "        context_struct_compute_av_fn(policydb, scontext, tcontext, tclass, avd, NULL);\n"
            "    } else {\n"
            "        context_struct_compute_av(policydb, scontext, tcontext, tclass, avd, NULL);\n"
            "    }\n",
            "    context_struct_compute_av_fn(policydb, scontext, tcontext, tclass, avd, NULL);\n",
        ),
    ]

    def _fix_selinux_hide_always_true_guards(self):
        """Removes NULL-checks that became always-true after the SUSFS patch.

        SukiSU-Ultra v4.2.0 declares these two as function POINTERS,
        filled in at init by its symbol resolver:

            static void (*security_dump_masked_av_fn)(...) = NULL;
            static void (*context_struct_compute_av_fn)(...) = NULL;

        so `if (security_dump_masked_av_fn)` is a meaningful NULL check.

        susfs4ksu's 10_enable_susfs_for_ksu.patch replaces that whole
        block with real extern FUNCTION declarations, because its
        kernel-side patch (50_add_susfs_in_gki-*.patch) defines both
        symbols in security/selinux/ss/services.c - verified present
        there, so they always exist and can be called unconditionally.
        What the patch does NOT do is remove the now-pointless NULL
        checks it left behind. Testing a function's address as a boolean
        is always true, and clang says so:

            error: address of function 'security_dump_masked_av_fn' will
            always evaluate to 'true' [-Werror,-Wpointer-bool-conversion]

        This only bites on 6.6 and up: both the declarations and the
        call sites live inside
        `#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)`, so on
        android12/13/14 the whole region is preprocessed away and never
        compiled. That is why android14-6.1 built fine with the exact
        same SukiSU tag and the byte-identical SUSFS patch.

        Dropping the guard (rather than silencing the warning) matches
        what the patched code actually means: call the function that is
        now guaranteed to be linked in. The dead `else` branch calling
        the local static context_struct_compute_av() goes with it.
        """
        target = self.work_dir / "common/drivers/kernelsu/feature/selinux_hide.c"
        if not target.exists():
            target = self.work_dir / "KernelSU/kernel/feature/selinux_hide.c"
        if not target.exists():
            logger.info("selinux_hide.c not found - skipping always-true guard fix")
            self._mark("selinux_hide_guards", "skipped", "file not found")
            return

        text = target.read_text()
        applied = 0
        for old, new in self._SELINUX_HIDE_GUARD_FIXES:
            if old in text:
                text = text.replace(old, new, 1)
                applied += 1

        if applied == 0:
            logger.info(
                "selinux_hide.c has no always-true _fn guards to fix "
                "(already handled upstream, or this SukiSU/SUSFS pairing "
                "doesn't produce them)."
            )
            self._mark("selinux_hide_guards", "skipped", "no matching guards")
            return

        target.write_text(text)
        logger.info(f"selinux_hide.c: removed {applied} always-true _fn guard(s) "
                    f"(-Wpointer-bool-conversion would be fatal on 6.6+)")
        self._mark("selinux_hide_guards", "applied", f"{applied} guard(s) removed")

    def apply_susfs_kernelsu_patch(self):
        # susfs4ksu ships in two halves:
        #   1) kernel_patches/50_add_susfs_...patch - patches the AOSP
        #      kernel tree (fs/, mm/, security/selinux/...) with the
        #      SUSFS core (fs/susfs.c) wrapped in #ifdef CONFIG_KSU_SUSFS.
        #      apply_susfs_patches() above handles this half.
        #   2) kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch -
        #      patches the KernelSU/SukiSU-Ultra tree itself. This is
        #      the half that actually DEFINES the CONFIG_KSU_SUSFS
        #      Kconfig symbol (kernel/Kconfig) and wires KernelSU's own
        #      hooks (sucompat.c, setuid_hook.c, selinux.c, core/init.c)
        #      to call into susfs_init()/susfs_set_current_proc_no_su()/
        #      etc. SukiSU-Ultra's own upstream Kconfig never defines
        #      CONFIG_KSU_SUSFS on its own.
        #
        # Without this second half, CONFIG_KSU_SUSFS=y written into
        # gki_defconfig by configure_kernel() is a reference to an
        # UNDEFINED Kconfig symbol. Kconfig silently drops unknown
        # symbols during defconfig processing - no error, no warning.
        # fs/Makefile's `obj-$(CONFIG_KSU_SUSFS) += susfs.o` then
        # resolves to nothing, fs/susfs.c never gets compiled, and the
        # kernel builds and "succeeds" while SUSFS is completely absent
        # from the flashed Image - despite the SUSFS patch itself having
        # applied cleanly and the pre-build config summary showing
        # CONFIG_KSU_SUSFS=y (that summary reflects what was written to
        # the defconfig text file, not what Kconfig actually accepted).
        logger.info("=== Applying SUSFS<->KernelSU integration patch ===")
        ksu_dir = self.work_dir / "KernelSU"
        patch_file = self.susfs_dir / "kernel_patches" / "KernelSU" / "10_enable_susfs_for_ksu.patch"
        if not ksu_dir.exists():
            raise RuntimeError(
                f"KernelSU checkout not found at {ksu_dir} - add_kernelsu() "
                f"must run before apply_susfs_kernelsu_patch()."
            )
        if not patch_file.exists():
            raise RuntimeError(
                f"SUSFS<->KernelSU integration patch not found: {patch_file}\n"
                f"susfs4ksu may have renamed/moved this file upstream. Check: "
                f"https://github.com/ShirkNeko/susfs4ksu/tree/{self.config.kernel_branch}/kernel_patches/KernelSU\n"
                f"This is a hard stop - continuing without this patch produces "
                f"a kernel where CONFIG_KSU_SUSFS is never actually defined, "
                f"so SUSFS silently compiles out entirely despite the main "
                f"SUSFS patch and PATCH_STATUS.json both reporting success."
            )
        src_note = ""
        if self.susfs_ksu_patch_ref:
            # Split pin: the KernelSU half comes from an older susfs commit
            # than the kernel half. Extract that version next to the build.
            ref = self.susfs_ksu_patch_ref
            extracted = self.work_dir / f"10_enable_susfs_for_ksu.from-{ref[:12]}.patch"
            res = self._run_cmd(
                f"git -C {self.susfs_dir} show "
                f"{ref}:kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch > {extracted}",
                check=False,
            )
            if res.returncode != 0 or not extracted.exists() or extracted.stat().st_size == 0:
                self._mark("susfs_kernelsu_integration", "failed", f"could not extract patch from {ref[:12]}")
                raise RuntimeError(
                    f"Could not extract KernelSU/10_enable_susfs_for_ksu.patch "
                    f"from susfs4ksu {ref[:12]}."
                )
            patch_file = extracted
            src_note = f"KernelSU patch from susfs {ref[:12]}"
            logger.info(f"Using the split-pin KernelSU patch: {extracted.name}")
        self._chdir(ksu_dir)
        result = self._run_cmd(f"patch -p1 --fuzz=3 < {patch_file}", check=False)
        self._chdir(self.work_dir)
        if result.returncode != 0:
            recovered = self._recover_susfs_init_c(ksu_dir)
            if not recovered:
                self._mark("susfs_kernelsu_integration", "failed", f"patch exit code {result.returncode}")
                raise RuntimeError(
                    f"SUSFS<->KernelSU integration patch failed to apply cleanly "
                    f"(patch exit code {result.returncode}). The KernelSU/"
                    f"SukiSU-Ultra tree may have diverged from what this patch "
                    f"expects - check the build log above for rejected hunks. "
                    f"Continuing would produce a kernel with CONFIG_KSU_SUSFS "
                    f"silently undefined."
                )
            src_note = "; ".join(x for x in (src_note, "manual SukiSU drift fixup (see log)") if x)
        if not self._disable_susfs_preinit(ksu_dir):
            self._mark("susfs_kernelsu_integration", "failed", "unknown preinit block in kernel/Kbuild")
            raise RuntimeError(
                "SukiSU-Ultra's kernel/Kbuild has a preinit block in a shape "
                "_disable_susfs_preinit() doesn't know. Preinit needs syscall "
                "hooks the SUSFS patch removes, so building it would fail to "
                "link (or silently never run). Stopping."
            )
        if not self._fixup_susfs_sucompat_util_h(ksu_dir):
            self._mark("susfs_kernelsu_integration", "failed", "sucompat.c util.h fixup")
            raise RuntimeError(
                "kernel/feature/sucompat.c uses userspace_stack_buffer() but neither "
                "defines it nor can get it from include/util.h in a known way "
                "(_fixup_susfs_sucompat_util_h). Stopping."
            )
        if not self._fixup_susfs_pkg_observer_c(ksu_dir):
            self._mark("susfs_kernelsu_integration", "failed", "pkg_observer.c priority call")
            raise RuntimeError(
                "kernel/manager/pkg_observer.c: the susfs_parse_priority_app_strings() "
                "call from the SUSFS patch is in a place _fixup_susfs_pkg_observer_c() "
                "doesn't know (patch --fuzz may have put it outside a function). Stopping."
            )
        if not self._disable_susfs_mount_hide(ksu_dir):
            self._mark("susfs_kernelsu_integration", "failed", "unknown mount_hide shape")
            raise RuntimeError(
                "SukiSU-Ultra's feature/mount_hide (kernel/Kbuild or "
                "feature/mount_hide.h) is in a shape _disable_susfs_mount_hide() "
                "doesn't know. mount_hide needs hook/<arch>/patch_memory.o, which "
                "the SUSFS patch removes, so building it would fail to link. Stopping."
            )
        self._mark("susfs_kernelsu_integration", "applied", src_note)

    def _recover_susfs_init_c(self, ksu_dir) -> bool:
        # Known, narrow drift point: 10_enable_susfs_for_ksu.patch was
        # authored against upstream KernelSU, not SukiSU-Ultra. Hunks
        # #1/#2/#4/#6/#8 of its kernel/core/init.c diff apply fine (they
        # land via `patch`'s own fuzzy matching before we ever get
        # here). Hunks #3 (include-list cleanup), #5 (top of
        # kernelsu_init(): ksu_bundled / x86 check / ksu_late_loaded)
        # and #7 (the main kernelsu_init() body reorg) don't - SukiSU-
        # Ultra has extra code in or next to all three regions (a
        # "feature/uts_spoof.h" include, the spoof_release/spoof_version
        # module params and the ksu_spoof_version() block inside
        # kernelsu_init()) that shifts context just enough to break the
        # fuzzy match.
        #
        # This isn't optional cleanup we can skip by leaving the old
        # code in place: kernel/Kbuild's own hunk (which DOES apply
        # cleanly) drops hook/lsm_hook.o, hook/syscall_hook_manager.o,
        # hook/syscall_event_bridge.o, hook/tp_marker.o, the arch-
        # specific hook/*/syscall_hook.o, and infra/symbol_resolver.o
        # from the build entirely - because feature/sucompat.c and
        # hook/setuid_hook.c (already patched to a NEW calling
        # convention by this same patch's other, successful hunks) are
        # meant to replace them, and this patch's own successful hunks
        # to selinux_hide.c/dispatch.c/app_profile.c/setuid_hook.c/
        # ksud_integration.c already migrate every one of THEIR calls
        # off that old machinery too - so leaving the old init.c body
        # calling into it produces "undefined symbol" at link time
        # (verified by grepping the post-patch tree for real callers of
        # every function in every dropped file). The only exception is
        # infra/symbol_resolver.o - see the comment near the end of
        # this function where it gets re-added to Kbuild. The only
        # self-consistent state is finishing the migration: gut the
        # same calls hunk #7 would have removed, and add the same
        # ksu_sucompat_init()/ksu_setuid_hook_init()/susfs_init() calls
        # it would have added, via direct, targeted string edits
        # instead of relying on `patch`'s fuzzy context matching.
        #
        # Each removal/insertion below targets one specific known
        # line/block by exact string match - robust to unrelated
        # insertions elsewhere in the function (like the spoof_version
        # block, which is simply left untouched). If any anchor isn't
        # found or isn't unique, we bail out and let the caller
        # hard-fail rather than guess at a file that's drifted further
        # than this.
        # kernel/feature/selinux_hide.c has its own, separate recovery
        # (see _recover_susfs_selinux_hide_c). Run it first; if it
        # can't handle that file, the whole recovery fails.
        sel_rej = ksu_dir / "kernel/feature/selinux_hide.c.rej"
        if sel_rej.exists():
            if not self._recover_susfs_selinux_hide_c(ksu_dir):
                return False
        # kernel/runtime/ksud_integration.c likewise (SukiSU's
        # second-stage module_blacklist call) - see
        # _recover_susfs_ksud_integration_c.
        ksud_rej = ksu_dir / "kernel/runtime/ksud_integration.c.rej"
        if ksud_rej.exists():
            if not self._recover_susfs_ksud_integration_c(ksu_dir):
                return False
        # kernel/supercall/dispatch.c (SukiSU's preinit flag in
        # do_get_info()) - see _recover_susfs_dispatch_c.
        dispatch_rej = ksu_dir / "kernel/supercall/dispatch.c.rej"
        if dispatch_rej.exists():
            if not self._recover_susfs_dispatch_c(ksu_dir):
                return False
        # kernel/hook/setuid_hook.c (SukiSU's mount_hide.h include) - see
        # _recover_susfs_setuid_hook_c.
        setuid_rej = ksu_dir / "kernel/hook/setuid_hook.c.rej"
        if setuid_rej.exists():
            if not self._recover_susfs_setuid_hook_c(ksu_dir):
                return False

        init_c = ksu_dir / "kernel/core/init.c"
        reject_files = list(ksu_dir.glob("**/*.rej"))
        other_rejects = [r for r in reject_files if r.name != "init.c.rej"]
        if other_rejects or not init_c.exists():
            return False
        if not reject_files:
            # init.c applied cleanly - only selinux_hide.c needed help.
            return True

        content = init_c.read_text()
        original = content
        init_rej = ksu_dir / "kernel/core/init.c.rej"
        rej_text = init_rej.read_text() if init_rej.exists() else ""

        try:
            # --- hunk #3 equivalent: includes + x86 dispatcher-safety block ---
            content = self._replace_once(content, '#include "hook/syscall_hook.h"\n', '')
            content = self._replace_once(
                content,
                '#include "infra/symbol_resolver.h"\n',
                '#include "hook/setuid_hook.h"\n#include "feature/sucompat.h"\n',
            )
            x86_block = (
                '#if defined(__x86_64__) && !defined(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER)\n'
                '#include <asm/cpufeature.h>\n'
                '#include <linux/version.h>\n'
                '#ifndef X86_FEATURE_INDIRECT_SAFE\n'
                '#error "FATAL: Your kernel is missing the indirect syscall bypass patches!"\n'
                '#endif\n'
                '#endif\n'
            )
            content = self._replace_once(content, x86_block, '')

            # susfs.h include (hunk #1 - normally already applied by
            # `patch` itself, but add it if this file drifted enough
            # that even that didn't land)
            if '#include <linux/susfs.h>' not in content:
                include_anchor = '#include <linux/moduleparam.h>\n'
                content = self._replace_once(
                    content, include_anchor, include_anchor + '#include <linux/susfs.h>\n'
                )

            # --- hunk #5 equivalent (SukiSU main / UAPI 4 only) ---
            # Newer SukiSU-Ultra (main after the UAPI 4 bump) grew an
            # x86 runtime X86_FEATURE_INDIRECT_SAFE check, a ksu_bundled
            # module param and a MODULE-only ksu_late_loaded assignment
            # at the top of kernelsu_init(). Hunk #5 removes all three;
            # when it fails to land we do the same by exact match. Each
            # block is optional here: on tag v4.2.0 none of them exist
            # in this form and the hunk either applied or wasn't needed.
            # On a Built-in (CONFIG_KSU=y) arm64 kernel all three are
            # dead code anyway (x86-only / MODULE-only), so removing
            # them cannot change runtime behaviour.
            hunk5_blocks = [
                '#ifdef MODULE\n'
                'bool ksu_bundled = false;\n'
                'module_param_named(bundled, ksu_bundled, bool, 0);\n'
                '#endif\n\n',
                '#if defined(__x86_64__) && !defined(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER)\n'
                '    // If the kernel has the hardening patch, X86_FEATURE_INDIRECT_SAFE must be set\n'
                '    if (!boot_cpu_has(X86_FEATURE_INDIRECT_SAFE)) {\n',
                '#ifdef MODULE\n'
                '    ksu_late_loaded = (current->pid != 1);\n'
                '#else\n'
                '    ksu_late_loaded = false;\n'
                '#endif\n\n',
            ]
            if content.count(hunk5_blocks[0]) == 1:
                content = content.replace(hunk5_blocks[0], '', 1)
            x86_rt = re.compile(
                re.escape(hunk5_blocks[1]) + r'.*?        return -ENOSYS;\n    \}\n#endif\n\n',
                re.DOTALL,
            )
            if len(x86_rt.findall(content)) == 1:
                content = x86_rt.sub('', content, count=1)
            if content.count(hunk5_blocks[2]) == 1:
                content = content.replace(hunk5_blocks[2], '', 1)

            # --- hunk #7 equivalent: retire the old hook-manager/
            # symbol-resolver init sequence, wire up the new one ---
            for dead in [
                '    ksu_init_symbol_resolver();\n\n',
                '    ksu_syscall_hook_init();\n\n',
                '    ksu_lsm_hook_init();\n',
            ]:
                content = self._replace_once(content, dead, '')
            # SukiSU-Ultra's extension hub (edf86467, 2026-10) calls
            # ksu_extension_init() + ksu_extension_register_all() right
            # after ksu_app_profile_init(), so that line is no longer
            # followed by a blank line. Drop only ksu_app_profile_init()
            # (as hunk #7 does) and keep SukiSU's two extension calls.
            ext_calls = (
                '    ksu_extension_init();\n'
                '    ksu_extension_register_all();\n'
            )
            if content.count('    ksu_app_profile_init();\n' + ext_calls) == 1:
                content = content.replace(
                    '    ksu_app_profile_init();\n' + ext_calls, ext_calls, 1
                )
            else:
                content = self._replace_once(content, '    ksu_app_profile_init();\n\n', '')

            call_anchor = '    ksu_feature_init();\n'
            content = self._replace_once(
                content,
                call_anchor,
                '#ifdef CONFIG_KSU_SUSFS\n    susfs_init();\n#endif // #ifdef CONFIG_KSU_SUSFS\n\n'
                + call_anchor
                + '\n    ksu_sucompat_init();\n\n    ksu_setuid_hook_init();\n',
            )

            # The late_loaded branch: both arms together called
            # ksu_syscall_hook_manager_init (now-removed, dropped
            # entirely), ksu_allowlist_init, ksu_throne_tracker_init,
            # ksu_ksud_init (else-branch only), ksu_file_wrapper_init,
            # plus late-load-only bookkeeping (escape_to_root_for_init,
            # apply_kernelsu_rules, cache_sid, setup_ksu_cred,
            # ksu_load_allow_list, ksu_observer_init, ksu_boot_completed,
            # track_throne, getenforce/setenforce) that only mattered
            # for the ksu_late_loaded distinction we're removing
            # entirely (hunks #4/#5 already dropped its declaration and
            # assignment). Keep the four calls both arms agreed on,
            # drop the rest, matching hunk #7's own resolution.
            late_loaded_re = re.compile(
                r'    if \(ksu_late_loaded\) \{.*?\n\nvoid __exit kernelsu_exit\(void\)\n\{\n',
                re.DOTALL,
            )
            if len(late_loaded_re.findall(content)) != 1:
                return False
            # SukiSU-Ultra's own module preinit (3b607917, 2026-10)
            # adds ksu_preinit_init() to the normal-boot arm, right
            # after ksu_ksud_init(). It isn't in the upstream hunk, so
            # carry it over. In a SUSFS build preinit is compiled out
            # (see _disable_susfs_preinit), so this is the header's
            # empty stub - kept so the file stays as close to SukiSU's
            # as possible.
            late_block = late_loaded_re.search(content).group(0)
            preinit_init = (
                '    ksu_preinit_init();\n\n'
                if '        ksu_preinit_init();\n' in late_block else ''
            )
            replacement = (
                '    ksu_allowlist_init();\n\n'
                '    ksu_throne_tracker_init();\n\n'
                '    ksu_ksud_init();\n\n'
                + preinit_init +
                '    ksu_file_wrapper_init();\n\n'
                '    return 0;\n'
                '}\n\n'
                'void __exit kernelsu_exit(void)\n{\n'
            )
            # The normal-boot ("else") arm is what a Built-in kernel
            # actually runs. Every call SukiSU puts there must survive,
            # except the hook manager the SUSFS patch replaces. A call we
            # don't know (like ksu_preinit_init() was) would otherwise be
            # dropped silently - the build passes, the feature is gone.
            else_arm = late_block.split('    } else {\n', 1)
            if len(else_arm) != 2 or '\n    }\n' not in else_arm[1]:
                return False
            else_arm = else_arm[1].split('\n    }\n', 1)[0]
            kept = {l.strip() for l in replacement.splitlines()}
            lost = [
                l.strip() for l in else_arm.splitlines()
                if self._INIT_C_CALL.match(l.strip())
                and l.strip() not in kept
                and l.strip() != 'ksu_syscall_hook_manager_init();'
            ]
            if lost:
                for l in lost:
                    logger.error(
                        f"kernel/core/init.c recovery: {l!r} in the normal-boot arm of "
                        "kernelsu_init() is new - not carried over"
                    )
                return False
            content = late_loaded_re.sub(lambda _m: replacement, content, count=1)

            # kernelsu_exit(): its own hook-manager teardown call, and
            # the ksu_late_loaded guard around ksu_ksud_exit() (always
            # run it now - there's no late-load distinction left).
            content = self._replace_once(
                content,
                '    // Phase 1: Stop all hooks first to prevent new callbacks\n'
                '    ksu_syscall_hook_manager_exit();\n\n',
                '',
            )
            # Since 3b607917 SukiSU-Ultra also tears down preinit under
            # the same guard (braced block). Accept either form.
            exit_guard_preinit = (
                '    if (!ksu_late_loaded) {\n'
                '        ksu_ksud_exit();\n'
                '        ksu_preinit_exit();\n'
                '    }\n'
            )
            if content.count(exit_guard_preinit) == 1:
                content = content.replace(
                    exit_guard_preinit, '    ksu_ksud_exit();\n    ksu_preinit_exit();\n', 1
                )
            else:
                content = self._replace_once(
                    content,
                    '    if (!ksu_late_loaded)\n        ksu_ksud_exit();\n',
                    '    ksu_ksud_exit();\n',
                )
        except ValueError:
            # One of the anchors above wasn't found, or wasn't unique -
            # this file has drifted further than we know how to handle.
            return False

        if 'ksu_late_loaded' in content:
            # Something (e.g. a declaration/assignment `patch` didn't
            # manage to remove) is still hanging around - don't ship a
            # file we're not confident compiles.
            return False

        # The edits above are fixed strings written for the hunks as
        # they are today. If susfs4ksu later adds something new to one
        # of the rejected hunks (an extra init call, a new include),
        # those edits would still "succeed" and the new line would
        # silently never land - the build passes, the feature is gone.
        # So check the result against what the rejected hunks actually
        # asked for, the same way the other two recoveries do.
        problems = self._check_init_c_against_rej(rej_text, original, content)
        # And the other direction: SukiSU-only calls the hunks don't
        # mention (like ksu_extension_init() / ksu_extension_exit()) must
        # all still be there. Only the late-load block may lose calls
        # (its normal-boot arm is checked separately above).
        problems += self._check_init_c_untouched_calls(rej_text, original, content)
        if problems:
            for p in problems:
                logger.error(f"kernel/core/init.c recovery: {p}")
            return False

        # kernel/Kbuild's own hunk (which DOES apply cleanly, separate
        # from init.c's failure) drops infra/symbol_resolver.o along
        # with the rest of the old hook-manager machinery, on the
        # assumption that nothing outside that old machinery used it.
        # That's no longer true: SukiSU-Ultra has since added
        # feature/cpu_spoof.c and feature/uts_spoof.c (anti-detection
        # spoofing, unrelated to SUSFS or to anything this patch
        # touches), and kernel/kpm/compact.c, all of which call
        # find_kernel_symbol_exact() directly - so it has to stay
        # compiled even though nothing calls ksu_init_symbol_resolver()
        # (the one-time fast-path warmup) anymore. Checked every other
        # dropped object file's functions the same way (grep for
        # external callers across the whole post-patch tree, not the
        # pristine one - the patch's own successful hunks to
        # selinux_hide.c/dispatch.c/app_profile.c/setuid_hook.c/
        # ksud_integration.c already migrate them off the rest of the
        # old hook machinery) - infra/symbol_resolver.o is the only one
        # that's still a real dependency.
        kbuild = ksu_dir / "kernel/Kbuild"
        if kbuild.exists():
            kbuild_content = kbuild.read_text()
            anchor = 'kernelsu-objs += infra/su_mount_ns.o\n'
            if (
                'infra/symbol_resolver.o' not in kbuild_content
                and kbuild_content.count(anchor) == 1
            ):
                kbuild.write_text(
                    kbuild_content.replace(
                        anchor, anchor + 'kernelsu-objs += infra/symbol_resolver.o\n', 1
                    )
                )

        n_rej = self._rej_hunk_count(rej_text)
        init_c.write_text(content)
        for r in reject_files:
            r.unlink()
        orig = ksu_dir / "kernel/core/init.c.orig"
        if orig.exists():
            orig.unlink()
        logger.info(
            f"kernel/core/init.c: {n_rej} hunk(s) of the SUSFS<->KernelSU patch "
            "didn't match (known drift, normally #3/#5/#7 - see "
            "_recover_susfs_init_c). The result was checked against every "
            "line those hunks add or remove. "
            "Manually finished the same migration those hunks were "
            "trying to make: retired the old hook-manager/symbol-"
            "resolver init calls (their backing .o files are already "
            "dropped from kernel/Kbuild by this patch's own successful "
            "hunk) and wired up ksu_sucompat_init()/ksu_setuid_hook_init()/"
            "susfs_init() in their place. Also re-added "
            "infra/symbol_resolver.o to kernel/Kbuild - it's still "
            "needed by feature/cpu_spoof.c and feature/uts_spoof.c, "
            "which Kbuild's own hunk didn't account for."
        )
        return True

    # Functions that 10_enable_susfs_for_ksu.patch deletes outright from
    # kernel/feature/selinux_hide.c. susfs re-implements them on the
    # kernel side (security/selinux/hooks.c + selinuxfs.c, from
    # 50_add_susfs_in_gki-*.patch), so whatever SukiSU changes INSIDE
    # their bodies never reaches a SUSFS build anyway.
    _SELINUX_HIDE_DELETED_FUNCS = re.compile(
        r'^(static [^\n;]*\b(?:my_write_context|my_write_access|my_setprocattr)\([^\n;]*\)\n\{\n)'
        r'.*?^\}\n',
        re.MULTILINE | re.DOTALL,
    )

    @classmethod
    def _strip_selinux_hide_bodies(cls, text: str) -> str:
        return cls._SELINUX_HIDE_DELETED_FUNCS.sub(lambda m: m.group(1) + '}\n', text)

    def _recover_susfs_selinux_hide_c(self, ksu_dir) -> bool:
        # Known, narrow drift point #2: hunk #3 of the selinux_hide.c
        # part of 10_enable_susfs_for_ksu.patch is one big DELETION
        # (~200 lines: the sel_inos enum, my_write_context(),
        # my_write_access(), my_setprocattr() and friends) because
        # susfs moves that logic into the kernel itself. When
        # SukiSU-Ultra edits the body of one of those functions
        # (e.g. 2026-10: b861ffa4 "fix sidtab detection", 49249b6a
        # "Keep original permission check sequence"), the text the
        # hunk wants to delete no longer matches and `patch` rejects
        # it - even though the result would be identical, since those
        # bodies get deleted either way.
        #
        # Safe recovery: for each rejected hunk, find the region it
        # covers in the current file (unique start/end anchors taken
        # from the hunk itself) and compare it with the hunk's "old"
        # side AFTER blanking out the bodies of the deleted functions.
        # Only if everything else matches byte-for-byte do we swap in
        # the hunk's "new" side. Any other drift (new code outside
        # those bodies, renamed functions, a changed include list ...)
        # -> return False and let the build hard-fail.
        target = ksu_dir / "kernel/feature/selinux_hide.c"
        rej = ksu_dir / "kernel/feature/selinux_hide.c.rej"
        if not target.exists() or not rej.exists():
            return False

        hunks = []
        cur = None
        for line in rej.read_text().splitlines(keepends=True):
            if line.startswith('@@'):
                cur = {'old': [], 'new': []}
                hunks.append(cur)
                continue
            if cur is None or line.startswith('\\'):
                continue  # file headers / "\ No newline at end of file"
            tag, body = line[:1], line[1:]
            if tag in (' ', '-'):
                cur['old'].append(body)
            if tag in (' ', '+'):
                cur['new'].append(body)
        if not hunks:
            return False

        content = target.read_text()
        for h in hunks:
            old, new = h['old'], h['new']
            if len(old) < 8:
                return False
            start_anchor = ''.join(old[:3])
            end_anchor = ''.join(old[-3:])
            if content.count(start_anchor) != 1 or content.count(end_anchor) != 1:
                return False
            start = content.index(start_anchor)
            end = content.index(end_anchor) + len(end_anchor)
            if end <= start:
                return False
            region = content[start:end]
            if self._strip_selinux_hide_bodies(region) != self._strip_selinux_hide_bodies(''.join(old)):
                return False
            content = content[:start] + ''.join(new) + content[end:]

        target.write_text(content)
        rej.unlink()
        orig = ksu_dir / "kernel/feature/selinux_hide.c.orig"
        if orig.exists():
            orig.unlink()
        logger.info(
            "kernel/feature/selinux_hide.c: SUSFS<->KernelSU patch hunk(s) "
            "didn't match because SukiSU-Ultra changed the bodies of "
            "my_write_context/my_write_access/my_setprocattr - functions "
            "this patch deletes anyway (susfs re-implements them in the "
            "kernel). Everything outside those bodies matched exactly, so "
            "the hunk's result was applied directly "
            "(see _recover_susfs_selinux_hide_c)."
        )
        return True

    # SukiSU-Ultra's own lines in the second_stage block of
    # ksu_handle_execveat_ksud() (c4d1f7ff / f868135d, 2026-10).
    _KSUD_BLACKLIST_BLOCK = (
        '            // Not earlier: a blocked module failing to load in first stage\n'
        '            // init is fatal, ksuinit uses modules.blocklist for that instead.\n'
        '            ksu_module_blacklist_apply();\n'
    )

    def _recover_susfs_ksud_integration_c(self, ksu_dir) -> bool:
        # Known, narrow drift point #3: SukiSU-Ultra ported upstream
        # KernelSU's module_blacklist (tiann#3881) but, unlike upstream,
        # installs the blacklist when second stage init starts
        # (f868135d - installing it at kernelsu_init can bootloop on
        # first stage modules). That adds ksu_module_blacklist_apply()
        # inside the second_stage block of ksu_handle_execveat_ksud(),
        # exactly the block 10_enable_susfs_for_ksu.patch rewrites (it
        # was written against upstream, which has no such call there),
        # so that hunk is rejected.
        #
        # Recovery: take SukiSU's 3 lines out, check the region now
        # matches the rejected hunk's "old" side byte-for-byte, swap in
        # its "new" side, then put the call back right after
        # ksu_feature_apply_forced() in the rewritten block (same spot
        # SukiSU has it). Any other drift -> False, build hard-fails.
        # Without putting the call back, block_modules would silently
        # do nothing; upstream's ksu_module_blacklist_init() is NOT
        # used - SukiSU's module_blacklist.h has no such function.
        target = ksu_dir / "kernel/runtime/ksud_integration.c"
        rej = ksu_dir / "kernel/runtime/ksud_integration.c.rej"
        if not target.exists() or not rej.exists():
            return False

        hunks = []
        cur = None
        for line in rej.read_text().splitlines(keepends=True):
            if line.startswith('@@'):
                cur = {'old': [], 'new': []}
                hunks.append(cur)
                continue
            if cur is None or line.startswith('\\'):
                continue
            tag, body = line[:1], line[1:]
            if tag in (' ', '-'):
                cur['old'].append(body)
            if tag in (' ', '+'):
                cur['new'].append(body)
        if len(hunks) != 1:
            return False
        old, new = ''.join(hunks[0]['old']), ''.join(hunks[0]['new'])

        content = target.read_text()
        if content.count(self._KSUD_BLACKLIST_BLOCK) != 1:
            return False
        content = content.replace(self._KSUD_BLACKLIST_BLOCK, '', 1)

        # SukiSU-Ultra's module preinit (3b607917, 2026-10) adds
        # ksu_preinit_release() in front of the first statement of both
        # blocks (second_stage and first zygote). Optional: older SukiSU
        # has neither. Taken out here, put back in the same spots after
        # the hunk is applied. In a SUSFS build it is the header's empty
        # stub (see _disable_susfs_preinit).
        release = 'ksu_preinit_release();\n'
        preinit_spots = []
        # Newer susfs (3ebb8a6+) was rebased onto a KernelSU that already
        # has preinit: the hunk itself removes and re-adds the
        # ksu_preinit_release() calls. Only strip/re-add them ourselves
        # when the hunk does not know about them.
        rej_knows_preinit = release in old and release in new
        for nxt in (() if rej_knows_preinit else ('ksu_selinux_hide_handle_second_stage();\n', 'on_post_fs_data();\n')):
            pair = ' ' * 12 + release + ' ' * 12 + nxt
            n = content.count(pair)
            if n > 1:
                return False
            if n == 1:
                content = content.replace(pair, ' ' * 12 + nxt, 1)
                preinit_spots.append(nxt)
        if release in content and not rej_knows_preinit:
            # a ksu_preinit_release() somewhere we don't know about
            return False

        if content.count(old) != 1:
            return False
        content = content.replace(old, new, 1)

        forced = '                ksu_feature_apply_forced();\n'
        if content.count(forced) != 1 or 'ksu_module_blacklist_apply' in content:
            return False
        content = content.replace(
            forced,
            forced + '                // Not earlier: a blocked module failing to load in first stage\n'
                     '                // init is fatal, ksuinit uses modules.blocklist for that instead.\n'
                     '                ksu_module_blacklist_apply();\n',
            1,
        )
        if '#include "feature/module_blacklist.h"' not in content:
            return False
        for nxt in preinit_spots:
            anchor = ' ' * 16 + nxt
            if content.count(anchor) != 1:
                return False
            content = content.replace(anchor, ' ' * 16 + release + anchor, 1)
        if preinit_spots and '#include "preinit/preinit.h"' not in content:
            return False

        target.write_text(content)
        rej.unlink()
        orig = ksu_dir / "kernel/runtime/ksud_integration.c.orig"
        if orig.exists():
            orig.unlink()
        logger.info(
            "kernel/runtime/ksud_integration.c: SUSFS<->KernelSU patch hunk "
            "didn't match because SukiSU-Ultra calls ksu_module_blacklist_apply() "
            "at second stage init (upstream KernelSU doesn't). Applied the hunk "
            "and put the call back after ksu_feature_apply_forced()"
            + (", plus SukiSU's ksu_preinit_release() calls" if preinit_spots else "")
            + " (see _recover_susfs_ksud_integration_c)."
        )
        return True

    # SukiSU-Ultra's preinit flag in do_get_info() (3b607917, 2026-10).
    _DISPATCH_PREINIT_BLOCK = (
        "    // A late load never sees PID 1's selinux_setup, so preinit cannot run.\n"
        "    if (ksu_preinit_enabled_param() && !ksu_late_loaded) {\n"
        "        cmd.flags |= KSU_GET_INFO_FLAG_PREINIT;\n"
        "    }\n"
    )
    # Same block for a SUSFS build: the patch removes ksu_late_loaded
    # (Built-in only, it was always false there).
    _DISPATCH_PREINIT_BLOCK_SUSFS = (
        "    if (ksu_preinit_enabled_param()) {\n"
        "        cmd.flags |= KSU_GET_INFO_FLAG_PREINIT;\n"
        "    }\n"
    )

    def _recover_susfs_dispatch_c(self, ksu_dir) -> bool:
        # Known, narrow drift point #4: 10_enable_susfs_for_ksu.patch
        # removes the ksu_bundled and ksu_late_loaded flag blocks from
        # both do_get_info() and do_get_info_legacy() (it deletes both
        # variables). SukiSU-Ultra 3b607917 ("module preinit") put a
        # KSU_GET_INFO_FLAG_PREINIT block, which also reads
        # ksu_late_loaded, at the end of that region in do_get_info().
        # The do_get_info() hunk then no longer matches; `patch` applies
        # it with fuzz to do_get_info_legacy() instead (same text there),
        # and the legacy hunk, whose text is now gone, is rejected. Net
        # result: legacy is done, do_get_info() still has the old blocks.
        #
        # Recovery: the rejected hunk's "old" side plus SukiSU's preinit
        # block in front of "#if KSU_IS_PR_BUILD" must match exactly once
        # (that is do_get_info()); swap in the hunk's "new" side with the
        # preinit block, minus "&& !ksu_late_loaded" (always true on a
        # Built-in kernel, and the variable no longer exists). In a SUSFS
        # build ksu_preinit_enabled_param() is the header stub returning
        # false (see _disable_susfs_preinit), so the manager is told
        # preinit is unavailable - which is the truth. Any other drift ->
        # False, build hard-fails.
        target = ksu_dir / "kernel/supercall/dispatch.c"
        rej = ksu_dir / "kernel/supercall/dispatch.c.rej"
        if not target.exists() or not rej.exists():
            return False

        hunks = []
        cur = None
        for line in rej.read_text().splitlines(keepends=True):
            if line.startswith('@@'):
                cur = {'old': [], 'new': []}
                hunks.append(cur)
                continue
            if cur is None or line.startswith('\\'):
                continue
            tag, body = line[:1], line[1:]
            if tag in (' ', '-'):
                cur['old'].append(body)
            if tag in (' ', '+'):
                cur['new'].append(body)
        if len(hunks) != 1:
            return False
        old, new = ''.join(hunks[0]['old']), ''.join(hunks[0]['new'])

        pr_build = '#if KSU_IS_PR_BUILD\n'
        if old.count(pr_build) != 1 or new.count(pr_build) != 1:
            return False
        old_suki = old.replace(pr_build, self._DISPATCH_PREINIT_BLOCK + pr_build, 1)
        new_suki = new.replace(pr_build, self._DISPATCH_PREINIT_BLOCK_SUSFS + pr_build, 1)

        content = target.read_text()
        if content.count(old_suki) != 1:
            return False
        content = content.replace(old_suki, new_suki, 1)
        # Both functions must now be free of the two removed variables,
        # and the legacy function must already look like the hunk wanted.
        if 'ksu_late_loaded' in content or 'ksu_bundled' in content:
            return False
        if content.count(new) != 1 or content.count(new_suki) != 1:
            return False
        if '#include "preinit/preinit.h"' not in content:
            return False

        target.write_text(content)
        rej.unlink()
        orig = ksu_dir / "kernel/supercall/dispatch.c.orig"
        if orig.exists():
            orig.unlink()
        logger.info(
            "kernel/supercall/dispatch.c: SUSFS<->KernelSU patch hunk didn't match "
            "because SukiSU-Ultra added a preinit flag to do_get_info(). Removed the "
            "ksu_bundled/ksu_late_loaded blocks as the hunk wanted and kept the preinit "
            "flag without the ksu_late_loaded test (see _recover_susfs_dispatch_c)."
        )
        return True

    # SukiSU-Ultra's mount view hiding (cf41f112, 2026-10).
    _SETUID_UMOUNT_INCLUDE = '#include "feature/kernel_umount.h"\n'
    _SETUID_MOUNT_HIDE_INCLUDE = '#include "feature/mount_hide.h"\n'

    def _recover_susfs_setuid_hook_c(self, ksu_dir) -> bool:
        # Known, narrow drift point #5: SukiSU-Ultra cf41f112 ("mount view
        # hiding for isolated processes", #947) added
        # `#include "feature/mount_hide.h"` right after
        # `#include "feature/kernel_umount.h"` in hook/setuid_hook.c - inside
        # the context of the susfs hunk that rewrites ksu_handle_setresuid(),
        # so that hunk is rejected. (Its ksu_mount_hide_init()/_exit() calls
        # sit after the hunk and apply fine.)
        #
        # Recovery: take the include out, the region must then match the
        # rejected hunk's "old" side exactly once, swap in its "new" side,
        # put the include back after kernel_umount.h. In a SUSFS build the
        # header gives empty stubs (see _disable_susfs_mount_hide). Any
        # other drift -> False, build hard-fails.
        target = ksu_dir / "kernel/hook/setuid_hook.c"
        rej = ksu_dir / "kernel/hook/setuid_hook.c.rej"
        if not target.exists() or not rej.exists():
            return False

        hunks = []
        cur = None
        for line in rej.read_text().splitlines(keepends=True):
            if line.startswith('@@'):
                cur = {'old': [], 'new': []}
                hunks.append(cur)
                continue
            if cur is None or line.startswith('\\'):
                continue
            tag, body = line[:1], line[1:]
            if tag in (' ', '-'):
                cur['old'].append(body)
            if tag in (' ', '+'):
                cur['new'].append(body)
        if len(hunks) != 1:
            return False
        old, new = ''.join(hunks[0]['old']), ''.join(hunks[0]['new'])

        content = target.read_text()
        pair = self._SETUID_UMOUNT_INCLUDE + self._SETUID_MOUNT_HIDE_INCLUDE
        if content.count(self._SETUID_MOUNT_HIDE_INCLUDE) != 1 or content.count(pair) != 1:
            return False
        content = content.replace(pair, self._SETUID_UMOUNT_INCLUDE, 1)

        if content.count(old) != 1:
            return False
        content = content.replace(old, new, 1)

        if content.count(self._SETUID_UMOUNT_INCLUDE) != 1:
            return False
        content = content.replace(self._SETUID_UMOUNT_INCLUDE, pair, 1)
        # SukiSU's init/exit calls (outside the hunk) must still be there.
        if content.count('ksu_mount_hide_init();') != 1 or content.count('ksu_mount_hide_exit();') != 1:
            return False

        target.write_text(content)
        rej.unlink()
        orig = ksu_dir / "kernel/hook/setuid_hook.c.orig"
        if orig.exists():
            orig.unlink()
        logger.info(
            "kernel/hook/setuid_hook.c: SUSFS<->KernelSU patch hunk didn't match because "
            'SukiSU-Ultra added #include "feature/mount_hide.h". Applied the hunk and put '
            "the include back (see _recover_susfs_setuid_hook_c)."
        )
        return True

    _KBUILD_MOUNT_HIDE_OBJ = 'kernelsu-objs += feature/mount_hide.o\n'
    _MOUNT_HIDE_DECLS = (
        'void ksu_mount_hide_init(void);\n'
        'void ksu_mount_hide_exit(void);\n'
    )

    def _disable_susfs_mount_hide(self, ksu_dir) -> bool:
        # SukiSU-Ultra's mount view hiding (cf41f112) cannot be built in a
        # SUSFS build: feature/mount_hide.c swaps the .open slots of the
        # /proc/<pid>/mounts* file_operations with ksu_patch_text(), which
        # lives in hook/<arch>/patch_memory.c - an object the
        # 10_enable_susfs_for_ksu.patch Kbuild hunk drops. mount_hide.o
        # would fail to link (undefined ksu_patch_text). SUSFS hides
        # mounts its own way (sus_mount + umount at zygote setresuid).
        #
        # So: drop mount_hide.o from kernel/Kbuild and turn the two
        # functions in feature/mount_hide.h (only setuid_hook.c calls
        # them) into empty static inline stubs. The feature handler is
        # then never registered, so the manager sees "Hide suspicious
        # mounts" as unsupported - which is the truth. Runs on every
        # SUSFS build. No mount_hide in Kbuild (older SukiSU) or
        # patch_memory.o still built (it could link then) -> nothing to
        # do. Unknown shape -> False, build hard-fails.
        kbuild = ksu_dir / "kernel/Kbuild"
        header = ksu_dir / "kernel/feature/mount_hide.h"
        if not kbuild.exists():
            return False
        ktext = kbuild.read_text()
        htext = header.read_text() if header.exists() else ''
        if 'KSU_SUSFS_NO_MOUNT_HIDE' in ktext:
            return 'KSU_SUSFS_NO_MOUNT_HIDE' in htext  # already done
        if 'feature/mount_hide.o' not in ktext:
            return True
        if 'patch_memory.o' in ktext:
            return True  # ksu_patch_text is built, mount_hide can link
        if ktext.count(self._KBUILD_MOUNT_HIDE_OBJ) != 1 or ktext.count('feature/mount_hide.o') != 1:
            return False
        if htext.count(self._MOUNT_HIDE_DECLS) != 1:
            return False
        ktext = ktext.replace(
            self._KBUILD_MOUNT_HIDE_OBJ,
            '# KSU_SUSFS_NO_MOUNT_HIDE: feature/mount_hide.o needs ksu_patch_text()\n'
            '# from hook/<arch>/patch_memory.o, which the SUSFS patch drops, see\n'
            '# _disable_susfs_mount_hide in the GKI pipeline\'s kernel_builder.py.\n',
            1,
        )
        htext = htext.replace(
            self._MOUNT_HIDE_DECLS,
            '/* KSU_SUSFS_NO_MOUNT_HIDE: not built in a SUSFS kernel (needs\n'
            ' * hook/<arch>/patch_memory.o, which the SUSFS patch drops). */\n'
            'static inline void ksu_mount_hide_init(void) {}\n'
            'static inline void ksu_mount_hide_exit(void) {}\n',
            1,
        )
        kbuild.write_text(ktext)
        header.write_text(htext)
        logger.info(
            "kernel/Kbuild: SukiSU-Ultra mount view hiding (feature/mount_hide.o) compiled out - "
            "it needs hook/<arch>/patch_memory.o, which the SUSFS patch removes "
            "(see _disable_susfs_mount_hide)."
        )
        return True

    _PKG_OBS_FUNC_TOP = (
        'static void ksu_defer_track_throne(void)\n'
        '{\n'
        '    struct callback_head *cb;\n'
        '\n'
    )
    _PKG_OBS_PRIORITY_BLOCK = (
        '    // Renew the getpriority uid bitmap filter\n'
        '    susfs_parse_priority_app_strings();\n'
        '\n'
    )

    def _fixup_susfs_pkg_observer_c(self, ksu_dir) -> bool:
        # susfs 0b5f967 (2026-10, priority UID filter) adds a call to
        # susfs_parse_priority_app_strings() at the top of
        # ksu_defer_track_throne() in manager/pkg_observer.c. SukiSU-Ultra
        # 4c15fca1 (2026-10-09) rewrote that function's body (goto style),
        # so with --fuzz=3 `patch` drops all context and reports
        # "Hunk #2 succeeded at 42 with fuzz 3" - but the two lines land in
        # the comment block ABOVE the function, outside any function, and
        # the compile fails ("conflicting types for
        # 'susfs_parse_priority_app_strings'"). Fix: take the block out
        # wherever it is and put it right after `struct callback_head *cb;`
        # of ksu_defer_track_throne() - exactly where the hunk puts it on
        # upstream KernelSU. Runs on every SUSFS build. No call in the file
        # (older susfs) or already in place -> nothing to do. Anything
        # else -> False, build stops.
        target = ksu_dir / "kernel/manager/pkg_observer.c"
        if not target.exists():
            return True
        text = target.read_text()
        call = 'susfs_parse_priority_app_strings();'
        if call not in text:
            return True
        top, block = self._PKG_OBS_FUNC_TOP, self._PKG_OBS_PRIORITY_BLOCK
        if text.count(call) != 1 or text.count(block) != 1 or text.count(top) != 1:
            return False
        if '#include <linux/susfs.h>' not in text:
            return False
        if top + block in text:
            return True  # applied in the right place
        text = text.replace(block, '', 1)
        if text.count(top) != 1:
            return False
        text = text.replace(top, top + block, 1)
        if text.count(call) != 1 or (top + block) not in text:
            return False
        target.write_text(text)
        orig = ksu_dir / "kernel/manager/pkg_observer.c.orig"
        if orig.exists():
            orig.unlink()
        logger.info(
            "kernel/manager/pkg_observer.c: the SUSFS patch's susfs_parse_priority_app_strings() "
            "call landed outside ksu_defer_track_throne() (fuzz 3, SukiSU rewrote that function). "
            "Moved it to the top of the function body (see _fixup_susfs_pkg_observer_c)."
        )
        return True

    def _fixup_susfs_sucompat_util_h(self, ksu_dir) -> bool:
        # SukiSU-Ultra 3b607917 (2026-10) moved userspace_stack_buffer()
        # out of feature/sucompat.c into include/util.h (static inline,
        # preinit.c needs it too). 10_enable_susfs_for_ksu.patch, written
        # against upstream KernelSU, removes `#include "util.h"` from
        # sucompat.c, and its own new code (sh_user_path()) calls that
        # helper - so the hunks apply cleanly but sucompat.c fails with
        # "implicit declaration of function 'userspace_stack_buffer'".
        # Fix: put the include back, right after "arch.h" (util.h needs
        # nothing else; checked that none of util.h's names clash with
        # sucompat.c). Runs on every SUSFS build, clean patch or not.
        # Helper still in sucompat.c (older SukiSU) or include already
        # there -> nothing to do. Unknown shape -> False, build stops.
        sucompat = ksu_dir / "kernel/feature/sucompat.c"
        util_h = ksu_dir / "kernel/include/util.h"
        if not sucompat.exists():
            return False
        text = sucompat.read_text()
        if 'userspace_stack_buffer(' not in text:
            return True
        if re.search(r'^static [^\n;]*\buserspace_stack_buffer\(', text, re.M):
            return True  # still defined locally
        if '#include "util.h"' in text:
            return True
        if not util_h.exists() or 'userspace_stack_buffer(' not in util_h.read_text():
            return False
        anchor = '#include "arch.h"\n'
        if text.count(anchor) != 1:
            return False
        sucompat.write_text(text.replace(anchor, anchor + '#include "util.h"\n', 1))
        logger.info(
            'kernel/feature/sucompat.c: re-added #include "util.h" - SukiSU-Ultra moved '
            "userspace_stack_buffer() there and the SUSFS patch drops that include "
            "(see _fixup_susfs_sucompat_util_h)."
        )
        return True

    # SukiSU-Ultra 3b607917 (2026-10) builds a static "preinit runner"
    # into kernelsu and swaps it in for PID 1's selinux_setup execve.
    _KBUILD_PREINIT_IF = 'ifneq ($(wildcard $(KSU_UAPI_INC)/linux/unistd.h),)\n'

    def _susfs_wires_preinit(self) -> bool:
        """True when the susfs kernel-side patch (50_add_susfs_in_gki-*)
        itself wires SukiSU/KernelSU module preinit into fs/exec.c
        (susfs 3ebb8a6+, calls ksu_setup_preinit_file()), so preinit can
        work in a SUSFS build. Older susfs -> False. Also False when this
        object was made without __init__ (check-release.sh's patch test),
        which keeps its verdict exactly as before."""
        susfs_dir = getattr(self, "susfs_dir", None)
        config = getattr(self, "config", None)
        if susfs_dir is None or config is None:
            return False
        try:
            patch = Path(susfs_dir) / "kernel_patches" / config.get_susfs_patch_filename()
            return "ksu_setup_preinit_file" in patch.read_text(errors="surrogateescape")
        except Exception:
            return False

    def _keep_susfs_preinit(self) -> bool:
        """Build SukiSU module preinit in a SUSFS build: only when susfs
        wires it (see _susfs_wires_preinit) and SUSFS_NO_PREINIT=1 is not
        set in the environment (that forces the old preinit-off build,
        e.g. to tell a preinit bootloop from anything else)."""
        if os.environ.get("SUSFS_NO_PREINIT", "").strip() in ("1", "yes", "true"):
            return False
        return self._susfs_wires_preinit()

    def _disable_susfs_preinit(self, ksu_dir) -> bool:
        if self._keep_susfs_preinit():
            logger.info(
                "kernel/Kbuild: SukiSU-Ultra module preinit KEPT - this susfs wires it "
                "into fs/exec.c (set SUSFS_NO_PREINIT=1 to build without it)"
            )
            if hasattr(self, "_mark"):
                self._mark("susfs_preinit", "enabled", "susfs wires preinit via fs/exec.c")
            return True
        # SukiSU-Ultra's module preinit cannot work in a SUSFS build:
        # its only entry point, ksu_preinit_handle_execve(), is called
        # from hook/syscall_event_bridge.c, and preinit.c itself uses
        # ksu_syscall_table from hook/<arch>/syscall_hook.c - both
        # objects are dropped from kernel/Kbuild by
        # 10_enable_susfs_for_ksu.patch (susfs hooks execve its own
        # way). Compiling preinit.o anyway would fail to link (undefined
        # ksu_syscall_table), and the runner would never run.
        #
        # So build it the way SukiSU itself does when the runner can't be
        # built: -DKSU_NO_PREINIT, which turns preinit.h into empty
        # stubs (ksu_preinit_enabled_param() returns false, so the
        # manager/ksud see preinit as unavailable). This also skips
        # compiling the static runner with $(LD). Runs on every SUSFS
        # build, clean patch or recovered. No preinit block in Kbuild
        # (older SukiSU) -> nothing to do. Block present but in an
        # unknown shape -> False, build hard-fails.
        kbuild = ksu_dir / "kernel/Kbuild"
        if not kbuild.exists():
            return False
        text = kbuild.read_text()
        if 'preinit/preinit.o' not in text:
            return True
        if 'KSU_SUSFS_NO_PREINIT' in text:
            return True  # already done
        if text.count(self._KBUILD_PREINIT_IF) != 1:
            return False
        text = text.replace(
            self._KBUILD_PREINIT_IF,
            '# KSU_SUSFS_NO_PREINIT: the SUSFS patch drops the syscall hooks preinit\n'
            '# needs (hook/syscall_event_bridge.o, hook/*/syscall_hook.o), see\n'
            '# _disable_susfs_preinit in the GKI pipeline\'s kernel_builder.py.\n'
            'ifeq (1,1)\n'
            'ccflags-y += -DKSU_NO_PREINIT\n'
            'else ' + self._KBUILD_PREINIT_IF,
            1,
        )
        kbuild.write_text(text)
        logger.info(
            "kernel/Kbuild: SukiSU-Ultra module preinit compiled out (-DKSU_NO_PREINIT) - "
            "it needs the syscall hooks the SUSFS patch removes (see _disable_susfs_preinit)."
        )
        return True

    # Lines in a rejected init.c hunk that are statements/includes we can
    # check for. Anything else (blank lines, braces, comments, #endif)
    # is layout, not content.
    _INIT_C_CALL = re.compile(r'^\s*(?:[A-Za-z_]\w*\s*\(.*\)\s*;|#include\s+.+)\s*$')

    # Present in the upstream-KernelSU context of hunk #7 but not in
    # SukiSU-Ultra: SukiSU installs the module blacklist at second stage
    # init (ksu_module_blacklist_apply() in ksud_integration.c, see
    # _recover_susfs_ksud_integration_c) and has no _init() for it.
    _INIT_C_UPSTREAM_ONLY = {'ksu_module_blacklist_init();'}

    @staticmethod
    def _rej_hunk_count(rej_text: str) -> int:
        return sum(1 for l in rej_text.splitlines() if l.startswith('@@'))

    @classmethod
    def _check_init_c_against_rej(cls, rej_text: str, before: str, after: str) -> list:
        # Compares the recovered file with what the rejected hunks asked
        # for, one statement/include at a time, by COUNT (the same line
        # can legitimately exist more than once - e.g. the pr_alert
        # banner lines are shared by the x86 check hunk #5 removes and
        # the CONFIG_KSU_DEBUG banner that stays):
        #   after == before - (times the hunks remove it) + (times they add it)
        # `before` is init.c as `patch` left it (its good hunks applied,
        # the rejected ones not). A hunk that MOVES a call (removes it
        # in one place, adds it in another) balances out. Returns a list
        # of problems; empty = OK.
        if not rej_text:
            return []
        added, removed = {}, {}
        for line in rej_text.splitlines():
            if line.startswith(('+++', '---')) or line[:1] not in '+-':
                continue
            body = line[1:].strip()
            if not cls._INIT_C_CALL.match(body) or body in cls._INIT_C_UPSTREAM_ONLY:
                continue
            bucket = added if line[0] == '+' else removed
            bucket[body] = bucket.get(body, 0) + 1

        def count(text, body):
            return sum(1 for l in text.splitlines() if l.strip() == body)

        problems = []
        for body in sorted(set(added) | set(removed)):
            want = max(count(before, body) - removed.get(body, 0), 0) + added.get(body, 0)
            have = count(after, body)
            if have != want:
                problems.append(
                    f"{body!r}: the rejected hunks expect it {want} time(s), "
                    f"the recovered file has it {have} time(s)"
                )
        return problems

    @classmethod
    def _check_init_c_untouched_calls(cls, rej_text: str, before: str, after: str) -> list:
        # Every call/#include in init.c (as `patch` left it) that the
        # rejected hunks neither add nor remove, and that isn't inside
        # the `if (ksu_late_loaded) { ... } else { ... }` block (which
        # the recovery replaces on purpose), must appear the same number
        # of times after recovery. Catches SukiSU-only calls that a
        # fixed-string edit would otherwise drop without a word.
        touched = set()
        for line in rej_text.splitlines():
            if line.startswith(('+++', '---')) or line[:1] not in '+-':
                continue
            touched.add(line[1:].strip())
        m = re.search(
            r'    if \(ksu_late_loaded\) \{.*?\n\nvoid __exit kernelsu_exit\(void\)\n\{\n',
            before, re.DOTALL,
        )
        outside = before if not m else before[:m.start()] + before[m.end():]

        def count(text, body):
            return sum(1 for l in text.splitlines() if l.strip() == body)

        problems = []
        seen = set()
        for l in outside.splitlines():
            body = l.strip()
            if body in seen or body in touched or not cls._INIT_C_CALL.match(body):
                continue
            seen.add(body)
            want, have = count(outside, body), count(after, body)
            if have != want:
                problems.append(
                    f"{body!r}: SukiSU has it {want} time(s) outside the late-load "
                    f"block, the recovered file has it {have} time(s)"
                )
        return problems

    @staticmethod
    def _replace_once(content: str, old: str, new: str) -> str:
        if content.count(old) != 1:
            raise ValueError(f"expected exactly one occurrence of {old!r}, found {content.count(old)}")
        return content.replace(old, new, 1)

    def apply_sukisu_patches(self):
        logger.info("=== Applying SukiSU patches ===")
        self._chdir(self.work_dir / "common")
        if not self.config.use_hide_stuff:
            self._mark("sukisu_hide_stuff", "skipped", "disabled via --no-hide-stuff")
            return
        hooks_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if not hooks_patch.exists():
            self._mark("sukisu_hide_stuff", "skipped", "69_hide_stuff.patch not found")
            return
        result = self._run_cmd(f"cp {hooks_patch} . && patch -p1 -F 3 < 69_hide_stuff.patch", check=False)
        self._mark("sukisu_hide_stuff", "applied" if result.returncode == 0 else "failed")

    def apply_zram_patches(self):
        if not self.config.use_zram:
            self._mark("zram_lz4kd", "skipped", "not requested")
            return
        logger.info("=== Applying ZRAM (LZ4KD) patches ===")
        self._chdir(self.work_dir / "common")
        for src in [
            (self.sukisu_patch_dir / "other/zram/lz4k/include/linux", "include/linux/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/lib", "lib/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/crypto", "crypto/"),
        ]:
            if src[0].exists():
                self._run_cmd(
                    f"find {src[0]} -mindepth 1 -maxdepth 1 ! -name Kconfig ! -name Makefile -exec cp -r {{}} {src[1]} \\;",
                    check=False,
                )
        oplus_src = self.sukisu_patch_dir / "other/zram/lz4k_oplus"
        if oplus_src.exists():
            self._run_cmd("mkdir -p lib/lz4k_oplus", check=False)
            self._run_cmd(f"cp -r {oplus_src}/* lib/lz4k_oplus/", check=False)
        zram_patch_dir = self.sukisu_patch_dir / f"other/zram/zram_patch/{self.config.kernel_version}"
        common_dir = self.work_dir / "common"
        zram_ok = True
        zram_found_any = False
        details = []
        for patch in ["lz4kd.patch", "lz4k_oplus.patch"]:
            p = zram_patch_dir / patch
            if p.exists():
                zram_found_any = True
                ok, detail = self._apply_patch_with_dry_run(p, common_dir)
                details.append(detail)
                if not ok:
                    logger.warning(f"ZRAM patch not applied ({detail}) - tree left untouched")
                    zram_ok = False
        if not zram_found_any:
            self._mark("zram_lz4kd", "failed", "no zram patch files found for this kernel version")
        else:
            self._mark("zram_lz4kd", "applied" if zram_ok else "failed", "; ".join(details))

    def apply_task_mmu_fixes(self):
        logger.info("=== Applying task_mmu.c fixes ===")
        self._chdir(self.work_dir / "common")
        task_mmu = Path("fs/proc/task_mmu.c")
        if not task_mmu.exists():
            self._mark("task_mmu_fixes", "skipped", "fs/proc/task_mmu.c not found")
            return

        changed = False
        fb = f"{self.config.android_version}-{self.config.kernel_version}"
        with open(task_mmu, "r") as f:
            content = f.read()

        if fb == "android15-6.6" and "unsigned int nr_subpages" not in content:
            self._fix_base_c_header()
            changed = True
        elif fb == "android14-6.1" and "if (!vma_pages(vma))" not in content:
            self._fix_base_c_header()
            changed = True
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)
        elif fb in ["android12-5.10", "android13-5.10", "android13-5.15"] and "if (!vma_pages(vma))" not in content:
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)
                changed = True

        with open(task_mmu, "r") as f:
            content = f.read()
        if "struct dentry *dentry;\n" in content:
            content = content.replace("struct dentry *dentry;\n", "struct dentry *dentry = NULL;\n")
            with open(task_mmu, "w") as f:
                f.write(content)
            changed = True

        self._mark("task_mmu_fixes", "applied" if changed else "skipped",
                    "" if changed else "not needed on this branch")

    def _fix_base_c_header(self):
        base_c = self.work_dir / "common/fs/proc/base.c"
        if not base_c.exists():
            return
        with open(base_c, "r") as f:
            content = f.read()
        if "#include <linux/dma-buf.h>" not in content:
            content = content.replace("#include <linux/cpufreq_times.h>",
                                    "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>")
            with open(base_c, "w") as f:
                f.write(content)


    # ---------------------------------------------------------------
    # ath9k_htc - TP-Link TL-WN722N v1 (AR9271) over OTG
    #
    # An earlier attempt at this set CONFIG_CFG80211=y/CONFIG_MAC80211=y
    # and killed the phone's built-in WiFi: GKI ships NO wireless stack
    # (gki_defconfig defines neither symbol; only net/wireless/wext-*.o
    # gets compiled), the real stack is Qualcomm's cfg80211.ko/mac80211.ko
    # in /vendor/lib/modules, and a builtin copy puts a second symbol
    # surface next to theirs so qca_cld3/kiwi_v2 stops loading.
    #
    # =m is a different thing entirely and is what this does. Verified
    # against the android13-5.15 source: the ONLY place in core net code
    # that tests CONFIG_CFG80211 is net/core/net-sysfs.c, under
    #   #if IS_ENABLED(CONFIG_WIRELESS_EXT) || IS_ENABLED(CONFIG_CFG80211)
    # and CONFIG_WIRELESS_EXT=y already, so that block is already active.
    # net/core/{dev,rtnetlink,dev_ioctl}.c and net/socket.c: zero hits.
    # include/linux/netdevice.h keeps ieee80211_ptr unconditionally
    # (Google dropped upstream's IS_ENABLED guard for KMI stability) -
    # which is also why the vendor stack works at all on an Image built
    # without CFG80211. So vmlinux does not change; only new .ko files
    # appear, and we ship four of them.
    #
    # cfg80211.ko and mac80211.ko ARE built here (ath9k needs something
    # to link against) and are deliberately NOT collected. Shipping them
    # would recreate the original failure one level up.
    ATH9K_MODULES = ("ath.ko", "ath9k_hw.ko", "ath9k_common.ko", "ath9k_htc.ko")
    ATH9K_NEVER_SHIP = ("cfg80211.ko", "mac80211.ko")

    def _ath9k_dir(self) -> Path:
        return Path(__file__).parent / "ath9k"

    def _ath9k_fragment_lines(self) -> list:
        """The defconfig fragment is the single source of truth: it is
        what gets appended to gki_defconfig AND what the post-build
        effective-config check verifies, so the two cannot drift."""
        frag = self._ath9k_dir() / "ath9k-modules.fragment"
        if not frag.exists():
            return []
        return [l.strip() for l in frag.read_text().splitlines() if l.strip()]

    def apply_ath9k_patches(self):
        """Removes ath9k_htc's forced `select MAC80211_LEDS`.

        ATH9K_HTC does `select MAC80211_LEDS if LEDS_CLASS=y` and GKI has
        LEDS_CLASS=y (via CONFIG_LEDS_CLASS_FLASH=y), so the option cannot
        be turned off from a defconfig fragment - Kconfig re-selects it.
        With it on, the inlines in include/net/mac80211.h reference
        __ieee80211_create_tpt_led_trigger / __ieee80211_get_radio_led_name,
        and the device's vendor mac80211.ko exports neither (checked: zero
        LED symbols in the .ko pulled off the phone). The module would
        build fine and then fail at insmod with unknown symbols.

        Hard failure rather than soft: without this patch the build
        produces a driver that cannot load, and the whole point of the
        feature is a driver that loads.
        """
        if not self.config.use_ath9k:
            self._mark("ath9k", "skipped", "not requested")
            return
        logger.info("=== ath9k_htc: preparing out-of-tree wireless modules ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            self._mark("ath9k", "failed", "common/ missing")
            raise RuntimeError("ath9k requested but common/ does not exist")

        patch_name = (f"ath9k_htc_no_mac80211_leds_"
                      f"{self.config.android_version}-{self.config.kernel_version}.patch")
        patch_file = Path(__file__).parent / "patches" / patch_name
        if not patch_file.exists():
            self._mark("ath9k", "failed", f"no patch for this branch ({patch_name})")
            raise RuntimeError(
                f"ath9k requested but {patch_name} does not exist. The "
                f"MAC80211_LEDS select is branch-specific and must be "
                f"removed, or the resulting ath9k_htc.ko will not load."
            )

        ok, detail = self._apply_patch_with_dry_run(patch_file, common_dir)
        if not ok:
            self._mark("ath9k", "failed", detail)
            raise RuntimeError(
                f"ath9k Kconfig patch did not apply ({detail}). Refusing to "
                f"build ath9k_htc with MAC80211_LEDS forced on - it would "
                f"produce a module that fails at insmod with unknown symbols."
            )
        logger.info(f"ath9k: MAC80211_LEDS select removed ({detail})")
        self._mark("ath9k", "applied", detail)

    def _amneziawg_applied(self) -> bool:
        return self.patch_status.get("amneziawg", {}).get("status") == "applied"

    def _fetch_amneziawg(self, ref: str) -> Path:
        """The module repo at exactly `ref`, in <workspace>/amneziawg-<sha12>/,
        fetched once per pin and reused (same pattern as the KPM_NEXT kptools)."""
        repo = self.workspace / f"amneziawg-{ref[:12]}"

        def head() -> str:
            r = self._run_cmd(f"git -C {repo} rev-parse HEAD", check=False,
                              capture_output=True)
            return (r.stdout or "").strip() if r.returncode == 0 else ""

        if repo.exists() and head() == ref and (repo / "src/Kbuild").is_file():
            logger.info(f"AmneziaWG: reusing {repo}")
            return repo
        if repo.exists():
            shutil.rmtree(repo)
        repo.mkdir(parents=True)
        r = self._run_cmd(
            f"git -C {repo} init -q && "
            f"git -C {repo} fetch -q --depth 1 {AMNEZIAWG_REPO} {ref} && "
            f"git -C {repo} checkout -q FETCH_HEAD", check=False)
        if r.returncode != 0 or head() != ref:
            raise RuntimeError(
                f"AmneziaWG: could not fetch amneziawg-linux-kernel-module "
                f"{ref[:12]}. Stopping (build without it: --no-amneziawg).")
        return repo

    def add_amneziawg(self):
        """AmneziaWG built into the Image (CONFIG_AMNEZIAWG=y) next to the
        stock WireGuard, which stays exactly as it is.

        The upstream out-of-tree module is copied into
        drivers/net/amneziawg/ and made in-tree ready by
        prepare_amneziawg_src() (Android GKI compat + wg_ -> awg_ rename,
        see there). drivers/net/Kconfig and Makefile get one line each.

        Hard failure on anything unexpected: AmneziaWG is on by default,
        and a kernel that silently lacks it is the failure mode the
        post-build checks exist for. --no-amneziawg builds without it.
        """
        if not self.config.use_amneziawg:
            self._mark("amneziawg", "skipped", "not requested")
            return
        branch = (self.config.android_version, self.config.kernel_version)
        if branch not in AMNEZIAWG_SUPPORTED:
            logger.warning(
                f"AmneziaWG: not supported on {branch[0]}-{branch[1]} yet "
                f"(only tested on android13-5.15) - building without it")
            self._mark("amneziawg", "skipped",
                       f"not supported on {branch[0]}-{branch[1]} yet")
            return
        logger.info("=== AmneziaWG: adding the built-in driver ===")
        ref = (self.config.amneziawg_commit or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            self._mark("amneziawg", "failed", f"not a full commit SHA: {ref!r}")
            raise RuntimeError(
                f"AmneziaWG: '{ref}' is not a full 40-character commit SHA. "
                f"The module is only ever built from a pinned commit "
                f"(DEFAULT_AMNEZIAWG_REF in config.py, or --amneziawg-commit).")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            self._mark("amneziawg", "failed", "common/ missing")
            raise RuntimeError("AmneziaWG requested but common/ does not exist")

        repo = self._fetch_amneziawg(ref)
        dest = common_dir / "drivers/net/amneziawg"
        try:
            version = prepare_amneziawg_src(repo / "src", dest)
        except AmneziaWGPrepareError as e:
            self._mark("amneziawg", "failed", str(e))
            raise RuntimeError(
                f"AmneziaWG {ref[:12]}: {e}. Upstream changed in a way the "
                f"integration does not know yet. Stopping (build without it: "
                f"--no-amneziawg, or with the last known-good pin).")

        # (file, line we add, anchor it goes next to, before/after)
        wiring = (
            ("drivers/net/Kconfig", 'source "drivers/net/amneziawg/Kconfig"\n',
             "\nconfig WIREGUARD_DEBUG\n", "before"),
            ("drivers/net/Makefile", "obj-$(CONFIG_AMNEZIAWG) += amneziawg/\n",
             "\nobj-$(CONFIG_WIREGUARD) += wireguard/\n", "after"),
        )
        for rel, line, anchor, where in wiring:
            f = common_dir / rel
            text = f.read_text()
            have = text.count(line)
            if have == 1:
                logger.info(f"AmneziaWG: {rel} already wired")
                continue
            if have != 0 or text.count(anchor) != 1:
                self._mark("amneziawg", "failed", f"{rel}: unknown shape")
                raise RuntimeError(
                    f"AmneziaWG: {rel} is not in the expected shape "
                    f"({have} existing line(s), anchor {anchor.strip()!r} "
                    f"found {text.count(anchor)} time(s), expected 1). Stopping.")
            if where == "before":
                text = text.replace(anchor, "\n" + line + anchor, 1)
            else:
                text = text.replace(anchor, anchor + line, 1)
            f.write_text(text)

        self._amneziawg_version = version
        detail = f"AmneziaWG {version} @ {ref[:12]} (built-in, wg_ -> awg_)"
        logger.info(f"AmneziaWG: {detail}")
        self._mark("amneziawg", "applied", detail)

    def collect_ath9k_modules(self) -> list:
        """Collects the four ath9k .ko files and verifies their symbol
        CRCs against the device's vendor wireless stack BEFORE anything
        gets flashed.

        A module whose __versions CRCs disagree with the running stack
        fails at insmod ("disagrees about version of symbol"). That is a
        harmless failure - but finding out on the phone costs a flash
        cycle, and the check costs milliseconds here.
        """
        if not self.config.use_ath9k:
            return []
        logger.info("=== ath9k_htc: collecting modules ===")
        out_dir = self.work_dir / "out"
        if not out_dir.exists():
            raise RuntimeError(f"ath9k: {out_dir} does not exist - nothing was built")

        def _pick(name: str):
            # Prefer a copy from a modules-install staging tree: those are
            # the stripped and (with CONFIG_MODULE_SIG_ALL=y) signed ones.
            cands = list(out_dir.rglob(name))
            cands.sort(key=lambda p: (0 if "staging" in p.parts else 1, len(p.parts)))
            return cands[0] if cands else None

        found, missing = {}, []
        for name in self.ATH9K_MODULES:
            hit = _pick(name)
            (found.setdefault(name, hit) if hit else missing.append(name))
        if missing:
            self._mark("ath9k_modules", "failed", f"not built: {', '.join(missing)}")
            raise RuntimeError(
                f"ath9k: these modules were requested but never built: "
                f"{', '.join(missing)}. The build reported success, so this "
                f"is the silent-config failure mode - check whether Kconfig "
                f"kept CONFIG_ATH9K_HTC=m in the built .config."
            )

        dest = self.work_dir / "ath9k-modules"
        dest.mkdir(exist_ok=True)
        artifacts = []
        for name, src in found.items():
            target = dest / name
            self._run_cmd(f"cp {src} {target}", check=False)
            logger.info(f"  {name}  <- {src}")
            artifacts.append(str(target))

        for name in self.ATH9K_NEVER_SHIP:
            if _pick(name):
                logger.info(f"  {name} was built and is deliberately NOT packaged - "
                            f"the device loads Qualcomm's from /vendor/lib/modules")

        # Kept next to the modules on purpose: diffing this against the
        # previous release's copy is what proves the Image's exported
        # symbol surface did not move, i.e. that internal WiFi cannot
        # break the way it did on the first attempt.
        symvers = sorted(out_dir.rglob("Module.symvers"), key=lambda p: len(p.parts))
        if not symvers:
            raise RuntimeError("ath9k: no Module.symvers under out/ - cannot verify CRCs")
        symvers = symvers[0]
        self._run_cmd(f"cp {symvers} {dest}/Module.symvers", check=False)
        artifacts.append(str(dest / "Module.symvers"))

        # Marking happens here, where `found` actually exists. An earlier
        # version marked inside _verify_ath9k_symbols() and referenced
        # `found` from the caller's scope - a NameError that only fired
        # after a full 15-minute build had already succeeded.
        drift = self._verify_ath9k_symbols(dest, symvers)
        self._mark(
            "ath9k_modules", "applied",
            f"{len(found)} modules, "
            + ("CRC drift only (not enforced on this kernel)" if drift
               else "CRCs match vendor stack"))
        return artifacts

    def _verify_ath9k_symbols(self, dest: Path, symvers: Path) -> bool:
        """Returns True if there was CRC drift (non-fatal), False if clean.
        Raises on unresolved symbols."""
        script = self._ath9k_dir() / "verify-ath9k-symbols.py"
        table = self._ath9k_dir() / "vendor-symbols-crc.txt"
        if not script.exists() or not table.exists():
            raise RuntimeError(
                f"ath9k: {script.name} / {table.name} missing - refusing to "
                f"report modules as verified when nothing verified them."
            )
        kos = " ".join(str(dest / n) for n in self.ATH9K_MODULES)
        result = self._run_cmd(
            f"python3 {script} --symvers {symvers} --vendor-crc {table} {kos}",
            check=False, capture_output=True)
        for line in (result.stdout or "").splitlines():
            logger.info(f"  {line}")
        # Exit 2 = CRC drift only. Verified on the device: this kernel does
        # NOT enforce modversions. The vendor's own cfg80211.ko/mac80211.ko
        # are loaded and working with 54 and 130 mismatched CRCs
        # respectively, and our ath.ko loaded with three. The drift is 133
        # LTS releases of declaration churn between 5.15.78 and 5.15.211,
        # not a layout change - the vendor stack passes sk_buff and
        # net_device to this vmlinux constantly and WiFi works. So this is
        # a warning, not a reason to throw away a good build.
        #
        # Exit 1 is still fatal: a symbol nobody provides means "Unknown
        # symbol" at insmod and the module does not load at all.
        if result.returncode == 2:
            logger.warning(
                "ath9k: symbol CRCs differ from the vendor stack (listed "
                "above). Expected on this kernel - modversions is not "
                "enforced here. The modules will still load."
            )
            return True
        if result.returncode != 0:
            self._mark("ath9k_modules", "failed", "unresolved symbols")
            raise RuntimeError(
                "ath9k: the built modules reference symbols nothing "
                "provides (see the list above). insmod would fail with "
                "'Unknown symbol'. Do not ship these modules."
            )
        return False

    def configure_kernel(self):
        logger.info("=== Configuring kernel ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning(f"Config file does not exist: {config_file}")
            return

        with open(config_file, "a") as f:
            f.write(self.KERNEL_CONFIG_TEMPLATE)
            if self.config.kernel_version != "6.6":
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=y\n")
            else:
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=n\n")
            # FUSE-BPF: only exists from android13 onwards. Writing it on
            # android12-5.10 would be a reference to an undefined Kconfig
            # symbol, which Kconfig drops in silence - the exact failure
            # mode that hid CONFIG_KSU_SUSFS never actually being defined
            # for a long time. Keep undefined symbols out of the
            # defconfig entirely rather than relying on them being
            # harmless.
            if self._has_fuse_bpf():
                f.write("# === FUSE-BPF (android13+; fs/fuse/backing.c) ===\n")
                f.write("CONFIG_FUSE_BPF=y\n")
            else:
                logger.info(
                    f"Skipping CONFIG_FUSE_BPF - not available on "
                    f"{self.config.android_version}-{self.config.kernel_version} "
                    f"(fs/fuse/backing.c does not exist on this branch)"
                )
            if self.config.use_extra_net:
                logger.info("Adding extra networking configs (--extra-net): "
                            "IPv6 NAT, nftables, extra ipset types")
                f.write(self._extra_net_config_text())

        if self.config.use_ath9k:
            # Appended verbatim, comments and all: the file explains why
            # each value is what it is, and those reasons belong next to
            # the values in the defconfig too.
            logger.info("Adding ath9k_htc module configs (--ath9k)")
            with open(config_file, "a") as f:
                f.write("\n" + (self._ath9k_dir() / "ath9k-modules.fragment").read_text())

        if self._amneziawg_applied():
            # Only when add_amneziawg() really put the driver in the tree:
            # an undefined symbol in the defconfig is dropped in silence.
            logger.info("Adding AmneziaWG config (built in)")
            with open(config_file, "a") as f:
                f.write("# === AmneziaWG (WireGuard with DPI obfuscation), built in next to WireGuard ===\n")
                f.write("CONFIG_AMNEZIAWG=y\n")
                f.write("# CONFIG_AMNEZIAWG_DEBUG is not set\n")

        if self.config.use_zram:
            self._configure_zram()
            self._configure_bazel()

        if self.config.use_mglru:
            with open(config_file, "a") as f:
                f.write("# === MGLRU (Multi-Gen LRU) Config ===\n")
                f.write("CONFIG_LRU_GEN=y\n")
                f.write("CONFIG_LRU_GEN_ENABLED=y\n")

        if self.config.use_psi:
            with open(config_file, "a") as f:
                f.write("# === PSI (Pressure Stall Information) Config ===\n")
                f.write("CONFIG_PSI=y\n")

        if self.config.use_ntsync:
            with open(config_file, "a") as f:
                f.write("# === NTSync Config (NT sync primitives, e.g. for Winlator/Wine) ===\n")
                f.write("CONFIG_NTSYNC=y\n")

        if self.config.bbr_version == "bbr1":
            with open(config_file, "a") as f:
                f.write("CONFIG_DEFAULT_BBR=y\n")
        elif self.config.bbr_version == "bbr3":
            if self._bbrv3_applied:
                with open(config_file, "a") as f:
                    f.write("CONFIG_TCP_CONG_BBR3=y\n")
                    f.write("CONFIG_DEFAULT_BBR3=y\n")
            else:
                # apply_bbrv3_patches() already logged/marked why this
                # didn't land - CONFIG_TCP_CONG_BBR3 simply doesn't
                # exist in Kconfig without the patch, so setting
                # CONFIG_DEFAULT_BBR3=y here would just be silently
                # ignored by the build and leave the kernel on cubic
                # with no obvious explanation. Fall back to bbr1
                # instead, which is always available, so the person
                # gets a working (if not the requested) congestion
                # control rather than a silent, unexplained cubic.
                logger.warning(
                    "BBRv3 was requested but did not apply - falling "
                    "back to BBRv1 as the system default congestion "
                    "control instead of silently leaving it on cubic."
                )
                with open(config_file, "a") as f:
                    f.write("CONFIG_DEFAULT_BBR=y\n")

        build_config = self.work_dir / "common/build.config.gki"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("check_defconfig", "")
            with open(build_config, "w") as f:
                f.write(content)

    def _configure_zram(self):
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "r") as f:
            content = f.read()
        kv = self.config.kernel_version
        if kv == "5.10":
            with open(config_file, "a") as f:
                f.write(self.ZRAM_CONFIG_5_10)
        else:
            content = content.replace("CONFIG_ZRAM=m", "CONFIG_ZRAM=y")
            with open(config_file, "w") as f:
                f.write(content)
            with open(config_file, "a") as f:
                f.write("CONFIG_ZSMALLOC=y\n")
        with open(config_file, "a") as f:
            f.write(self.ZRAM_CONFIG_COMMON)

    def _configure_bazel(self):
        modules_bzl = self.work_dir / "common/modules.bzl"
        if modules_bzl.exists():
            with open(modules_bzl, "r") as f:
                content = f.read()
            modified = False
            for old in ['"drivers/block/zram/zram.ko",\n', '"drivers/block/zram/zram.ko",',
                       '"mm/zsmalloc.ko",\n', '"mm/zsmalloc.ko",']:
                if old in content:
                    content = content.replace(old, '')
                    modified = True
            if modified:
                with open(modules_bzl, "w") as f:
                    f.write(content)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "a") as f:
            f.write("CONFIG_MODULE_SIG_FORCE=n\n")

    def configure_kernel_name(self):
        logger.info("=== Configuring kernel name ===")
        self._chdir(self.work_dir)
        MAX_CUSTOM_LEN = 48
        safe_custom_version = ""
        if self.config.custom_version:
            safe_custom_version = self.config.custom_version.rstrip('-')[:MAX_CUSTOM_LEN]

        setlocalversion = self.work_dir / "common/scripts/setlocalversion"
        if setlocalversion.exists():
            with open(setlocalversion, "r") as f:
                content = f.read()
            if safe_custom_version:
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if 'echo "$res"' in line and not line.strip().startswith('#'):
                        lines[i] = f'\techo "{safe_custom_version}$res"'
                        break
                with open(setlocalversion, "w") as f:
                    f.write('\n'.join(lines))
            if "-dirty" in content:
                content = content.replace("-dirty", "")
                with open(setlocalversion, "w") as f:
                    f.write(content)

        import datetime
        current_time = datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y")
        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            with open(mkcompile_h, "r") as f:
                content = f.read()
            content = content.replace('UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                                    f'UTS_VERSION="#1 SMP PREEMPT {current_time}"')
            with open(mkcompile_h, "w") as f:
                f.write(content)

        if self.config.kernel_version in ["6.1", "6.6"]:
            init_makefile = self.work_dir / "common/init/Makefile"
            if init_makefile.exists():
                with open(init_makefile, "r") as f:
                    content = f.read()
                content = content.replace('$(preempt-flag-y) "$(build-timestamp)"', f'$(preempt-flag-y) "{current_time}"')
                with open(init_makefile, "w") as f:
                    f.write(content)

        if not (self.work_dir / "build/build.sh").exists():
            bazel_build = self.work_dir / "common/BUILD.bazel"
            if bazel_build.exists():
                with open(bazel_build, "r") as f:
                    content = f.read()
                lines = [l for l in content.split('\n') if '"protected_exports_list"' not in l or 'android/abi_gki_protected_exports_aarch64' not in l]
                with open(bazel_build, "w") as f:
                    f.write('\n'.join(lines))

            abi_path = self.work_dir / "common/android/abi_gki_protected_exports_aarch64"
            if abi_path.exists():
                import shutil
                try:
                    if abi_path.is_dir():
                        shutil.rmtree(abi_path)
                    else:
                        abi_path.unlink()
                except Exception:
                    pass

            stamp_bzl = self.work_dir / "build/kernel/kleaf/impl/stamp.bzl"
            if stamp_bzl.exists():
                with open(stamp_bzl, "r") as f:
                    content = f.read()
                content = content.replace("-maybe-dirty", "")
                with open(stamp_bzl, "w") as f:
                    f.write(content)

            if self.config.custom_version:
                config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
                if config_file.exists():
                    with open(config_file, "r") as f:
                        content = f.read()
                    content = re.sub(r'^CONFIG_LOCALVERSION=".*"$', f'CONFIG_LOCALVERSION="{self.config.custom_version}"', content, flags=re.MULTILINE)
                    with open(config_file, "w") as f:
                        f.write(content)
                else:
                    logger.warning(f"Config file does not exist, skipping custom_version setting: {config_file}")

    def show_kernel_config(self):
        """Prints what this pipeline REQUESTED, by reading gki_defconfig -
        the input file configure_kernel() appends to. It is not proof any
        of it took effect: Kconfig drops undefined symbols and unmet
        dependencies without a word, so a line can appear here and be
        absent from the kernel that gets built. _verify_effective_config()
        is the check that reads the produced .config after the build; this
        is only a pre-build echo of the intent."""
        logger.info("=== Requested kernel config (from gki_defconfig, pre-build) ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        
        if not config_file.exists():
            logger.warning(f"Config file does not exist: {config_file}")
            return
        
        with open(config_file, "r") as f:
            lines = f.readlines()
        
        config_lines = [line.strip() for line in lines if line.strip().startswith("CONFIG_")]
        
        key_configs = {
            "CONFIG_KSU": "KernelSU",
            "CONFIG_KPM": "KPM",
            "CONFIG_KSU_SUSFS": "SUSFS",
            "CONFIG_BBG": "Baseband-guard",
            "BBR": "BBR",  # substring match: real symbols are CONFIG_TCP_CONG_BBR
                            # and CONFIG_DEFAULT_BBR - neither actually starts
                            # with "CONFIG_BBR", so this can't use a prefix match
            "CONFIG_ZRAM": "ZRAM",
            "BFQ": "BFQ I/O Scheduler",
            "F2FS_FS_": "F2FS Compression",
        }
        
        logger.info("Key config status (requested, not yet verified):")
        for prefix, name in key_configs.items():
            found = [c for c in config_lines if prefix in c]
            if found:
                status = "requested"
            else:
                status = "not configured"
            logger.info(f"  [{status}] {name}")
            if found:
                for f in sorted(found):
                    logger.info(f"      -> {f}")
        
        # Show ZRAM related config
        if self.config.use_zram:
            zram_configs = [c for c in config_lines if any(x in c for x in ["ZRAM", "ZSMALLOC", "LZ4", "LZ4KD", "CRYPTO_LZ4", "MODULE_SIG"])]
            if zram_configs:
                logger.info("ZRAM related config:")
                for zc in sorted(zram_configs):
                    logger.info(f"  -> {zc}")
        
        logger.info("-" * 60)

    def _disable_bazel_check_defconfig(self):
        """Turns off Kleaf's defconfig equality check at its real source.

        We already strip the string 'check_defconfig' out of
        common/build.config.gki - but that only does anything on the
        LEGACY build.sh path, where check_defconfig is a literal command
        in that file. Under Bazel/Kleaf the check is reached differently
        (via POST_DEFCONFIG_CMDS, and/or as an attribute on the
        kernel_build rule in common/BUILD.bazel), so that strip is a
        no-op on exactly the branches (android14+) where the check keeps
        failing.

        Kleaf requires gki_defconfig to be in canonical
        `make savedefconfig` form. _canonicalize_defconfig() tries to
        satisfy that by regenerating it with the HOST toolchain, but the
        host clang is not Kleaf's hermetic clang, so the canonical form
        the two produce can legitimately differ - and then the build
        fails with 'savedefconfig does not match ...' even though
        nothing is actually wrong with our CONFIG_ additions.

        Two mechanisms are applied here, because which one works depends
        on the branch:

        1. POST_DEFCONFIG_CMDS=true in common/build.config.gki.aarch64.
           Kleaf still consumes build.config on android14-6.1 and
           android15-6.6, and the defconfig check runs as part of
           POST_DEFCONFIG_CMDS - so replacing the whole variable with a
           no-op kills it cleanly, without touching Bazel files at all.
           Note this targets build.config.gki.aarch64, NOT
           build.config.gki: .aarch64 sources .gki, so setting it at the
           end of .aarch64 is what actually wins. Our existing strip of
           the literal string 'check_defconfig' from build.config.gki is
           left alone - it's what the legacy build.sh path needs.

        2. check_defconfig = "disabled" on the kernel_aarch64 target in
           common/BUILD.bazel. Newer Kleaf (android16-6.12 onward) has
           been migrating off build.config, so mechanism 1 may have
           nothing to act on there; this is the attribute-level
           equivalent and is what WildKernels' build-kernel action uses.

        Neither is a no-op if the other already worked - they're
        idempotent and cheap, so both go in.

        Note what this does NOT disable: KMI symbol-list strict mode and
        the symbol-list violation check both stay ON (see build_kernel()
        for why that matters - a build with those off compiled fine and
        bootlooped a real device). This only stops Bazel from insisting
        that our modified gki_defconfig round-trips byte-identically.
        """
        # --- 1. POST_DEFCONFIG_CMDS ---
        gki_aarch64 = self.work_dir / "common/build.config.gki.aarch64"
        if gki_aarch64.exists():
            text = gki_aarch64.read_text()
            lines = text.split("\n")
            replaced = False
            for i, line in enumerate(lines):
                if line.startswith("POST_DEFCONFIG_CMDS="):
                    lines[i] = "POST_DEFCONFIG_CMDS=true"
                    replaced = True
            if replaced:
                gki_aarch64.write_text("\n".join(lines))
                logger.info("Set POST_DEFCONFIG_CMDS=true in build.config.gki.aarch64")
            else:
                if not text.endswith("\n"):
                    text += "\n"
                gki_aarch64.write_text(text + "POST_DEFCONFIG_CMDS=true\n")
                logger.info("Appended POST_DEFCONFIG_CMDS=true to build.config.gki.aarch64")
            self._mark("bazel_post_defconfig_cmds", "applied",
                       "replaced" if replaced else "appended")
        else:
            logger.info(
                "common/build.config.gki.aarch64 not found - branch has likely "
                "migrated off build.config; relying on the BUILD.bazel attribute."
            )
            self._mark("bazel_post_defconfig_cmds", "skipped", "build.config.gki.aarch64 absent")

        # --- 2. BUILD.bazel attribute ---
        bazel_build = self.work_dir / "common/BUILD.bazel"
        if not bazel_build.exists():
            logger.warning("common/BUILD.bazel not found - skipping check_defconfig disable")
            self._mark("bazel_check_defconfig", "skipped", "BUILD.bazel not found")
            return

        content = bazel_build.read_text()
        if 'check_defconfig = "disabled"' in content:
            logger.info("check_defconfig already disabled in BUILD.bazel")
            self._mark("bazel_check_defconfig", "skipped", "already disabled")
            return

        anchor = 'name = "kernel_aarch64",'
        if anchor not in content:
            if self.patch_status.get("bazel_post_defconfig_cmds", {}).get("status") == "applied":
                # Expected on branches where kernel_aarch64 is produced by
                # define_common_kernels() rather than spelled out as a
                # literal target - there is no such line to anchor to.
                # POST_DEFCONFIG_CMDS already neutralised the check, so
                # this is a redundant belt, not a broken one.
                logger.info(
                    "No literal kernel_aarch64 target in common/BUILD.bazel "
                    "(generated target) - POST_DEFCONFIG_CMDS already handled "
                    "the defconfig check, nothing more to do."
                )
                self._mark("bazel_check_defconfig", "skipped",
                           "generated target; POST_DEFCONFIG_CMDS covers it")
                return
            logger.warning(
                "Could not find the kernel_aarch64 target in common/BUILD.bazel "
                "AND POST_DEFCONFIG_CMDS could not be set either - nothing is "
                "suppressing Kleaf's defconfig check. If the build fails with "
                "'savedefconfig does not match', this is the place to look."
            )
            self._mark("bazel_check_defconfig", "failed",
                       "no anchor and no POST_DEFCONFIG_CMDS fallback")
            return

        content = content.replace(
            anchor,
            anchor + '\n    check_defconfig = "disabled",',
            1,
        )
        bazel_build.write_text(content)
        logger.info('Injected check_defconfig = "disabled" into common/BUILD.bazel')
        self._mark("bazel_check_defconfig", "applied", "kernel_aarch64 target")

    def _canonicalize_defconfig(self):
        """Bazel/Kleaf's kernel_config rule strictly requires
        gki_defconfig to be in canonical `make savedefconfig` form
        (minimal, sorted, no lines matching Kconfig defaults) - it fails
        the build with 'savedefconfig does not match ...' otherwise.
        Our custom CONFIG_ additions (KSU, SUSFS, ZRAM, BBR, etc.) are
        appended as plain text, so after every change we regenerate the
        canonical form ourselves using the kernel's own host Kconfig
        tooling (no cross-compiler needed for this step) and write it
        back, so Bazel's check passes."""
        import tempfile
        logger.info("=== Canonicalizing gki_defconfig for Bazel (savedefconfig) ===")
        common_dir = self.work_dir / "common"
        defconfig_path = common_dir / "arch/arm64/configs/gki_defconfig"
        if not defconfig_path.exists():
            logger.warning(f"gki_defconfig not found at {defconfig_path}, skipping canonicalization")
            return

        with tempfile.TemporaryDirectory(prefix="savedefconfig_") as tmpdir:
            self._chdir(common_dir)
            expand = self._run_cmd(f"make ARCH=arm64 O={tmpdir} gki_defconfig", check=False)
            if expand.returncode != 0:
                logger.warning("Failed to expand gki_defconfig for canonicalization, leaving as-is")
                self._chdir(self.work_dir)
                return
            save = self._run_cmd(f"make ARCH=arm64 O={tmpdir} savedefconfig", check=False)
            self._chdir(self.work_dir)
            if save.returncode != 0:
                logger.warning("savedefconfig failed, leaving gki_defconfig as-is")
                return

            canonical = Path(tmpdir) / "defconfig"
            if canonical.exists():
                canonical_content = canonical.read_text()
                defconfig_path.write_text(canonical_content)
                logger.info("gki_defconfig canonicalized successfully")
            else:
                logger.warning("savedefconfig did not produce an output file, leaving gki_defconfig as-is")

    def _write_kasan_choice_fragment(self):
        """Bazel/Kleaf branches (android14+): CONFIG_KASAN_GENERIC /
        CONFIG_KASAN_SW_TAGS / CONFIG_KASAN_HW_TAGS are a Kconfig
        'choice' (radio-button) group. gki_defconfig's own choice
        default resolves to GENERIC unless the other members are
        *explicitly* disabled in the same defconfig layer - just adding
        'CONFIG_KASAN_HW_TAGS=y' is not enough, and gets silently
        dropped back to GENERIC by _canonicalize_defconfig()'s
        `make gki_defconfig` re-expansion. Confirmed by direct
        inspection of the actual compiled .config
        (bazel-bin/common/kernel_aarch64_config/out_dir/.config) showing
        KASAN_GENERIC=y / HW_TAGS unset despite a manual gki_defconfig
        edit. The certified KMI symbol list for these branches expects
        kasan_flag_enabled exported (it's in every vendor
        abi_gki_aarch64_* list plus the frozen .stg), which only
        happens under HW_TAGS.

        Applied via --defconfig_fragment instead of editing gki_defconfig
        directly, mirroring coolzyd9107/GKI_SukiSU_Ultra_SUSFS's own
        working pipeline (same path convention:
        common/arch/arm64/configs/<name>.fragment, no BUILD.bazel edit
        needed since that directory is already covered by an existing
        glob()). Unlike that project, we do NOT strip
        KMI_SYMBOL_LIST_STRICT_MODE or protected_exports - KMI
        enforcement stays on; this fragment only resolves the KASAN
        choice so the build's actual ksymtab matches what strict mode
        expects, instead of bypassing the check itself."""
        common_dir = self.work_dir / "common"
        frag_path = common_dir / "arch/arm64/configs/kasan_fix.fragment"
        frag_path.write_text(
            "# CONFIG_KASAN_GENERIC is not set\n"
            "# CONFIG_KASAN_SW_TAGS is not set\n"
            "CONFIG_KASAN_HW_TAGS=y\n"
        )
        logger.info(f"Wrote KASAN choice fragment: {frag_path}")

    @property
    def artifact_suffix(self) -> str:
        """On the legacy build.sh path, tags full-LTO artifacts so they
        don't overwrite/get confused with a thin-LTO build of the same
        respin. Bazel/Kleaf branches stay untagged ("") since LTO mode
        there is fixed upstream (see build_kernel()), not user-chosen."""
        suffix = ""
        if (self.work_dir / "build/build.sh").exists() and self.config.lto_mode == "full":
            suffix = "-lto-full"
        # Experimental pids slot-swap builds must never be mistaken for a
        # normal release build.
        if self.patch_status.get("droidspaces_pids", {}).get("status") == "applied":
            suffix += "-pids"
        return suffix

    def _mem_used_mb(self) -> Optional[float]:
        """Reads /proc/meminfo and returns current used RAM in MB
        (MemTotal - MemAvailable), or None if unreadable (e.g. non-Linux
        host). MemAvailable (not MemFree) is used since it already
        accounts for reclaimable cache/buffers - the same metric the
        kernel's own OOM-killer heuristics are based on."""
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.strip().split()[0])  # kB
            total = info.get("MemTotal")
            avail = info.get("MemAvailable")
            if total is None or avail is None:
                return None
            return (total - avail) / 1024
        except (OSError, ValueError, IndexError):
            return None

    def _start_mem_monitor(self, interval: float = 2.0):
        """Starts a background thread sampling used RAM every `interval`
        seconds for the duration of the build command, so peak RAM usage
        (particularly relevant for LTO=full's single-threaded, memory-
        heavy link step) ends up in the build report instead of being a
        guess. Returns (thread, stop_event) - pass both to
        _stop_mem_monitor() once the build command finishes."""
        stop_event = threading.Event()
        peak = [self._mem_used_mb() or 0.0]

        def _poll():
            while not stop_event.is_set():
                sample = self._mem_used_mb()
                if sample is not None and sample > peak[0]:
                    peak[0] = sample
                stop_event.wait(interval)

        thread = threading.Thread(target=_poll, daemon=True)
        thread._peak_ref = peak  # stash for retrieval in _stop_mem_monitor
        thread.start()
        return thread, stop_event

    def _stop_mem_monitor(self, thread, stop_event):
        stop_event.set()
        thread.join(timeout=5)
        self._peak_mem_used_mb = thread._peak_ref[0]
        if self._peak_mem_used_mb:
            logger.info(f"Peak RAM used during build: {self._peak_mem_used_mb / 1024:.1f} GB")

    def _run_build_command(self, cmd: str) -> tuple:
        """Runs a (potentially very long) build command. Streams output
        live exactly as before, while also capturing it so build_kernel()
        can check it for known failure signatures afterwards."""
        lines = []

        def _capture(line: str):
            lines.append(line)
            # flush=True: without it, stdout is fully block-buffered once
            # piped through tee (build-kernel.sh does `2>&1 | tee log`),
            # while the logger's stderr writes are not - so these lines
            # could show up in the log file out of chronological order
            # relative to logger.info/warning/error calls (e.g. a WARNING
            # about a retry appearing before the failure output that
            # triggered it). Flushing immediately keeps the log readable
            # top-to-bottom.
            print(line, flush=True)

        mem_thread, mem_stop = self._start_mem_monitor()
        try:
            self.shell.run_with_callback(cmd, callback=_capture)
            return True, "\n".join(lines)
        except RuntimeError:
            return False, "\n".join(lines)
        finally:
            self._stop_mem_monitor(mem_thread, mem_stop)

    def _reported_lto_mode(self, is_legacy: bool) -> str:
        """What LTO actually got used, for the report and PATCH_STATUS.

        This used to hardcode "thin (Bazel default/fixed)" / "n/a (bazel)"
        on the Bazel path, from when we always passed --lto=thin there.
        We now pass --lto=full when the user asks for it and pass nothing
        otherwise, so the old strings would misreport a full-LTO Bazel
        build as thin - and the LTO mode is exactly the kind of thing
        someone reads off the report months later and believes.
        """
        if is_legacy:
            return self.config.lto_mode
        if self.config.lto_mode == "full":
            return "full (Bazel --lto=full)"
        return "Bazel target default (no --lto passed)"

    def _write_build_report(self, success: bool, build_seconds: float, is_legacy: bool):
        """Writes a short, human-readable summary of how this kernel was
        actually built - so this doesn't have to be dug out of the full
        build log."""
        from datetime import datetime
        report_path = self.work_dir / "BUILD_REPORT.txt"
        lines = [
            "GKI Kernel Build Report",
            "=" * 40,
            f"Config:        {self.config.config_name}",
            f"Kernel respin: {self.detected_respin or '(unknown - could not be determined)'}",
            f"Timestamp:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Build method:  {'Legacy build.sh' if is_legacy else 'Bazel (Kleaf)'}",
            f"LTO mode used: {self._reported_lto_mode(is_legacy)}",
            f"Peak RAM used:  {f'{self._peak_mem_used_mb / 1024:.1f} GB' if self._peak_mem_used_mb else '(could not be measured)'}",
            f"Result:        {'SUCCESS' if success else 'FAILED'}",
            f"Build time:    {build_seconds:.1f}s",
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"Build report written to: {report_path}")

    def build_kernel(self) -> bool:
        logger.info("=== Starting kernel compilation ===")
        self._chdir(self.work_dir)

        # NOTE ON SCOPE: this strips KMI_SYMBOL_LIST_STRICT_MODE from
        # build.config.gki.aarch64, which is the LEGACY build.sh path's
        # KMI gate (android12-5.10 / android13-5.15). So the "KMI
        # enforcement stays ON unconditionally" statement further down
        # applies to the Bazel/Kleaf path only - on the legacy path it is
        # off, and has been for as long as this pipeline has existed.
        #
        # That is the normal arrangement for a legacy-path KernelSU/SUSFS
        # build (the patches add exported symbols the stock list doesn't
        # know about, and build.sh's check has no equivalent of Bazel's
        # violation report), but it should be stated rather than implied:
        # android12/13 builds do NOT have the symbol-list safety net that
        # caught the Bazel bootloop. The post-build object and effective-
        # config checks are what stands in for it here.
        build_config = self.work_dir / "common/build.config.gki.aarch64"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            lines = [l for l in content.split('\n') if 'MODULES_ORDER=android/gki_aarch64_modules' not in l and 'KMI_SYMBOL_LIST_STRICT_MODE' not in l]
            with open(build_config, "w") as f:
                f.write('\n'.join(lines))

        import time
        start_time = time.time()
        is_legacy = (self.work_dir / "build/build.sh").exists()
        bazel_cache = Path.home() / ".cache" / "bazel"

        # Historical note: this used to refuse Bazel/Kleaf branches unless
        # --allow-bazel was passed explicitly. That guard existed because
        # a prior Bazel build on a different branch was once made to
        # compile by disabling KMI symbol-list enforcement, and that
        # confirmed a real-device bootloop - the Image's exported symbol
        # table silently didn't match what the device's vendor .ko
        # modules expected.
        #
        # The actual safety property was never the flag - it's that KMI
        # enforcement stays ON unconditionally below (no
        # --nokmi_symbol_list_strict_mode, no stripped protected_exports).
        # With the KASAN Kconfig-choice fix in
        # _write_kasan_choice_fragment() (see there for the full story -
        # tl;dr CONFIG_KASAN_GENERIC/SW_TAGS/HW_TAGS is a Kconfig 'choice'
        # group and gki_defconfig's own default silently overrides a bare
        # 'CONFIG_KASAN_HW_TAGS=y'), a from-scratch Bazel/Kleaf build on
        # android14-6.1-172 has now actually been verified end-to-end:
        # kmi_symbol_list_strict_mode and kmi_symbol_list_violations both
        # ran and passed for real, not bypassed. So Bazel branches now
        # build automatically here, same as legacy build.sh branches -
        # no manual per-run opt-in needed for the whole matrix to build.
        #
        # This is still self-enforcing per-branch/sub_level: if some
        # OTHER Bazel branch (e.g. a different android15-6.6 sub_level,
        # or android16 later) hits a genuine KMI violation that isn't
        # this same KASAN choice quirk, the build fails loudly here with
        # the specific violation list - it does not silently produce an
        # unverified Image. That failure is the safety net now, not a
        # flag someone has to remember to pass.
        if not is_legacy:
            logger.info("Bazel/Kleaf branch detected - building automatically (KMI enforcement stays ON).")

        # Known LLVM/ThinLTO verifier bug on Bazel/Kleaf branches (6.6 and
        # up so far): cross-module inlining under ThinLTO can produce an
        # inlined call in a function with debug info that's missing a
        # !dbg location, which trips the IR verifier ("Broken module
        # found, compilation aborted"). This isn't a LTO=thin-vs-none
        # question - a known-working reference build on this same branch
        # doesn't pass --lto at all and lets the Bazel target's own
        # default win, and that avoids the bug while still getting real
        # LTO (not a no-LTO fallback). So: force thin explicitly where
        # it's proven to work, and just omit --lto on branches affected
        # by this bug so Bazel's target default applies instead.
        def _build_cmd() -> str:
            if is_legacy:
                # LTO mode is user-selectable here (android12/android13,
                # legacy build.sh path only). build.sh natively supports
                # LTO=none|thin|full, so this is a straight passthrough -
                # no known verifier bug on this path like the Bazel one
                # below. 'full' means a single-threaded, RAM-heavy link
                # step (can need 16GB+ and much longer wall-clock time),
                # so it's opt-in via --lto-mode full rather than default.
                return (f"LTO={self.config.lto_mode} BUILD_CONFIG=common/build.config.gki.aarch64 "
                        f"build/build.sh CC=\"/usr/bin/ccache clang\"")
            # Building //common:kernel_aarch64/Image directly (instead of
            # the full //common:kernel_aarch64_dist target) matches
            # WildKernels/GKI_KernelSU_SUSFS's own build-kernel action,
            # and skips the GKI certification/ABI-validation actions that
            # are part of the _dist target's dependency chain.
            #
            # IMPORTANT: KMI symbol-list strict enforcement is deliberately
            # left ON here (no --nokmi_symbol_list_strict_mode /
            # --nokmi_symbol_list_violations_check, and Google's
            # abi_gki_protected_exports_* lists are left in place - a
            # remove_protected_exports() helper that stripped them used to
            # live here, was never actually wired into build(), and has
            # been deleted rather than left as a loaded gun).
            # A prior build that disabled this enforcement
            # compiled successfully but caused a confirmed real-device
            # bootloop - the resulting Image's exported symbol table
            # didn't actually match what the device's vendor .ko modules
            # expected, and nothing caught that until it was flashed. If
            # the patches genuinely violate the KMI symbol list now, the
            # build will fail loudly with the specific violations instead
            # - which is strictly more useful than a silent, unverified
            # "success".
            # LTO: pass nothing by default and let the Bazel target's own
            # default win. This is what WildKernels' build-kernel action
            # does on EVERY Bazel branch, not just the ones where ThinLTO
            # is known to trip the IR verifier - and it's the
            # configuration their android14-6.1 / android15-6.6 /
            # android16-6.12 builds actually ship from. Forcing
            # --lto=thin was only ever a guess that happened to work on
            # 6.1; there is no upstream reference build that passes it.
            #
            # --lto-mode full is still honoured here as an explicit
            # opt-in (Kleaf accepts --lto=full), because full LTO on 6.6
            # is a thing other projects do get working - just not via
            # Kleaf's defaults. Treat it as an experiment, not a default.
            lto_flag = "--lto=full " if self.config.lto_mode == "full" else ""
            frag_flag = "--defconfig_fragment=//common:arch/arm64/configs/kasan_fix.fragment "
            return (f"tools/bazel build --disk_cache={bazel_cache} --config=fast "
                    f"{lto_flag}{frag_flag}//common:kernel_aarch64/Image")

        try:
            if is_legacy:
                logger.info("Using legacy build method...")
            else:
                logger.info("Using Bazel build method (KMI enforcement left ON)...")
                self._disable_bazel_check_defconfig()
                # Only canonicalize if POST_DEFCONFIG_CMDS could NOT be
                # neutralised. Regenerating gki_defconfig through the HOST
                # toolchain is not a neutral reformat: `make savedefconfig`
                # re-evaluates every dependency using the host's detected
                # capabilities, and silently drops anything the host can't
                # satisfy. CONFIG_DEBUG_INFO_BTF is the live example -
                # on 6.6 it depends on `!DEBUG_INFO_DWARF5 ||
                # PAHOLE_VERSION >= 121`, and android15-6.6's gki_defconfig
                # selects DWARF5, so a host without a recent pahole quietly
                # removes BTF from the defconfig before Kleaf ever sees it.
                # android14-6.1 selects DWARF4, where that dependency is
                # satisfied unconditionally - which is exactly why BTF
                # survived there and vanished here.
                if self.patch_status.get("bazel_post_defconfig_cmds", {}).get("status") == "applied":
                    logger.info(
                        "Skipping host savedefconfig canonicalization - "
                        "POST_DEFCONFIG_CMDS=true already disables Kleaf's "
                        "defconfig check, and canonicalizing through the host "
                        "toolchain silently drops host-unsatisfiable symbols."
                    )
                    self._mark("defconfig_canonicalization", "skipped",
                               "not needed; POST_DEFCONFIG_CMDS covers the check")
                else:
                    self._canonicalize_defconfig()
                    self._mark("defconfig_canonicalization", "applied",
                               "host savedefconfig fallback")
                self._write_kasan_choice_fragment()
                bazel_cache.mkdir(parents=True, exist_ok=True)

            # No fallback retry on failure - if the build fails, it fails,
            # full stop. The LTO mode itself is chosen upfront per branch
            # above (thin where proven, Bazel default where thin is known
            # to hit the verifier bug), not switched after a failed attempt.
            success, output = self._run_build_command(_build_cmd())

            build_seconds = time.time() - start_time
            self._write_build_report(success, build_seconds, is_legacy)

            if success:
                logger.info("=== Kernel compilation succeeded ===")
                return True
            logger.error("Kernel compilation failed")
            return False
        except Exception as e:
            logger.error(f"Error during compilation: {e}")
            return False

    def _verify_expected_objects(self):
        """Post-build sanity check: confirm specific source files we
        expect to be compiled in actually produced a .o file, instead
        of only trusting "the patch applied" / "CONFIG_X=y made it into
        gki_defconfig" as proof on their own.

        This is exactly the class of bug that let SUSFS report as fully
        applied (green PATCH_STATUS.json, patch tool exit code 0) for a
        long time while fs/susfs.o was never actually compiled in - a
        missing CONFIG_KSU_SUSFS Kconfig *definition* further up the
        chain (the SukiSU-Ultra side, not the patch itself) silently
        swallowed the setting, and nothing checked the actual build
        output to notice. See apply_susfs_kernelsu_patch()'s docstring
        for the full story. A hard failure here, naming exactly which
        expected object is missing, is a lot cheaper to debug than
        rediscovering that story from scratch.

        Legacy build.sh branches only for now - Bazel's sandboxed
        intermediate outputs aren't laid out the same way (they live
        under bazel-bin/ / bazel-out/ rather than out/<branch>/common).
        NOTE: Bazel branches DO build now (see build_kernel()), so this
        early return means the whole Bazel half of the matrix currently
        has no post-build object verification at all. That gap is worth
        closing separately - it is not "those branches don't build
        anyway" any more.
        """
        if not (self.work_dir / "build/build.sh").exists():
            logger.warning(
                "Post-build object verification skipped: Bazel/Kleaf build "
                "(output layout differs). This check currently only covers "
                "the legacy build.sh path."
            )
            return
        obj_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/common"
        if not obj_dir.exists():
            logger.warning(
                f"_verify_expected_objects: expected object directory "
                f"not found ({obj_dir}) - skipping post-build "
                f"verification (this itself may be worth investigating "
                f"if the build otherwise reported success)."
            )
            return

        # (description, path relative to obj_dir) - only features this
        # build actually enabled AND that exist on this branch. Anything
        # listed here is treated as mandatory: a miss is a hard failure.
        # Add entries as more features gain a similarly narrow,
        # hard-to-notice failure mode - but gate them the same way, or
        # the check starts failing correct builds, which trains people
        # to ignore it.
        checks = [("SUSFS", "fs/susfs.o")]
        if self._amneziawg_applied():
            checks.append(("AmneziaWG", "drivers/net/amneziawg/main.o"))
        if self._has_fuse_bpf():
            checks.append(("FUSE-BPF", "fs/fuse/backing.o"))
        else:
            logger.info(
                f"Skipping FUSE-BPF object check - not available on "
                f"{self.config.android_version}-{self.config.kernel_version}"
            )

        missing = []
        for name, rel_path in checks:
            obj_path = obj_dir / rel_path
            if not obj_path.exists():
                missing.append((name, rel_path))

        if missing:
            lines = "\n".join(f"  - {name}: expected {rel_path}, not found" for name, rel_path in missing)
            raise RuntimeError(
                f"Post-build verification failed - the following "
                f"features are marked as enabled/applied but their "
                f"expected compiled object is missing from the build "
                f"output ({obj_dir}):\n{lines}\n"
                f"This means the feature almost certainly did not "
                f"actually make it into the Image, even though earlier "
                f"steps reported success. Do not flash this build."
            )
        logger.info(
            f"=== Post-build verification: all {len(checks)} expected "
            f"object(s) found - OK ==="
        )
        for name, rel_path in checks:
            logger.info(f"      -> {name}: {rel_path}")

    # Path segments that mark a Bazel location as transient. Anything
    # under a sandbox is deleted when the action finishes, so a .config
    # found there can vanish between glob and read - which is exactly
    # what happened on an android15-6.6 build: the glob picked
    # .../sandbox/sandbox_stash/KernelBuildKmiSymbolListStrictMode/15/
    # execroot/.../kernel_aarch64_config/out_dir/.config, and the read
    # then died with ENOENT AFTER a fully successful kernel build.
    _TRANSIENT_PATH_MARKERS = ("sandbox", "sandbox_stash", "execroot",
                               "output_user_root")

    @classmethod
    def _is_transient_bazel_path(cls, path: Path) -> bool:
        return any(part in cls._TRANSIENT_PATH_MARKERS for part in path.parts)

    def _find_built_config(self) -> Optional[Path]:
        """Locates the .config the build actually produced (not the
        gki_defconfig we wrote into).

        Stable locations are tried first, transient Bazel sandbox paths
        are excluded outright, and the newest survivor wins. Callers must
        still read defensively: Bazel can clean up underneath us at any
        point after the build finishes.
        """
        candidates = [
            self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/common/.config",
            self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/.config",
            # Kleaf's stable published config output (bazel-bin is a
            # symlink into the build root, not into a sandbox).
            self.work_dir / "bazel-bin/common/kernel_aarch64_config/out_dir/.config",
        ]
        for c in candidates:
            if c.exists() and not self._is_transient_bazel_path(c):
                return c
        found = []
        if (self.work_dir / "out").exists():
            for p in (self.work_dir / "out").glob("**/.config"):
                if self._is_transient_bazel_path(p):
                    continue
                try:
                    found.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        found.sort(reverse=True)
        return found[0][1] if found else None

    @staticmethod
    def _read_config_symbols(path: Path):
        """Parses a .config into (present set, values dict).

        Returns None if the file has disappeared - a real possibility for
        Bazel outputs, and never a reason to fail an otherwise successful
        build.
        """
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return None
        present, values = set(), {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            if sep and value not in ("n", '""'):
                present.add(name)
                values[name] = value
        return present, values

    # Symbols this pipeline writes into gki_defconfig and expects to
    # survive into the real .config. The first element of each tuple is
    # the symbol, the second whether a miss is fatal.
    #
    # Why this exists: show_kernel_config() reads gki_defconfig - the
    # INPUT file this pipeline appends to - so it reports "enabled" for
    # anything we wrote, whether or not Kconfig accepted it. An
    # undefined symbol (missing Kconfig definition, unmet dependency,
    # overridden by a `choice` group) is dropped silently, with no
    # warning and a zero exit code. That is precisely how
    # CONFIG_KSU_SUSFS=y sat in the defconfig for a long time while
    # fs/susfs.o was never compiled. _verify_expected_objects() catches
    # that for two specific files; this catches it for every feature
    # flag at once, which is the generalisable half of the same lesson.
    def _expected_config_symbols(self) -> list:
        symbols = [
            ("CONFIG_KSU", True),
            ("CONFIG_KSU_SUSFS", True),
            # All nine SUSFS symbols, not a sample. A device check found
            # four options being written into gki_defconfig that upstream
            # had already deprecated - they were absent from the built
            # kernel and nothing noticed, because the two SUSFS symbols
            # spot-checked here happened to be survivors. Listing the
            # full set means the next upstream removal shows up on the
            # first build after it happens.
            # configure_kernel() deliberately writes
            # CONFIG_KSU_SUSFS_SUS_PATH=n on 6.6 ("6.6 needs it off"), so
            # expecting it there turns a deliberate choice into a reported
            # failure. Expect it only where we actually ask for it.
        ] + ([("CONFIG_KSU_SUSFS_SUS_PATH", False)]
             if self.config.kernel_version != "6.6" else []) + [
            ("CONFIG_KSU_SUSFS_SUS_MAP", False),
            ("CONFIG_KSU_SUSFS_SUS_MOUNT", False),
            ("CONFIG_KSU_SUSFS_SUS_KSTAT", False),
            ("CONFIG_KSU_SUSFS_SPOOF_UNAME", False),
            ("CONFIG_KSU_SUSFS_ENABLE_LOG", False),
            ("CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS", False),
            ("CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG", False),
            ("CONFIG_KSU_SUSFS_OPEN_REDIRECT", False),
            ("CONFIG_WIREGUARD", False),
            ("CONFIG_IP_SET", False),
            ("CONFIG_NET_SCH_CAKE", False),
            ("CONFIG_CIFS", False),
            ("CONFIG_TMPFS_XATTR", False),
            ("CONFIG_DEBUG_INFO_BTF", False),
            ("CONFIG_BPF_EVENTS", False),
        ]
        if self.config.use_kpm:
            symbols.append(("CONFIG_KPM", False))
        if self._amneziawg_applied():
            symbols.append(("CONFIG_AMNEZIAWG", True))
        if self._has_fuse_bpf():
            symbols.append(("CONFIG_FUSE_BPF", True))
        if self.config.use_bbg:
            symbols.append(("CONFIG_BBG", False))
        if self.config.use_zram:
            symbols.append(("CONFIG_ZRAM", False))
            symbols.append(("CONFIG_CRYPTO_LZ4KD", False))
        if self.config.use_mglru:
            symbols.append(("CONFIG_LRU_GEN", False))
        if self.config.use_psi:
            symbols.append(("CONFIG_PSI", False))
        if self.patch_status.get("ntsync", {}).get("status") == "applied":
            symbols.append(("CONFIG_NTSYNC", False))
        if self.patch_status.get("droidspaces", {}).get("status") == "applied":
            symbols.extend([
                ("CONFIG_SYSVIPC", False),
                ("CONFIG_IPC_NS", False),
                ("CONFIG_POSIX_MQUEUE", False),
            ])
        if self.config.bbr_version == "bbr3" and self._bbrv3_applied:
            # TCP_CONG_BBR3 absent = the backport didn't actually reach
            # Kconfig, so the kernel has no BBRv3 at all despite the
            # patch reporting applied - fatal. DEFAULT_BBR3 is a choice
            # member; if it lost the choice the algorithm is still built
            # and switchable at runtime, so that's a warning, not a
            # reason to throw away a working kernel.
            symbols.append(("CONFIG_TCP_CONG_BBR3", True))
            symbols.append(("CONFIG_DEFAULT_BBR3", False))
        elif self.config.bbr_version in ("bbr1", "bbr3"):
            symbols.append(("CONFIG_DEFAULT_BBR", False))
        if self.config.use_ath9k:
            # All fatal, unlike the networking set below: a dropped option
            # here means either the driver was never built or it was built
            # against a different struct layout than the device's stack.
            # Both produce a kernel that looks fine and an adapter that
            # does not work.
            for line in self._ath9k_fragment_lines():
                if line.startswith("CONFIG_") and "=" in line:
                    symbols.append((line.split("=", 1)[0], True))
        if self.config.use_extra_net:
            # Derived from the template itself rather than retyped, so
            # the two lists cannot drift apart. All non-fatal: a dropped
            # networking option means that feature is missing, which is
            # worth a loud warning, but throwing away an otherwise good
            # kernel over it would be out of proportion.
            for line in self._extra_net_config_text().splitlines():
                line = line.strip()
                if line.startswith("CONFIG_") and "=" in line:
                    symbols.append((line.split("=", 1)[0], False))
        return symbols

    def _extra_net_config_text(self) -> str:
        """EXTRA_NET_CONFIG_TEMPLATE with symbols that don't exist on this
        kernel version filtered out.

        CONFIG_NFT_COUNTER was removed upstream by commit 023223dfbfb3
        ("netfilter: nf_tables: make counter support built-in") - it
        exists as a selectable symbol only in 3.13 through 5.16. From
        5.17 onward the counter expression is unconditionally part of
        nf_tables core, so there is nothing to enable.

        This matters because writing a symbol Kconfig doesn't know about
        is silently dropped, and _expected_config_symbols() then
        correctly reports it missing from the built .config - producing
        an effective_config FAILURE for a feature that is actually
        present. Filtering here (rather than special-casing the
        verifier) keeps the "written" and "expected" lists derived from
        the same source, which is the property that stops them drifting.
        """
        text = self.EXTRA_NET_CONFIG_TEMPLATE
        if self._version_tuple(self.config.kernel_version) >= (5, 17):
            text = "\n".join(
                line for line in text.splitlines()
                if not line.strip().startswith("CONFIG_NFT_COUNTER=")
            ) + "\n"
        return text

    def _expected_config_values(self) -> list:
        """(symbol, expected, fatal) where expected is "y"/"m", or None
        meaning the symbol MUST be absent.

        _expected_config_symbols() only asks "is it there?", which is the
        right question for a feature toggle and the wrong one here.
        CONFIG_CFG80211=y instead of =m is exactly the change that killed
        internal WiFi on the first attempt, and it would pass a presence
        check without a murmur. Absence matters just as much:
        MAC80211_DEBUGFS or CFG80211_WEXT creeping back in changes the
        layout of exported structs, so the CRCs stop matching the vendor
        stack and the driver becomes unloadable.
        """
        out = []
        if self.patch_status.get("droidspaces_pids", {}).get("status") == "applied":
            # Both fatal: PIDS on with NET_PRIO still on means 8 cgroup
            # subsystems - exactly the kABI break the slot swap avoids.
            out.append(("CONFIG_CGROUP_PIDS", "y", True))
            out.append(("CONFIG_CGROUP_NET_PRIO", None, True))
        if self._amneziawg_applied():
            # =m would pass a presence check and ship a kernel without it
            # (nothing loads a .ko from the build); stock WireGuard must
            # stay built in next to it.
            out.append(("CONFIG_AMNEZIAWG", "y", True))
            out.append(("CONFIG_AMNEZIAWG_DEBUG", None, True))
            out.append(("CONFIG_WIREGUARD", "y", True))
        if self.config.use_ath9k:
            for line in self._ath9k_fragment_lines():
                if line.startswith("CONFIG_") and "=" in line:
                    name, _, value = line.partition("=")
                    out.append((name, value.strip(), True))
                elif line.startswith("# CONFIG_") and line.endswith("is not set"):
                    out.append((line.split()[1], None, True))
        return out

    def _verify_effective_config(self):
        """Diffs "what we asked for" against "what Kconfig actually
        accepted", by reading the .config the build produced rather than
        the gki_defconfig we wrote.

        Fatal symbols raise; non-fatal ones are logged as a warning list
        so a single unexpectedly-unavailable option on some branch
        doesn't block an otherwise good build - but never disappears in
        silence either. Every result also lands in PATCH_STATUS.json
        under "effective_config" so it shows up in the CI summary.
        """
        config_path = self._find_built_config()
        if config_path is None:
            logger.warning(
                "Effective-config verification skipped: could not locate the "
                "built .config under out/. If the build reported success, "
                "this is itself worth a look."
            )
            self._mark("effective_config", "skipped", "built .config not found")
            return

        parsed = self._read_config_symbols(config_path)
        if parsed is None:
            logger.warning(
                f"Effective-config verification skipped: {config_path} "
                f"disappeared before it could be read (Bazel cleans up "
                f"sandbox outputs). The Image IKCONFIG check below reads "
                f"the artifact itself and does not depend on this file."
            )
            self._mark("effective_config", "skipped", "built .config vanished before read")
            return
        present, values = parsed

        missing_fatal, missing_soft = [], []
        for symbol, fatal in self._expected_config_symbols():
            if symbol not in present:
                (missing_fatal if fatal else missing_soft).append(symbol)

        for symbol, expected, fatal in self._expected_config_values():
            actual = values.get(symbol)
            if expected is None and actual is not None:
                msg = (f"{symbol}={actual} but it MUST be absent - "
                       f"something re-enabled it (a Kconfig select?)")
            elif expected is not None and actual != expected:
                msg = (f"{symbol}={actual or 'absent'} but {expected} was "
                       f"requested")
            else:
                continue
            (missing_fatal if fatal else missing_soft).append(msg)

        logger.info(f"=== Effective config verification ({config_path}) ===")
        if missing_soft:
            logger.warning(
                "These options were written into gki_defconfig but are NOT in "
                "the built .config - Kconfig dropped them (undefined symbol, "
                "unmet dependency, or overridden by a choice group). The "
                "feature is NOT in this kernel:\n  "
                + "\n  ".join(missing_soft)
            )
        if missing_fatal:
            raise RuntimeError(
                "Effective config verification failed - these mandatory "
                "options were requested but are absent from the built "
                f".config ({config_path}):\n  "
                + "\n  ".join(missing_fatal)
                + "\nKconfig silently drops undefined symbols, so the "
                  "defconfig and the patch status both still look correct. "
                  "Do not flash this build."
            )
        if not missing_soft:
            logger.info("All expected config symbols present in the built .config - OK")
        self._mark(
            "effective_config",
            "applied" if not missing_soft else "failed",
            "all expected symbols present" if not missing_soft
            else f"dropped by Kconfig: {', '.join(missing_soft)}",
        )

    def _find_built_image(self) -> Optional[Path]:
        """The uncompressed Image this build produced, on either path."""
        if (self.work_dir / "build/build.sh").exists():
            candidate = (self.work_dir
                         / f"out/{self.config.android_version}-{self.config.kernel_version}/dist/Image")
        else:
            candidate = self.work_dir / "bazel-bin/common/kernel_aarch64/Image"
        return candidate if candidate.exists() else None

    @staticmethod
    def _extract_ikconfig(image_bytes: bytes) -> Optional[str]:
        """Pulls the kernel's own embedded .config out of a built Image.

        CONFIG_IKCONFIG stores the gzipped .config inside the image,
        bracketed by the magic markers IKCFG_ST / IKCFG_ED. This is the
        same data the kernel later serves as /proc/config.gz.
        """
        import gzip
        start = image_bytes.find(b"IKCFG_ST")
        if start < 0:
            return None
        end = image_bytes.find(b"IKCFG_ED", start + 8)
        if end < 0:
            return None
        try:
            return gzip.decompress(image_bytes[start + 8:end]).decode("utf-8", errors="replace")
        except (OSError, EOFError, gzip.BadGzipFile):
            return None

    def _verify_image_ikconfig(self):
        """Verifies the FINAL ARTIFACT, not the build tree.

        _verify_effective_config() reads the .config found under out/.
        That is one step better than trusting gki_defconfig, but it still
        trusts that the .config we located is the one this Image was
        actually built from - which is exactly the assumption that breaks
        on a stale out/ directory, a reused workspace, or a Bazel build
        whose real config lives in a content-hashed cache path.

        This check closes that gap by reading the config out of the Image
        itself (CONFIG_IKCONFIG's gzipped blob between the IKCFG_ST and
        IKCFG_ED markers - the same bytes the kernel serves as
        /proc/config.gz). If the two disagree, the .config we verified was
        not the one that produced this Image, and the earlier PASS was
        meaningless.

        Be clear about what this does and does not prove. It proves the
        shipped Image contains the configuration that was asked for. It
        does NOT prove the kernel boots - nothing short of running it on
        matching hardware does. It is evidence, not a boot test.
        """
        image = self._find_built_image()
        if image is None:
            logger.warning(
                "Image IKCONFIG verification skipped: no uncompressed Image "
                "found at the expected location for this build method."
            )
            self._mark("image_ikconfig", "skipped", "Image not found")
            return

        config_text = self._extract_ikconfig(image.read_bytes())
        if config_text is None:
            logger.warning(
                "Image IKCONFIG verification skipped: no readable IKCFG_ST/"
                "IKCFG_ED blob in the Image. This needs CONFIG_IKCONFIG=y, "
                "which stock GKI normally sets (it is what backs "
                "/proc/config.gz). If it is missing, that is worth knowing "
                "in its own right."
            )
            self._mark("image_ikconfig", "skipped", "no IKCONFIG blob in Image")
            return

        present, values = set(), {}
        for line in config_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            if sep and value not in ("n", '""'):
                present.add(name)
                values[name] = value

        missing_fatal, missing_soft = [], []
        for symbol, fatal in self._expected_config_symbols():
            if symbol not in present:
                (missing_fatal if fatal else missing_soft).append(symbol)

        for symbol, expected, fatal in self._expected_config_values():
            actual = values.get(symbol)
            if expected is None and actual is not None:
                msg = f"{symbol}={actual} but it MUST be absent"
            elif expected is not None and actual != expected:
                msg = f"{symbol}={actual or 'absent'} but {expected} was requested"
            else:
                continue
            (missing_fatal if fatal else missing_soft).append(msg)

        logger.info(f"=== Image IKCONFIG verification ({image.name}, "
                    f"{len(present)} symbols set) ===")

        # Cross-check against the .config _verify_effective_config() read.
        # A disagreement means we verified a different tree than we shipped.
        drift = []
        config_path = self._find_built_config()
        tree_parsed = self._read_config_symbols(config_path) if config_path else None
        if tree_parsed is not None:
            tree_present = tree_parsed[0]
            for symbol, _fatal in self._expected_config_symbols():
                in_tree = symbol in tree_present
                in_image = symbol in present
                if in_tree != in_image:
                    drift.append(
                        f"{symbol}: .config={'set' if in_tree else 'unset'} "
                        f"but Image={'set' if in_image else 'unset'}"
                    )

        if missing_soft:
            logger.warning(
                "Requested but NOT present in the shipped Image:\n  "
                + "\n  ".join(missing_soft)
            )
        if drift:
            raise RuntimeError(
                "Image IKCONFIG disagrees with the .config that was verified "
                f"({config_path}):\n  " + "\n  ".join(drift)
                + "\nThe .config checked earlier is not the one this Image was "
                  "built from, so that check proved nothing. Most likely a "
                  "stale out/ directory or a reused workspace. Do not flash "
                  "this build."
            )
        if missing_fatal:
            raise RuntimeError(
                "Image IKCONFIG verification failed - mandatory options are "
                f"absent from the shipped Image ({image}):\n  "
                + "\n  ".join(missing_fatal)
                + "\nDo not flash this build."
            )
        if not missing_soft:
            logger.info("All expected config symbols present in the shipped Image - OK")
        self._mark(
            "image_ikconfig",
            "applied" if not missing_soft else "failed",
            f"{len(present)} symbols; all expected present" if not missing_soft
            else f"absent from Image: {', '.join(missing_soft)}",
        )

    def patch_kpm_image(self):
        if not self.config.use_kpm or self.config.kernel_version == "6.6":
            return
        logger.info("=== Patching Image file (KPM) ===")
        self._chdir(self.work_dir)

        if (self.work_dir / "build/build.sh").exists():
            image_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_dir = self.work_dir / "bazel-bin/common/kernel_aarch64"

        if not image_dir.exists():
            return
        self._chdir(image_dir)
        if self._kpm_next():
            self._patch_kpm_image_next(image_dir)
            return
        # The patcher is pinned (KPM_PATCH_REF, top of this file) and verified by sha256
        # before it runs: it rewrites the finished Image, so an unnoticed
        # upstream change here ships a kernel that may not boot.
        patcher = image_dir / "patch"
        if patcher.exists():
            patcher.unlink()
        stale = image_dir / "oImage"
        if stale.exists():
            stale.unlink()
        self._run_cmd(f"curl -LSsf {KPM_PATCH_URL} -o patch", check=False)
        import hashlib
        digest = hashlib.sha256(patcher.read_bytes()).hexdigest() if patcher.exists() else ""
        if digest != KPM_PATCH_SHA256:
            self._mark("kpm_image_patch", "failed", f"patch_linux sha256 mismatch ({digest[:12] or 'download failed'})")
            raise RuntimeError(
                f"KPM patcher (SukiSU_patch {KPM_PATCH_REF[:12]} kpm/patch_linux) "
                f"download failed or its sha256 doesn't match KPM_PATCH_SHA256 "
                f"(got {digest or 'nothing'}). Refusing to patch the Image with an "
                f"unknown binary. Stopping."
            )
        patcher.chmod(0o755)
        result = self._run_cmd("./patch", check=False)
        if result.returncode != 0 or not stale.exists():
            self._mark("kpm_image_patch", "failed", f"patch_linux exit {result.returncode}")
            raise RuntimeError(
                f"KPM patcher failed (exit code {result.returncode}, oImage "
                f"{'present' if stale.exists() else 'missing'}). The Image would "
                f"ship without KPM. Stopping."
            )
        self._run_cmd("mv oImage Image", check=False)
        self._mark("kpm_image_patch", "applied", f"SukiSU_patch {KPM_PATCH_REF[:12]} patch_linux")

    @staticmethod
    def _kpm_next() -> bool:
        """True (pinned kptools + kpimg, KernelPatch 0.13.9) unless the build
        asks for the legacy 547ae94 patcher: KPM_LEGACY=1, or KPM_NEXT=0."""
        off = ("0", "no", "false")
        if os.environ.get("KPM_LEGACY", "").strip().lower() in ("1", "yes", "true"):
            return False
        return os.environ.get("KPM_NEXT", "1").strip().lower() not in off

    def _build_kpm_next_kptools(self) -> Path:
        """kptools (x86-64 host) from SukiSU_KernelPatch_patch at
        KPM_NEXT_TOOLS_REF. Built once per pin and reused (workspace dir
        named after the commit). Needs gcc + a static libz (zlib1g-dev)."""
        src = self.workspace / f"kptools-{KPM_NEXT_TOOLS_REF[:12]}"
        kptools = src / "tools/kptools"
        if not kptools.exists():
            if src.exists():
                shutil.rmtree(src)
            src.mkdir(parents=True)
            r = self._run_cmd(
                f"git -C {src} init -q && "
                f"git -C {src} fetch -q --depth 1 {KPM_NEXT_TOOLS_REPO} {KPM_NEXT_TOOLS_REF} && "
                f"git -C {src} checkout -q FETCH_HEAD", check=False)
            head = self._run_cmd(f"git -C {src} rev-parse HEAD", check=False,
                                 capture_output=True)
            if r.returncode != 0 or (head.stdout or "").strip() != KPM_NEXT_TOOLS_REF:
                raise RuntimeError(
                    f"KPM_NEXT: could not fetch SukiSU_KernelPatch_patch "
                    f"{KPM_NEXT_TOOLS_REF[:12]}. Stopping.")
            r = self._run_cmd(f"make -C {src}/tools -j2 kptools", check=False)
            if r.returncode != 0 or not kptools.exists():
                raise RuntimeError(
                    "KPM_NEXT: building kptools failed (needs gcc and zlib1g-dev "
                    "for the static libz). Stopping.")
        ver = self._run_cmd(f"{kptools} -v", check=False, capture_output=True)
        out = (ver.stdout or "") + (ver.stderr or "")
        if not re.search(rf"\b{KPM_NEXT_VERSION}\b", out):
            raise RuntimeError(
                f"KPM_NEXT: kptools reports '{out.strip()}', expected version "
                f"{KPM_NEXT_VERSION}. Stopping.")
        return kptools

    def _check_kpm_bridge(self):
        """SukiSU 3b58d46f+ reaches KernelPatch through a kernel-to-kernel bridge
        (kernel/extension/kpm/bridge.h) and has no fallback to the old stub
        hooks. Checked right after SukiSU is in place, so a wrong mix stops
        before the long compile instead of shipping a kernel whose KPM is dead:
          bridge.h + --kpm-legacy (0.13.0, no bridge)        -> stop
          bridge.h + default, bridge != KPM_NEXT_BRIDGE      -> stop, pins must move
          no bridge.h + default (kpimg has only the bridge)  -> stop, use --kpm-legacy
          no bridge.h + --kpm-legacy                         -> old way, fine"""
        if not self.config.use_kpm or self.config.kernel_version == "6.6":
            return
        bridge = self.work_dir / KPM_BRIDGE_H
        nxt = self._kpm_next()
        if not bridge.exists():
            if nxt:
                self._mark("kpm_bridge", "failed", "SukiSU has no bridge.h, the pinned kpimg needs it (use --kpm-legacy)")
                raise RuntimeError(
                    f"KPM: the pinned KernelPatch (kpimg SukiSU_patch {KPM_NEXT_KPIMG_REF[:7]}) "
                    f"only speaks the new KPM bridge, but this SukiSU has no "
                    f"kernel/extension/kpm/bridge.h (older than 3b58d46f). KPM would not "
                    f"work. Build this SukiSU with --kpm-legacy. Stopping.")
            self._mark("kpm_bridge", "skipped", "older SukiSU, no bridge - legacy patcher (--kpm-legacy)")
            return
        if not nxt:
            self._mark("kpm_bridge", "failed", "SukiSU needs the bridge, legacy patcher has none")
            raise RuntimeError(
                f"KPM: this SukiSU (3b58d46f or newer) talks to KernelPatch over the new "
                f"KPM bridge. The legacy patcher (SukiSU_patch {KPM_PATCH_REF[:7]}, "
                f"KernelPatch 0.13.0) does not have it, so KPM would not work. "
                f"Build without --kpm-legacy. Stopping.")
        text = bridge.read_text(errors="replace")
        have = dict(re.findall(r"^#define\s+(SUKISU_KPM_\w+)\s+(\S+)", text, re.M))
        diff = [f"{k}: SukiSU {have.get(k, 'missing')}, pinned {v}"
                for k, v in KPM_NEXT_BRIDGE.items() if have.get(k) != v]
        if diff:
            self._mark("kpm_bridge", "failed", "; ".join(diff))
            raise RuntimeError(
                "KPM: SukiSU's KPM bridge does not match the pinned "
                "KernelPatch (" + "; ".join(diff) + "). KPM_NEXT_* must move to a "
                "KernelPatch built for this bridge. Stopping.")
        logger.info(f"KPM: SukiSU bridge matches KernelPatch kpimg {KPM_NEXT_KPIMG_REF[:7]} "
                    f"(ABI {KPM_NEXT_BRIDGE['SUKISU_KPM_ABI_VERSION']})")
        self._mark("kpm_bridge", "applied",
                   f"bridge ABI {KPM_NEXT_BRIDGE['SUKISU_KPM_ABI_VERSION']} = kpimg {KPM_NEXT_KPIMG_REF[:12]}")

    def _patch_kpm_image_next(self, image_dir: Path):
        """KPM_NEXT path of patch_kpm_image(): same result as SukiSU_patch's
        patch_linux, done with a pinned kptools + pinned kpimg (see the
        KPM_NEXT_* constants at the top of this file for why)."""
        import hashlib
        logger.info(f"KPM_NEXT: KernelPatch {KPM_NEXT_VERSION} - kpimg SukiSU_patch "
                    f"{KPM_NEXT_KPIMG_REF[:12]}, kptools SukiSU_KernelPatch_patch "
                    f"{KPM_NEXT_TOOLS_REF[:12]}")
        kptools = self._build_kpm_next_kptools()
        kpimg = image_dir / "kpimg"
        oimage = image_dir / "oImage"
        for f in (kpimg, oimage):
            if f.exists():
                f.unlink()
        self._run_cmd(f"curl -LSsf {KPM_NEXT_KPIMG_URL} -o {kpimg}", check=False)
        digest = hashlib.sha256(kpimg.read_bytes()).hexdigest() if kpimg.exists() else ""
        if digest != KPM_NEXT_KPIMG_SHA256:
            self._mark("kpm_image_patch", "failed", f"kpimg sha256 mismatch ({digest[:12] or 'download failed'})")
            raise RuntimeError(
                f"KPM_NEXT: kpimg (SukiSU_patch {KPM_NEXT_KPIMG_REF[:12]}) download "
                f"failed or its sha256 doesn't match (got {digest or 'nothing'}). Stopping.")
        image = image_dir / "Image"
        in_size = image.stat().st_size
        # kptools can print raw bytes on a bad Image, so go through a file
        # instead of capture_output (text mode would crash on decoding).
        log = image_dir / "kptools.log"
        r = self._run_cmd(f"{kptools} -p -s 123 -i Image -k kpimg -o oImage > kptools.log 2>&1",
                          check=False)
        out = log.read_text(errors="replace") if log.exists() else ""
        for line in out.splitlines():
            logger.info(f"kptools: {line}")
        problems = []
        if r.returncode != 0:
            problems.append(f"exit code {r.returncode}")
        if not oimage.exists():
            problems.append("no oImage")
        elif oimage.stat().st_size <= in_size:
            problems.append(f"oImage ({oimage.stat().st_size}) not larger than Image ({in_size})")
        for marker in (f"kpimg version: {KPM_NEXT_VERSION}", "kpimg config: android", "patch done"):
            if marker not in out:
                problems.append(f"'{marker}' missing from the kptools log")
        if problems:
            self._mark("kpm_image_patch", "failed", "KPM_NEXT: " + "; ".join(problems))
            raise RuntimeError(
                "KPM_NEXT: patching the Image failed (" + "; ".join(problems) +
                "). The kernel would not boot or would ship without KPM. Stopping.")
        kpimg.unlink()
        self._run_cmd("mv oImage Image", check=False)
        logger.info(f"KPM_NEXT: Image patched with KernelPatch {KPM_NEXT_VERSION}")
        self._mark("kpm_image_patch", "applied",
                   f"KPM_NEXT kptools {KPM_NEXT_TOOLS_REF[:12]} + kpimg "
                   f"{KPM_NEXT_KPIMG_REF[:12]} ({KPM_NEXT_VERSION})")

    def prepare_boot_images(self) -> list:
        logger.info("=== Preparing boot images ===")
        self._chdir(self.work_dir)
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        artifacts = []

        if (self.work_dir / "build/build.sh").exists():
            image_source = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_source = self.work_dir / "bazel-bin/common/kernel_aarch64"

        for image_name in ["Image"]:
            src = image_source / image_name
            if src.exists():
                self._run_cmd(f"cp {src} {bootimgs_dir}/ && cp {src} {self.work_dir}/", check=False)

        if self.config.android_version == "android12":
            self._prepare_android12_boot_images(bootimgs_dir, artifacts)
        else:
            self._prepare_boot_images_generic(bootimgs_dir, artifacts)
        return artifacts

    def _prepare_android12_boot_images(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        fallback_url = "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-2023-01_r1.zip"
        # Only try the matching certified-boot archive when a revision was
        # actually supplied. Without this guard the f-string interpolated
        # the literal string "None" (--revision defaults to None, and most
        # matrix.json android12 entries carry no "revision" field), giving
        # a URL that can never resolve - so every such build quietly fell
        # back to the 2023-01_r1 ramdisk while looking like it had checked.
        if self.config.revision:
            gki_url = (f"https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-"
                       f"{self.config.os_patch_level}_{self.config.revision}.zip")
            result = subprocess.run(f"curl -sL -w '%{{http_code}}' {gki_url} -o /dev/null",
                                    shell=True, capture_output=True, text=True)
            if "200" in result.stdout:
                url = gki_url
            else:
                logger.warning(
                    f"No certified-boot archive for {self.config.os_patch_level}_"
                    f"{self.config.revision} - falling back to the generic "
                    f"2023-01_r1 ramdisk."
                )
                url = fallback_url
        else:
            logger.info(
                "No --revision given for this android12 build - using the "
                "generic 2023-01_r1 certified-boot ramdisk. Pass --revision "
                "(or add a \"revision\" field to this matrix.json entry) to "
                "use the archive matching this OS patch level."
            )
            url = fallback_url
        self._run_cmd(f"curl -Lo gki-kernel.zip {url} && unzip -o gki-kernel.zip && rm gki-kernel.zip", check=False)
        boot_img_path = bootimgs_dir / "boot-5.10.img"
        if boot_img_path.exists():
            self._run_cmd(f"$UNPACK_BOOTIMG --boot_img={boot_img_path}", check=False)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)

    def _prepare_boot_images_generic(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=False)

    def _create_boot_image_variants(self, bootimgs_dir: Path, artifacts: list, has_ramdisk: bool = False):
        self._chdir(bootimgs_dir)

        # Only the plain boot.img is packaged/uploaded - boot-gz.img and
        # boot-lz4.img variants are intentionally not produced.
        for kernel_file, output_file in [("Image", "boot.img")]:
            kernel_path = bootimgs_dir / kernel_file
            if not kernel_path.exists():
                logger.error(
                    f"{kernel_file} not found in {bootimgs_dir} - the compiled "
                    f"Image was not copied here, so no boot image can be built."
                )
                continue
            cmd = f"$MKBOOTIMG --header_version 4 --kernel {kernel_file} --output {output_file}"
            if has_ramdisk:
                cmd += f" --ramdisk out/ramdisk --os_version 12.0.0 --os_patch_level {self.config.os_patch_level}"
            self._run_cmd(cmd, check=False)
            if not (bootimgs_dir / output_file).exists():
                raise RuntimeError(
                    f"mkbootimg did not produce {output_file} in {bootimgs_dir}. "
                    f"Not reporting a boot image that was never created."
                )
            signed = self._run_cmd(
                f"$AVBTOOL add_hash_footer --partition_name boot "
                f"--partition_size $((64 * 1024 * 1024)) --image {output_file} "
                f"--algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH",
                check=False,
            )
            if signed.returncode != 0:
                raise RuntimeError(
                    f"avbtool failed to sign {output_file} (exit code "
                    f"{signed.returncode}). Every boot.img this pipeline "
                    f"publishes is AVB-signed; an unsigned one must not be "
                    f"handed out as if it were."
                )
            dest = self.work_dir / f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}{self.artifact_suffix}{self.respin_suffix}-{output_file}"
            self._run_cmd(f"cp {output_file} {dest}", check=False)
            if not dest.exists():
                raise RuntimeError(f"Failed to copy {output_file} to {dest}")
            logger.info(f"Boot image ready: {dest}")
            artifacts.append(str(dest))

    def create_anykernel_zips(self) -> list:
        logger.info("=== Creating AnyKernel3 ZIP files ===")
        self._chdir(self.work_dir)
        artifacts = []
        ak3_dir = self.anykernel_dir

        # Only the plain AnyKernel3.zip is packaged/uploaded - the
        # -lz4/-gz zip variants are intentionally not produced.
        for suffix in [""]:
            image_file = f"Image{suffix}"
            image_path = self.work_dir / image_file
            if not image_path.exists():
                logger.error(f"{image_file} not found in {self.work_dir} - skipping AnyKernel3 zip")
                continue
            zip_name = f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}{self.artifact_suffix}-AnyKernel3{self.respin_suffix}{suffix}.zip"
            # Written straight into work_dir. This used to be `zip -r
            # ../{zip_name}` from inside the shared AnyKernel3 checkout,
            # which resolves to the WORKSPACE root - one level above
            # work_dir - while the artifact path recorded below (and
            # build-kernel.sh's closing "Artifacts in:" line) both
            # pointed at work_dir. CI never noticed because it collects
            # with a recursive `find`; locally it meant the boot.img and
            # the flashable zip landed in two different directories.
            zip_dest = self.work_dir / zip_name
            self._run_cmd(f"cp {image_path} {ak3_dir}/", check=False)
            self._chdir(ak3_dir)
            self._run_cmd(f"zip -r {zip_dest} ./*", check=False)
            self._run_cmd(f"rm {ak3_dir}/{image_file}", check=False)
            self._chdir(self.work_dir)
            if not zip_dest.exists():
                raise RuntimeError(
                    f"AnyKernel3 zip was not created at {zip_dest} - not "
                    f"reporting an artifact that does not exist."
                )
            logger.info(f"AnyKernel3 zip ready: {zip_dest}")
            artifacts.append(str(zip_dest))
        return artifacts

    def build(self) -> BuildResult:
        import time
        start_time = time.time()
        logger.info("=" * 50)
        logger.info(f"Starting GKI Kernel build - {self.config.config_name}")
        logger.info("=" * 50)

        try:
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self._detect_kernel_respin()
            self._write_scmversion()
            self.apply_ptrace_leak_fix()
            self.apply_unicode_fix()
            if self.config.use_ntsync:
                self.apply_ntsync_patches()
            else:
                self._mark("ntsync", "skipped", "not requested")
            self.add_kernel_supatch()
            self.add_kernelsu()
            self._pin_ksu_version_code()
            self._check_kpm_bridge()
            self.add_bbg()
            self.apply_susfs_patches()
            self.apply_susfs_kernelsu_patch()
            self._fix_selinux_hide_always_true_guards()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            # Applied last, deliberately after SUSFS/SukiSU: Droidspaces'
            # kABI patch and SUSFS both potentially touch the same
            # ANDROID_KABI_RESERVE slots in include/linux/sched.h. If
            # Droidspaces ran first and claimed a slot SUSFS's patch
            # expects to find untouched, SUSFS - the core, non-optional
            # functionality of this whole project - could fail to apply.
            # Running Droidspaces last means if there IS a conflict, it's
            # this new optional feature that fails/gets skipped, never
            # SUSFS.
            self.apply_droidspaces_support()
            # Opt-in, needs the Droidspaces step above to have applied.
            self.apply_droidspaces_pids_slot()
            # BBRv3 touches net/ipv4/* only - no overlap with SUSFS/
            # Droidspaces' files, but applied here too for consistency
            # (all optional features settled before writing defconfig).
            self.apply_bbrv3_patches()
            # New driver dir + one line each in drivers/net/{Kconfig,Makefile}:
            # no overlap with SUSFS / Droidspaces / BBRv3.
            self.add_amneziawg()
            # Before configure_kernel(), like every other feature: it
            # edits Kconfig, and the fragment written below depends on
            # that edit having happened.
            self.apply_ath9k_patches()
            self._write_patch_status()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()

            if not self.build_kernel():
                return BuildResult(success=False, config=self.config, message="Kernel compilation failed", build_time=time.time() - start_time)

            self._verify_expected_objects()
            self._verify_effective_config()
            self._verify_image_ikconfig()
            ath9k_artifacts = self.collect_ath9k_modules()

            # Rewritten now that the build is done: the earlier call
            # happens before compilation (so a hard patch failure still
            # produces a status file), which meant peak_mem_gb was always
            # null in PATCH_STATUS.json even when BUILD_REPORT.txt had
            # the real figure, and post-build checks like
            # effective_config were never recorded at all.
            self._write_patch_status()

            self.patch_kpm_image()
            # Again after KPM: kpm_image_patch (which patcher, which
            # KernelPatch version) is only known now.
            self._write_patch_status()
            artifacts = []
            artifacts.extend(ath9k_artifacts)
            artifacts.extend(self.prepare_boot_images())
            artifacts.extend(self.create_anykernel_zips())

            build_time = time.time() - start_time
            logger.info(f"Build succeeded! Time: {build_time:.2f}s, generated {len(artifacts)} artifact(s)")
            return BuildResult(success=True, config=self.config, message="Build succeeded", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.error(f"Error during build: {e}")
            # Best-effort: persist whatever patch statuses were recorded
            # before the failure (e.g. a hard-stop SUSFS patch failure),
            # so the summary table still shows what did/didn't apply.
            try:
                self._write_patch_status()
            except Exception:
                pass
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.time() - start_time)
