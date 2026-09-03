#!/usr/bin/env python3
import argparse
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from config import BuildConfig, AndroidVersion, KernelVersion, ANDROID_KERNEL_MAP, KSUVersion
from kernel_builder import KernelBuilder, BuildResult

logging.basicConfig(
    level=logging.INFO,
    format='\033[92m[%(levelname)s]\033[0m %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# NOTE: there used to be a DEFAULT_BUILD_MATRIX dict here, plus --matrix/
# --all/--list-matrix flags that built from it. It was a second, unsynced
# source of truth, separate from matrix.json (which update_matrix.py keeps
# current from Google's own tags) and never actually used by build-kernel.sh,
# kernel-build.yml, or build-kernels.yml - all of them always pass explicit
# --android/--kernel/--sub-level/--os-patch/--kernel-tag. Removed to avoid
# it silently going stale again (e.g. missing android16/17, old respins).
# matrix.json is now the one and only source of truth for "which versions
# exist to build" - see update_matrix.py.


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GKI Kernel Build System")

    parser.add_argument("--android", "-a", choices=[v.value for v in AndroidVersion])
    parser.add_argument("--kernel", "-k", choices=[v.value for v in KernelVersion])
    parser.add_argument("--sub-level", "-s")
    parser.add_argument("--os-patch")
    parser.add_argument("--ksu-version", choices=[v.value for v in KSUVersion], default=KSUVersion.STABLE.value)
    parser.add_argument("--ksu-commit", default=None)
    parser.add_argument("--susfs-commit", default=None)
    parser.add_argument("--zram", action="store_true")
    parser.add_argument("--no-kpm", action="store_true")
    parser.add_argument("--no-mglru", action="store_true",
                        help="Disable MGLRU (Multi-Gen LRU memory reclaim). On by default.")
    parser.add_argument("--no-psi", action="store_true",
                        help="Disable PSI (Pressure Stall Information). On by default.")
    parser.add_argument("--no-ntsync", action="store_true",
                        help="Disable NTSync (Winlator/Wine NT sync primitives). On by default.")
    parser.add_argument("--no-hide-stuff", action="store_true",
                        help="Skip SukiSU_patch's 69_hide_stuff.patch (LineageOS/jit-zygote-cache "
                             "path spoofing). Currently stale against susfs4ksu's refactored "
                             "show_map_vma() and fails the build under -Werror=unused-*. On by "
                             "default (existing behavior) - pass this until upstream fixes it.")
    parser.add_argument("--bbg", action="store_true")
    parser.add_argument("--allow-bazel", action="store_true",
                        help="Allow building branches that require Bazel/Kleaf instead of the "
                             "legacy build/build.sh script (android15-6.6+, and some newer "
                             "android14-6.1 sub_levels that have already migrated). OFF by "
                             "default: without this flag, the build refuses to start on a "
                             "Bazel-only branch rather than silently building one. When this "
                             "IS passed, KMI symbol-list strict enforcement is left fully ON "
                             "(unlike some other community kernel builders) - if the patches "
                             "genuinely violate the KMI symbol list, the build will fail loudly "
                             "with the violation list rather than silently producing an Image "
                             "that may not be ABI-compatible with the device's vendor .ko "
                             "modules. A confirmed real-device bootloop was traced back to this "
                             "exact bypass, so it is not used here.")
    parser.add_argument("--blacklist-modules", default="",
                        help="Comma-separated list of vendor module names to block from loading "
                             "(CONFIG_DEBLOAT_VENDOR_MODULES). Auto-disables itself during "
                             "recovery/fastbootd boot so it never interferes with OTA/flashing. "
                             "Example: --blacklist-modules millet_binder,millet_hs,millet_oem_cgroup,millet_pkg,mi_cnss_statistic")
    parser.add_argument("--droidspaces", action="store_true",
                        help="Enable Droidspaces (github.com/ravindu644/Droidspaces-OSS) container-runtime "
                             "support - real Linux namespace isolation (PID/IPC/Mount) instead of a plain "
                             "chroot, so a full distro can run its own init (systemd/OpenRC). Only wired up "
                             "for kernel 5.10/5.15/6.1 (android12/13/14) so far.")
    parser.add_argument("--op8e", action="store_true")
    parser.add_argument("--ksm", action="store_true", help="Enable KSM (Kernel Samepage Merging)")
    parser.add_argument("--bbr-version", choices=["none", "bbr1", "bbr3"], default="bbr1")
    parser.add_argument("--lto-mode", choices=["thin", "full"], default="thin",
                        help="LLVM LTO mode for the legacy build.sh path (android12/android13). "
                             "'thin' (default) is faster and lower-memory; 'full' can squeeze out "
                             "slightly better runtime performance/code size at the cost of a much "
                             "longer, single-threaded, RAM-heavy link step. Ignored on Bazel/Kleaf "
                             "branches (android14-6.1+), where the mode is fixed upstream due to a "
                             "known ThinLTO verifier bug on some sub_levels.")
    parser.add_argument("--no-release", action="store_true")
    parser.add_argument("--custom-version", dest="custom_version", default=None)
    parser.add_argument("--revision")
    parser.add_argument("--kernel-tag", default=None,
                        help="Pin kernel/common to a specific respin tag (e.g. android13-5.15-2025-12_r10) "
                             "OR a raw commit SHA (7-40 hex chars, e.g. 12b3f6828b67824c794e422d5785dba6eb559bb2) "
                             "instead of the moving branch HEAD. A SHA is useful when an LTS-merge commit has "
                             "landed upstream but Google hasn't cut the official _r00 tag for it yet.")
    parser.add_argument("--disable-safemode", action="store_true",
                        help="Permanently disable KernelSU/SukiSU volume-key safe-mode detection "
                             "(most users rely on Yet Another Bootloop Protector instead)")
    parser.add_argument("--lts", action="store_true",
                        help="Mark this build as sourced from an LTS-merge respin tag "
                             "(e.g. android13-5.15.209_r00) - adds a '-lts' marker to the "
                             "output filenames so downstream users can tell it apart from a "
                             "regular date-based respin at a glance")
    parser.add_argument("--list-configs", action="store_true")
    parser.add_argument("--workspace", "-w", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output-json")
    parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def create_build_config(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(
        android_version=args.android or "android14",
        kernel_version=args.kernel or "6.1",
        sub_level=args.sub_level or "124",
        os_patch_level=args.os_patch or "2025-02",
        kernelsu_version=args.ksu_version,
        kernelsu_commit=args.ksu_commit,
        susfs_commit=args.susfs_commit,
        use_zram=args.zram,
        use_kpm=not args.no_kpm,
        use_mglru=not args.no_mglru,
        use_psi=not args.no_psi,
        use_ntsync=not args.no_ntsync,
        use_hide_stuff=not args.no_hide_stuff,
        use_bbg=args.bbg,
        allow_bazel=args.allow_bazel,
        blacklist_modules=args.blacklist_modules,
        use_droidspaces=args.droidspaces,
        support_op8e=args.op8e,
        enable_ksm=args.ksm,
        bbr_version=args.bbr_version,
        lto_mode=args.lto_mode,
        make_release=not args.no_release,
        custom_version=args.custom_version,
        revision=args.revision,
        kernel_tag=args.kernel_tag,
        disable_safemode=args.disable_safemode,
        is_lts_build=args.lts,
    )


def list_configs():
    print("\n" + "=" * 60)
    print("Supported Android/Kernel combinations")
    print("=" * 60)
    for android, kernels in ANDROID_KERNEL_MAP.items():
        print(f"  {android.value}: {', '.join(k.value for k in kernels)}")
    print("\nFor the actual sub_level/os_patch_level/kernel_tag matrix, see")
    print("matrix.json (kept current by update_matrix.py) - this list only")
    print("shows which android/kernel combinations config.py accepts.")
    print("\n" + "=" * 60)
    print("KernelSU version options")
    print("=" * 60)
    for v in KSUVersion:
        print(f"  - {v.value}")


def build_single(config: BuildConfig, workspace: str, dry_run: bool = False) -> BuildResult:
    if dry_run:
        logger.info(f"[DRY RUN] Validating config: {config.config_name}")
        return BuildResult(success=True, config=config, message="Config validation passed")

    builder = KernelBuilder(config, workspace)
    return builder.build()


def print_summary(results: list, output_json: str = None):
    total = len(results)
    success = sum(1 for r in results if r.success)

    print("\n" + "=" * 60)
    print("Build Summary")
    print("=" * 60)
    print(f"Total: {total}")
    print(f"Success: \033[92m{success}\033[0m")
    print(f"Failed: \033[91m{total - success}\033[0m")

    if success > 0:
        avg_time = sum(r.build_time or 0 for r in results if r.success) / success
        print(f"Average build time: {avg_time:.2f} sec")

    failed = total - success
    if failed > 0:
        print("\nFailed configs:")
        for r in results:
            if not r.success:
                print(f"  - {r.config.config_name}: {r.message}")
    print("=" * 60)

    if output_json:
        json_data = {
            "timestamp": datetime.now().isoformat(),
            "total": total,
            "success": success,
            "failed": failed,
            "results": [{"config": r.config.to_dict(), "success": r.success, "message": r.message,
                       "artifacts": r.artifacts, "build_time": r.build_time} for r in results]
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Results saved to: {output_json}")


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_configs:
        list_configs()
        return 0

    if not args.android:
        logger.error("Please specify --android (with --kernel, --sub-level, --os-patch)")
        return 1

    workspace = args.workspace
    logger.info(f"Workspace: {workspace}")
    os.makedirs(workspace, exist_ok=True)

    results = []

    try:
        config = create_build_config(args)
        result = build_single(config, workspace, args.dry_run)
        results.append(result)
    except Exception as e:
        logger.error(f"Config error: {e}")
        return 1

    if results:
        print_summary(results, args.output_json)

    if results and all(r.success for r in results):
        return 0
    elif results and any(r.success for r in results):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
