from dataclasses import dataclass
from typing import Optional
from enum import Enum
import re
import urllib.request
import ssl


def get_susfs_version() -> str:
    """Fetch the version number from the susfs repository"""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # Try multiple branches to get the version number
    branches = ["gki-android15-6.6", "gki-android14-6.1", "gki-android13-5.15", "gki-android12-5.10", "main"]
    version_pattern = re.compile(r'#define\s+SUSFS_VERSION\s+"([^"]+)"')

    for branch in branches:
        try:
            url = f"https://raw.githubusercontent.com/ShirkNeko/susfs4ksu/{branch}/kernel_patches/include/linux/susfs.h"
            req = urllib.request.Request(url, headers={'User-Agent': 'Python'})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as response:
                content = response.read().decode('utf-8')
                match = version_pattern.search(content)
                if match:
                    return match.group(1)
        except Exception:
            continue

    # If fetching fails, return the default value
    return "v2.1.0"


# Kernel version - automatically fetched from the susfs repository
KERNEL_VERSION = get_susfs_version()
print(f"SUSFS Version: {KERNEL_VERSION}")


class AndroidVersion(Enum):
    ANDROID12 = "android12"
    ANDROID13 = "android13"
    ANDROID14 = "android14"
    ANDROID15 = "android15"
    ANDROID16 = "android16"
    ANDROID17 = "android17"


class KernelVersion(Enum):
    KERNEL_5_10 = "5.10"
    KERNEL_5_15 = "5.15"
    KERNEL_6_1 = "6.1"
    KERNEL_6_6 = "6.6"
    KERNEL_6_12 = "6.12"
    KERNEL_6_18 = "6.18"


class KSUVersion(Enum):
    STABLE = "Stable"
    DEV = "Dev"


ANDROID_KERNEL_MAP = {
    AndroidVersion.ANDROID12: [KernelVersion.KERNEL_5_10],
    AndroidVersion.ANDROID13: [KernelVersion.KERNEL_5_10, KernelVersion.KERNEL_5_15],
    AndroidVersion.ANDROID14: [KernelVersion.KERNEL_5_15, KernelVersion.KERNEL_6_1],
    AndroidVersion.ANDROID15: [KernelVersion.KERNEL_6_6],
    AndroidVersion.ANDROID16: [KernelVersion.KERNEL_6_12],
    AndroidVersion.ANDROID17: [KernelVersion.KERNEL_6_18],
}

# Repository configuration
KSU_REPO_CONFIG = {"repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
                    "branch": "main",
                    "setup_script": "https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/kernel/setup.sh"}

# SUSFS repository configuration
SUSFS_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/susfs4ksu.git"}

# SukiSU Patch repository configuration
SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git"}

# AnyKernel3 repository configuration
ANYKERNEL_CONFIG = {"repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git", "branch": "gki-2.0"}

# Kernel Patches repository configuration
KERNEL_PATCHES_CONFIG = {"repo_url": "https://github.com/Tools-cx-app/kernel_patches.git"}

# Baseband-guard configuration
BBG_CONFIG = {"repo_url": "https://github.com/vc-teahouse/Baseband-guard.git",
              "setup_script": "https://github.com/vc-teahouse/Baseband-guard/raw/main/setup.sh"}

# Toolchain configuration
TOOLCHAIN_CONFIG = {"aosp_mirror": "https://android.googlesource.com",
                    "build_tools_branch": "main-kernel-build-2024",
                    "mkbootimg_branch": "main-kernel-build-2024"}
LEGACY_FIXES = {
    "android13-5.15-below-123": {"url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fix_5.15.legacy", "min_sub_level": 123},
    "android12-5.10-below-136": {"url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fdinfo.c.patch", "min_sub_level": 136},
}
OP8E_PATCH_URL = "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/dev/hmbird_patch.c"
KPM_PATCH_URL = "https://raw.githubusercontent.com/ShirkNeko/SukiSU_patch/refs/heads/main/kpm/patch_linux"


@dataclass
class BuildConfig:
    android_version: str
    kernel_version: str
    sub_level: str
    os_patch_level: str
    kernelsu_version: str = "Stable"
    kernelsu_commit: Optional[str] = None
    susfs_commit: Optional[str] = None
    use_zram: bool = False
    use_kpm: bool = True
    use_mglru: bool = True
    use_psi: bool = True
    use_ntsync: bool = True
    use_bbg: bool = False
    allow_bazel: bool = False
    blacklist_modules: str = ""
    use_droidspaces: bool = False
    support_op8e: bool = False
    enable_ksm: bool = False
    bbr_version: str = "bbr1"
    lto_mode: str = "thin"
    make_release: bool = True
    custom_version: Optional[str] = None
    revision: Optional[str] = None
    kernel_tag: Optional[str] = None
    disable_safemode: bool = False
    build_id: Optional[str] = None
    is_lts_build: bool = False
    # Whether to apply SukiSU_patch's 69_hide_stuff.patch (LineageOS/
    # jit-zygote-cache /proc path spoofing - cosmetic root-hiding extra,
    # not core SUSFS/SukiSU functionality). Default True to preserve
    # existing behavior, but this patch is currently stale relative to
    # susfs4ksu's refactored show_map_vma() (the "bypass_orig_flow:"
    # label it expects there was removed upstream), so `patch -F 3`
    # fuzzy-matches onto an unrelated label in a different function and
    # produces dead code that fails the build under -Werror=unused-*.
    # Set to False (--no-hide-stuff) to skip it until ShirkNeko/
    # SukiSU_patch updates it to match current susfs4ksu.
    use_hide_stuff: bool = True

    def __post_init__(self):
        self._validate_android_version()
        self._validate_kernel_version()
        self._validate_kernel_android_compat()
        self._validate_sub_level()
        self._validate_bbr_version()
        self._validate_lto_mode()
        self._set_build_id()

    def _validate_android_version(self):
        valid = [v.value for v in AndroidVersion]
        if self.android_version not in valid:
            raise ValueError(f"Invalid Android version: {self.android_version}. Supported: {', '.join(valid)}")

    def _validate_kernel_version(self):
        valid = [v.value for v in KernelVersion]
        if self.kernel_version not in valid:
            raise ValueError(f"Invalid Kernel version: {self.kernel_version}. Supported: {', '.join(valid)}")

    def _validate_kernel_android_compat(self):
        av = AndroidVersion(self.android_version)
        kv = KernelVersion(self.kernel_version)
        if kv not in ANDROID_KERNEL_MAP.get(av, []):
            raise ValueError(f"Android {self.android_version} does not support Kernel {self.kernel_version}")

    def _validate_sub_level(self):
        if self.sub_level != "X" and not self.sub_level.isdigit():
            raise ValueError(f"Invalid sub_level: {self.sub_level}")

    def _validate_bbr_version(self):
        valid = ("none", "bbr1", "bbr3")
        if self.bbr_version not in valid:
            raise ValueError(f"Invalid bbr_version: {self.bbr_version}. Supported: {', '.join(valid)}")

    def _validate_lto_mode(self):
        valid = ("thin", "full")
        if self.lto_mode not in valid:
            raise ValueError(f"Invalid lto_mode: {self.lto_mode}. Supported: {', '.join(valid)}")

    def _set_build_id(self):
        if self.build_id is None:
            self.build_id = f"{self.android_version}-{self.kernel_version}-{self.sub_level}-{self.os_patch_level}"

    @property
    def config_name(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.sub_level}"

    @property
    def formatted_branch(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.os_patch_level}"

    @property
    def kernel_branch(self) -> str:
        return f"gki-{self.android_version}-{self.kernel_version}"

    def get_susfs_patch_filename(self) -> str:
        return f"50_add_susfs_in_gki-{self.android_version}-{self.kernel_version}.patch"

    def is_lts(self) -> bool:
        return self.sub_level == "X"

    def get_sub_level_int(self) -> Optional[int]:
        return None if self.sub_level == "X" else int(self.sub_level)

    def to_dict(self) -> dict:
        return {
            "android_version": self.android_version,
            "kernel_version": self.kernel_version,
            "sub_level": self.sub_level,
            "os_patch_level": self.os_patch_level,
            "kernelsu_version": self.kernelsu_version,
            "kernelsu_commit": self.kernelsu_commit,
            "use_zram": self.use_zram,
            "use_kpm": self.use_kpm,
            "use_mglru": self.use_mglru,
            "use_psi": self.use_psi,
            "use_ntsync": self.use_ntsync,
            "use_bbg": self.use_bbg,
            "allow_bazel": self.allow_bazel,
            "blacklist_modules": self.blacklist_modules,
            "use_droidspaces": self.use_droidspaces,
            "support_op8e": self.support_op8e,
            "enable_ksm": self.enable_ksm,
            "bbr_version": self.bbr_version,
            "make_release": self.make_release,
            "custom_version": self.custom_version,
            "revision": self.revision,
            "kernel_tag": self.kernel_tag,
            "disable_safemode": self.disable_safemode,
            "build_id": self.build_id,
        }


def validate_commit_hash(commit_hash: str) -> bool:
    return bool(re.match(r'^[0-9a-f]{7,40}$', commit_hash, re.IGNORECASE))
