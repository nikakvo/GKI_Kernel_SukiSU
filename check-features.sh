#!/system/bin/sh
#
# check-features.sh - verify on-device what a GKI_Kernel_SukiSU build
# actually shipped.
#
#   github.com/nikakvo/GKI_Kernel_SukiSU
#
# Do NOT run this from /sdcard. Under `su` the root shell is in a
# different mount namespace, where /storage/emulated/0 is either invisible
# or mounted noexec - both `./script` and `sh /sdcard/script` fail there.
# Copy it somewhere on the data partition first:
#
#   Termux:      cp /sdcard/Download/check-features.sh ~
#                cd ~
#                chmod +x check-features.sh
#                su -c ./check-features.sh
#
#   adb:         adb push check-features.sh /data/local/tmp/
#                adb shell
#                cd /data/local/tmp && chmod +x check-features.sh
#                su -c ./check-features.sh
#
# Options:
#
#   su -c "./check-features.sh -v"          # show the measured value too
#   su -c "./check-features.sh --no-color"  # plain text, for pasting
#
# Statuses:
#   OK        feature is present and active
#   MISSING   expected, but not there - worth reporting
#   NOT BUILT this build did not include it (opt-in flag was off)
#   N/A       cannot be verified from the device (see the note)
#
# Exit code is 0 unless something is MISSING.

VERBOSE=0
COLOR=1
set -f   # no globbing: detail strings contain things like "[lz4kd]"
for a in "$@"; do
    case "$a" in
        -v|--verbose) VERBOSE=1 ;;
        --no-color)   COLOR=0 ;;
        -h|--help)    sed -n '3,34p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    esac
done

if [ "$COLOR" = 1 ]; then
    C_OK=$(printf '\033[32m'); C_BAD=$(printf '\033[31m')
    C_WARN=$(printf '\033[33m'); C_DIM=$(printf '\033[90m')
    C_HEAD=$(printf '\033[1;36m'); C_OFF=$(printf '\033[0m')
else
    C_OK=; C_BAD=; C_WARN=; C_DIM=; C_HEAD=; C_OFF=
fi

N_OK=0; N_BAD=0; N_OFF=0; N_NA=0

# ---------------------------------------------------------------- output

head_() { printf '\n%s== %s ==%s\n' "$C_HEAD" "$1" "$C_OFF"; }

# Detail lines are indented 11 columns; wrap the rest on word boundaries
# so a narrow phone terminal doesn't split them mid-word.
_WRAP=$(( ${COLUMNS:-64} - 11 ))
[ "$_WRAP" -lt 24 ] && _WRAP=24

detail_() {
    _line=
    for _w in $1; do
        if [ -z "$_line" ]; then
            _line=$_w
        elif [ $(( ${#_line} + 1 + ${#_w} )) -le "$_WRAP" ]; then
            _line="$_line $_w"
        else
            printf '           %s%s%s\n' "$C_DIM" "$_line" "$C_OFF"
            _line=$_w
        fi
    done
    [ -n "$_line" ] && printf '           %s%s%s\n' "$C_DIM" "$_line" "$C_OFF"
}

row() {  # row <status> <label> <detail>
    _s=$1; _l=$2; _d=$3
    case "$_s" in
        ok)   _c=$C_OK;   _t="OK       "; N_OK=$((N_OK+1)) ;;
        bad)  _c=$C_BAD;  _t="MISSING  "; N_BAD=$((N_BAD+1)) ;;
        off)  _c=$C_WARN; _t="NOT BUILT"; N_OFF=$((N_OFF+1)) ;;
        na)   _c=$C_DIM;  _t="N/A      "; N_NA=$((N_NA+1)) ;;
    esac
    printf '%s%s%s  %s\n' "$_c" "$_t" "$C_OFF" "$_l"
    if [ -n "$_d" ] && { [ "$VERBOSE" = 1 ] || [ "$_s" = bad ] || [ "$_s" = na ]; }; then
        detail_ "$_d"
    fi
}

# ------------------------------------------------------------ config.gz

for d in /data/local/tmp "$TMPDIR" /tmp .; do
    [ -n "$d" ] && [ -d "$d" ] && [ -w "$d" ] && { CFG="$d/.gki_config.$$"; break; }
done
trap 'rm -f "$CFG"' EXIT INT TERM

if [ -r /proc/config.gz ] && zcat /proc/config.gz > "$CFG" 2>/dev/null && [ -s "$CFG" ]; then
    HAVE_CFG=1
elif [ -r /proc/config ]; then
    cat /proc/config > "$CFG"; HAVE_CFG=1
else
    HAVE_CFG=0
fi

# cfg <SYMBOL> -> prints y/m/n, or nothing if the symbol is absent
cfg() {
    [ "$HAVE_CFG" = 1 ] || return 1
    _v=$(grep "^$1=" "$CFG" 2>/dev/null | head -1)
    [ -n "$_v" ] || return 1
    printf '%s' "${_v#*=}"
}

# cfg_is <SYMBOL> <value>
cfg_is() { [ "$(cfg "$1" 2>/dev/null)" = "$2" ]; }

# check_cfg <label> <SYMBOL> [expected]   expected defaults to y
check_cfg() {
    _lab=$1; _sym=$2; _exp=${3:-y}
    if [ "$HAVE_CFG" != 1 ]; then
        row na "$_lab" "/proc/config.gz not readable - cannot check $_sym"
        return
    fi
    _got=$(cfg "$_sym" 2>/dev/null)
    if [ "$_got" = "$_exp" ]; then
        row ok "$_lab" "$_sym=$_got"
    elif [ -z "$_got" ]; then
        row bad "$_lab" "$_sym is absent from the built config"
    else
        row bad "$_lab" "$_sym=$_got (expected $_exp)"
    fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------- header

printf '%s' "$C_HEAD"
echo "GKI_Kernel_SukiSU - on-device feature check"
printf '%s' "$C_OFF"
echo "$(date 2>/dev/null)"
echo
echo "Kernel : $(uname -r)"
echo "Build  : $(uname -v 2>/dev/null)"
[ -r /proc/version ] && echo "Device : $(getprop ro.product.model 2>/dev/null) / $(getprop ro.build.version.release 2>/dev/null)"

if [ "$(id -u 2>/dev/null)" != "0" ]; then
    printf '\n%sNot running as root - dmesg/tc/iptables checks will be skipped.%s\n' \
        "$C_WARN" "$C_OFF"
fi
if [ "$HAVE_CFG" != 1 ]; then
    printf '\n%s/proc/config.gz is not readable.%s\n' "$C_BAD" "$C_OFF"
    echo
    echo "Almost every check below reads the kernel's own embedded config,"
    echo "so without it the report would be a wall of false MISSING lines."
    echo "Stopping instead of guessing."
    echo
    echo "Usual causes: not running as root, or a kernel built without"
    echo "CONFIG_IKCONFIG_PROC (every build from this pipeline has it)."
    exit 2
fi

# ------------------------------------------------------- root & hiding

head_ "Root & hiding"

if cfg_is CONFIG_KSU y; then
    _sv=$(su -v 2>/dev/null)
    row ok "SukiSU-Ultra (KernelSU root)" "CONFIG_KSU=y${_sv:+; su -v: $_sv}"
else
    row bad "SukiSU-Ultra (KernelSU root)" "CONFIG_KSU is not y"
fi

if cfg_is CONFIG_KSU_SUSFS y; then
    _n=0; _miss=
    for s in SUS_PATH SUS_MAP SUS_MOUNT SUS_KSTAT SPOOF_UNAME ENABLE_LOG \
             HIDE_KSU_SUSFS_SYMBOLS SPOOF_CMDLINE_OR_BOOTCONFIG OPEN_REDIRECT; do
        if cfg_is "CONFIG_KSU_SUSFS_$s" y; then
            _n=$((_n+1))
        else
            _miss="$_miss $s"
        fi
    done
    if [ "$_n" = 9 ]; then
        row ok "SUSFS v2.3.0 (9/9 options)" "all sub-options enabled"
    else
        # SUS_PATH is deliberately off on 6.6 - not a fault there.
        row bad "SUSFS v2.3.0 ($_n/9 options)" "missing:$_miss"
    fi
else
    row bad "SUSFS v2.3.0" "CONFIG_KSU_SUSFS is not y - SUSFS is absent"
fi

# KPM. The KernelPatch version comes from ksud (needs root):
#   3328 = 0.13.0 (default patcher), 3337 = 0.13.9 (--kpm-next builds).
KSUD=/data/adb/ksud
KP_VER=; KP_DOT=; KP_RAW=
if cfg_is CONFIG_KPM y && [ -x "$KSUD" ] && [ "$(id -u)" = 0 ]; then
    # ksud prints e.g. "3337 (17:50:49 Oct 17 2026)" - number, then build time
    KP_RAW=$("$KSUD" kpm version 2>/dev/null | head -1)
    KP_VER=$(printf '%s' "$KP_RAW" | sed -n 's/^[^0-9]*\([0-9][0-9]*\).*/\1/p')
    [ -n "$KP_VER" ] && KP_DOT="$((KP_VER / 65536)).$(((KP_VER / 256) % 256)).$((KP_VER % 256))"
fi

if cfg_is CONFIG_KPM y; then
    if [ -n "$KP_DOT" ]; then
        row ok "KPM: KernelPatch $KP_DOT" "CONFIG_KPM=y; ksud kpm version: $KP_RAW"
    elif [ -n "$KP_RAW" ]; then
        row ok "KPM: KernelPatch" "CONFIG_KPM=y; ksud kpm version: $KP_RAW"
    elif [ "$(id -u)" != 0 ] || [ ! -x "$KSUD" ]; then
        row na "KPM (Kernel Patch Module)" \
            "CONFIG_KPM=y; version needs root and $KSUD - not available"
    else
        row bad "KPM (Kernel Patch Module)" \
            "CONFIG_KPM=y but '$KSUD kpm version' returns nothing - KernelPatch is not in this kernel image"
    fi
    _kn=$([ -x "$KSUD" ] && "$KSUD" kpm num 2>/dev/null | tr -d '[:space:]')
    case "$_kn" in
        ''|*[!0-9]*) ;;
        *) row ok "KPM: loaded modules" "$_kn KPM(s) loaded (ksud kpm num)" ;;
    esac
else
    row off "KPM (Kernel Patch Module)" "CONFIG_KPM not enabled in this build"
fi

# BTF lets KPMs look up kernel structures and offsets (KernelPatch 0.13.9+).
if cfg_is CONFIG_KPM y; then
    if [ -n "$KP_VER" ] && [ "$KP_VER" -lt 3337 ] 2>/dev/null; then
        row off "KPM: BTF for KPMs" \
            "KernelPatch $KP_DOT - BTF support for KPMs starts with 0.13.9 (--kpm-next builds)"
    elif [ -r /sys/kernel/btf/vmlinux ]; then
        row ok "KPM: BTF for KPMs" "/sys/kernel/btf/vmlinux present"
    elif cfg_is CONFIG_DEBUG_INFO_BTF y; then
        row bad "KPM: BTF for KPMs" "CONFIG_DEBUG_INFO_BTF=y but /sys/kernel/btf/vmlinux is missing"
    else
        row off "KPM: BTF for KPMs" "CONFIG_DEBUG_INFO_BTF not enabled"
    fi
fi

# Module preinit. Available in SUSFS builds since v41038; earlier ones
# compile it out (-DKSU_NO_PREINIT). Only visible in the boot log.
_pre=$(dmesg 2>/dev/null | grep -iE 'preinit: (runner staged|exec runner)|KSU_NO_PREINIT' | tail -1)
case "$_pre" in
    *KSU_NO_PREINIT*)
        row off "Module preinit" "compiled out in this build (KSU_NO_PREINIT)" ;;
    *preinit:*)
        row ok "Module preinit" "boot log: ${_pre#*] }" ;;
    *)
        row na "Module preinit" \
            "no preinit lines in dmesg - needs root, and the ring buffer may have rolled since boot" ;;
esac

if grep -q ' /debug_ramdisk ' /proc/mounts 2>/dev/null; then
    row ok "Magic Mount" "/debug_ramdisk is mounted"
else
    row na "Magic Mount" \
        "userspace feature, no kernel symbol - only visible once a root module is active"
fi

row na "Safe Mode Removal" \
    "source patch, leaves no runtime marker; off by default in this pipeline (--disable-safemode)"

# ---------------------------------------------------------- networking

head_ "Networking"

_cc=$(cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null)
_avail=$(cat /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null)
case "$_cc" in
    bbr3) row ok "BBR v3 (default algorithm)" "tcp_congestion_control=bbr3" ;;
    bbr)  row bad "BBR v3 (default algorithm)" \
              "tcp_congestion_control=bbr - this is BBRv1, the v3 patch did not land" ;;
    "")   row na  "BBR v3 (default algorithm)" "could not read tcp_congestion_control" ;;
    *)    row bad "BBR v3 (default algorithm)" "tcp_congestion_control=$_cc" ;;
esac

_ccmiss=
for a in bic westwood htcp; do
    case " $_avail " in *" $a "*) ;; *) _ccmiss="$_ccmiss $a" ;; esac
done
if [ -z "$_avail" ]; then
    row na "Extra TCP CC (BIC/Westwood/H-TCP)" "could not read available algorithms"
elif [ -z "$_ccmiss" ]; then
    row ok "Extra TCP CC (BIC/Westwood/H-TCP)" "available: $_avail"
elif cfg_is CONFIG_TCP_CONG_BIC n; then
    row off "Extra TCP CC (BIC/Westwood/H-TCP)" "built without --extra-net"
else
    row bad "Extra TCP CC (BIC/Westwood/H-TCP)" "missing:$_ccmiss (have: $_avail)"
fi

if cfg_is CONFIG_NET_SCH_CAKE y; then
    if have tc && [ "$(id -u)" = 0 ]; then
        if tc qdisc add dev lo root cake >/dev/null 2>&1; then
            tc qdisc del dev lo root >/dev/null 2>&1
            row ok "CAKE qdisc" "CONFIG_NET_SCH_CAKE=y; attached to lo successfully"
        else
            row bad "CAKE qdisc" "CONFIG_NET_SCH_CAKE=y but 'tc qdisc add ... cake' failed"
        fi
    else
        row ok "CAKE qdisc" "CONFIG_NET_SCH_CAKE=y (no tc binary - runtime test skipped)"
    fi
else
    row bad "CAKE qdisc" "CONFIG_NET_SCH_CAKE is not y"
fi

check_cfg "WireGuard" CONFIG_WIREGUARD

# AmneziaWG, built in next to WireGuard. Kernels before it was added
# simply don't have the symbol: NOT BUILT, not MISSING.
_awg=$(cfg CONFIG_AMNEZIAWG 2>/dev/null)
if [ "$HAVE_CFG" != 1 ]; then
    row na "AmneziaWG" "/proc/config.gz not readable - cannot check CONFIG_AMNEZIAWG"
elif [ -z "$_awg" ]; then
    row off "AmneziaWG" "CONFIG_AMNEZIAWG not in this kernel (older build, or built with --no-amneziawg)"
elif [ "$_awg" != y ]; then
    row bad "AmneziaWG" "CONFIG_AMNEZIAWG=$_awg (must be =y, built in)"
else
    _awgver=$(cat /sys/module/amneziawg/version 2>/dev/null)
    if [ -z "$_awgver" ]; then
        row bad "AmneziaWG" "CONFIG_AMNEZIAWG=y but /sys/module/amneziawg/version is missing"
    elif have ip && [ "$(id -u)" = 0 ]; then
        if ip link add gkichk_awg type amneziawg >/dev/null 2>&1; then
            ip link del gkichk_awg >/dev/null 2>&1
            row ok "AmneziaWG $_awgver" "CONFIG_AMNEZIAWG=y; amneziawg interface created and removed"
        else
            row bad "AmneziaWG $_awgver" "CONFIG_AMNEZIAWG=y but 'ip link add ... type amneziawg' failed"
        fi
    else
        row ok "AmneziaWG $_awgver" "CONFIG_AMNEZIAWG=y (runtime test skipped - no ip binary or not root)"
    fi
fi

if cfg_is CONFIG_IP_SET y; then
    if have ipset && [ "$(id -u)" = 0 ]; then
        if ipset create gkichk hash:ip >/dev/null 2>&1; then
            ipset destroy gkichk >/dev/null 2>&1
            row ok "IP Set (netfilter)" "CONFIG_IP_SET=y; set created and destroyed"
        else
            row bad "IP Set (netfilter)" "CONFIG_IP_SET=y but 'ipset create' failed"
        fi
    else
        row ok "IP Set (netfilter)" \
            "CONFIG_IP_SET=y (no ipset binary - see github.com/nikakvo/ipset-arm64)"
    fi
else
    row bad "IP Set (netfilter)" "CONFIG_IP_SET is not y"
fi

if cfg_is CONFIG_IP_NF_TARGET_TTL y || cfg_is CONFIG_NETFILTER_XT_TARGET_HL y; then
    if have iptables && [ "$(id -u)" = 0 ] &&
       iptables -t mangle -A POSTROUTING -j TTL --ttl-set 65 >/dev/null 2>&1; then
        iptables -t mangle -D POSTROUTING -j TTL --ttl-set 65 >/dev/null 2>&1
        row ok "TTL / Hop-Limit target" "rule added and removed successfully"
    else
        row ok "TTL / Hop-Limit target" \
            "CONFIG_IP_NF_TARGET_TTL=$(cfg CONFIG_IP_NF_TARGET_TTL 2>/dev/null) (runtime test skipped)"
    fi
else
    row bad "TTL / Hop-Limit target" "neither IP_NF_TARGET_TTL nor NETFILTER_XT_TARGET_HL is y"
fi

if cfg_is CONFIG_NETFILTER_XT_TARGET_CONNMARK y; then
    if have iptables && [ "$(id -u)" = 0 ] &&
       iptables -t mangle -A POSTROUTING -j CONNMARK --set-mark 1 >/dev/null 2>&1; then
        iptables -t mangle -D POSTROUTING -j CONNMARK --set-mark 1 >/dev/null 2>&1
        row ok "Connection mark (connmark)" "rule added and removed successfully"
    else
        row ok "Connection mark (connmark)" \
            "CONFIG_NETFILTER_XT_TARGET_CONNMARK=y (runtime test skipped)"
    fi
else
    row bad "Connection mark (connmark)" "CONFIG_NETFILTER_XT_TARGET_CONNMARK is not y"
fi

if grep -qw cifs /proc/filesystems 2>/dev/null; then
    row ok "CIFS/SMB client" "cifs listed in /proc/filesystems"
elif cfg_is CONFIG_CIFS y; then
    row ok "CIFS/SMB client" "CONFIG_CIFS=y (not yet registered in /proc/filesystems)"
elif [ -z "$(cfg CONFIG_CIFS 2>/dev/null)" ] && [ -z "$(cfg CONFIG_WIREGUARD 2>/dev/null)" ]; then
    row off "CIFS/SMB client" "built without --extra-net"
else
    row bad "CIFS/SMB client" "CONFIG_CIFS is not y and cifs is not in /proc/filesystems"
fi

if cfg_is CONFIG_NF_TABLES y; then
    row ok "nftables (chroot iptables shim)" "CONFIG_NF_TABLES=y"
else
    row off "nftables (chroot iptables shim)" "built without --extra-net"
fi

if cfg_is CONFIG_IP6_NF_NAT y; then
    row ok "IPv6 NAT (ip6tables nat)" "CONFIG_IP6_NF_NAT=y"
else
    row off "IPv6 NAT (ip6tables nat)" "built without --extra-net"
fi

# --------------------------------------------------- memory & performance

head_ "Memory & performance"

_alg=$(cat /sys/block/zram0/comp_algorithm 2>/dev/null)
if [ -n "$_alg" ]; then
    case "$_alg" in
        *"[lz4kd]"*) row ok  "ZRAM + LZ4KD" "comp_algorithm: $_alg" ;;
        *lz4kd*)     row bad "ZRAM + LZ4KD" "lz4kd available but not selected: $_alg" ;;
        *)           row bad "ZRAM + LZ4KD" "lz4kd not offered: $_alg" ;;
    esac
elif cfg_is CONFIG_CRYPTO_LZ4KD y; then
    row ok "ZRAM + LZ4KD" "CONFIG_CRYPTO_LZ4KD=y (zram0 not active yet)"
else
    row off "ZRAM + LZ4KD" "built without --zram"
fi

_lru=$(cat /sys/kernel/mm/lru_gen/enabled 2>/dev/null)
if [ -z "$_lru" ]; then
    if cfg_is CONFIG_LRU_GEN y; then
        row bad "MGLRU (Multi-Gen LRU)" "CONFIG_LRU_GEN=y but /sys/kernel/mm/lru_gen missing"
    else
        row off "MGLRU (Multi-Gen LRU)" "CONFIG_LRU_GEN not enabled"
    fi
elif [ "$_lru" = "0x0000" ] || [ "$_lru" = "0" ]; then
    row bad "MGLRU (Multi-Gen LRU)" "present but disabled at runtime (enabled=$_lru)"
else
    row ok "MGLRU (Multi-Gen LRU)" "enabled=$_lru"
fi

if [ -r /proc/pressure/memory ] && grep -q avg10 /proc/pressure/memory 2>/dev/null; then
    row ok "PSI (pressure stall info)" "$(head -1 /proc/pressure/memory)"
elif cfg_is CONFIG_PSI y; then
    row bad "PSI (pressure stall info)" "CONFIG_PSI=y but /proc/pressure/memory is unreadable"
else
    row off "PSI (pressure stall info)" "CONFIG_PSI not enabled"
fi

if cfg_is CONFIG_LTO_CLANG_FULL y; then
    row ok "LTO: Full" "CONFIG_LTO_CLANG_FULL=y (local/release build)"
elif cfg_is CONFIG_LTO_CLANG_THIN y; then
    row ok "LTO: Thin" "CONFIG_LTO_CLANG_THIN=y (CI build - Full needs ~27 GB RAM)"
elif cfg_is CONFIG_LTO_NONE y; then
    row bad "LTO" "CONFIG_LTO_NONE=y - this kernel was built with LTO disabled"
else
    row bad "LTO" "neither LTO_CLANG_FULL nor LTO_CLANG_THIN is y"
fi

# ------------------------------------------------ containers & compat

head_ "Containers & compatibility"

_dsmiss=
for s in CONFIG_SYSVIPC CONFIG_POSIX_MQUEUE CONFIG_IPC_NS CONFIG_PID_NS \
         CONFIG_USER_NS CONFIG_DEVTMPFS; do
    cfg_is "$s" y || _dsmiss="$_dsmiss ${s#CONFIG_}"
done
if [ -n "$_dsmiss" ]; then
    row bad "Droidspaces (namespace isolation)" "missing:$_dsmiss"
elif unshare -pf echo ok >/dev/null 2>&1; then
    row ok "Droidspaces (namespace isolation)" "all 6 options =y; unshare -pf works"
else
    row ok "Droidspaces (namespace isolation)" \
        "all 6 options =y (unshare test skipped - no unshare binary or not root)"
fi

# --net=macvlan. Added in v40993; only built together with Droidspaces.
if [ -n "$_dsmiss" ]; then
    row off "Droidspaces macvlan networking" "needs a --droidspaces build"
elif cfg_is CONFIG_MACVLAN y; then
    if have ip && [ "$(id -u)" = 0 ] && [ -e /sys/class/net/wlan0 ] &&
       ip link add gkichk_mv link wlan0 type macvlan mode bridge >/dev/null 2>&1; then
        ip link del gkichk_mv >/dev/null 2>&1
        row ok "Droidspaces macvlan networking" \
            "CONFIG_MACVLAN=y; macvlan on wlan0 created and removed"
    else
        row ok "Droidspaces macvlan networking" \
            "CONFIG_MACVLAN=y (runtime test skipped - no ip binary, no wlan0 or not root)"
    fi
else
    row off "Droidspaces macvlan networking" \
        "CONFIG_MACVLAN not enabled - builds before v40993 did not include it"
fi

# --pids-limit. Experimental opt-in build (--droidspaces-pids): pids takes
# net_prio's cgroup id, so CONFIG_CGROUP_NET_PRIO must be off with it.
if cfg_is CONFIG_CGROUP_PIDS y; then
    if cfg_is CONFIG_CGROUP_NET_PRIO y; then
        row bad "Droidspaces pids limit (experimental)" \
            "CONFIG_CGROUP_PIDS=y together with CONFIG_CGROUP_NET_PRIO=y - one cgroup subsystem too many for the GKI kABI"
    elif [ -r /sys/fs/cgroup/cgroup.controllers ]; then
        if grep -qw pids /sys/fs/cgroup/cgroup.controllers 2>/dev/null; then
            row ok "Droidspaces pids limit (experimental)" \
                "CONFIG_CGROUP_PIDS=y; pids listed in /sys/fs/cgroup/cgroup.controllers"
        else
            row bad "Droidspaces pids limit (experimental)" \
                "CONFIG_CGROUP_PIDS=y but pids is not in /sys/fs/cgroup/cgroup.controllers"
        fi
    else
        row ok "Droidspaces pids limit (experimental)" \
            "CONFIG_CGROUP_PIDS=y (cgroup v2 root not readable - runtime test skipped)"
    fi
else
    row off "Droidspaces pids limit (experimental)" \
        "CONFIG_CGROUP_PIDS not enabled - opt-in test builds only (--droidspaces-pids)"
fi

if [ -e /dev/ntsync ]; then
    row ok "NTSync (Winlator/Wine)" "/dev/ntsync present"
elif cfg_is CONFIG_NTSYNC y; then
    row bad "NTSync (Winlator/Wine)" "CONFIG_NTSYNC=y but /dev/ntsync does not exist"
else
    row off "NTSync (Winlator/Wine)" "CONFIG_NTSYNC not enabled on this branch"
fi

# ------------------------------------------------------------ security

head_ "Security"

if cfg_is CONFIG_BBG y; then
    _bbg=$(dmesg 2>/dev/null | grep -c baseband_guard)
    if [ "${_bbg:-0}" -gt 0 ] 2>/dev/null; then
        row ok "Baseband-guard (BBG)" "CONFIG_BBG=y; $_bbg dmesg lines"
    else
        row ok "Baseband-guard (BBG)" \
            "CONFIG_BBG=y (dmesg shows no baseband_guard lines - needs root, and the ring buffer may have rolled)"
    fi
else
    row off "Baseband-guard (BBG)" "built without --bbg"
fi

row na "Ptrace leak fix (<5.16)" "internal timing fix, no runtime flag - see the build's PATCH_STATUS"
row na "Unicode normalization fix" "internal correctness fix, no runtime flag - see PATCH_STATUS"

# --------------------------------------------------- external hardware

head_ "External hardware"

_ath=$(cfg CONFIG_ATH9K_HTC 2>/dev/null)
if [ "$_ath" = m ]; then
    if lsmod 2>/dev/null | grep -q ath9k_htc; then
        _iw=$(iw dev 2>/dev/null | grep -c Interface)
        row ok "ath9k_htc: module loaded" "CONFIG_ATH9K_HTC=m; $_iw wireless interface(s)"
    else
        row ok "ath9k_htc: built, not loaded" \
            "CONFIG_ATH9K_HTC=m - flash the ath9k module and plug the adapter in"
    fi
elif [ "$_ath" = y ]; then
    row bad "ath9k_htc (TL-WN722N v1)" \
        "CONFIG_ATH9K_HTC=y - must be =m; built-in kills the internal WiFi"
else
    row off "ath9k_htc (TL-WN722N v1)" "built without --ath9k"
fi

# ------------------------------------------------------------- summary

printf '\n%s== Summary ==%s\n' "$C_HEAD" "$C_OFF"
printf '%sOK %d%s   %sMISSING %d%s   %sNOT BUILT %d%s   %sN/A %d%s\n' \
    "$C_OK" "$N_OK" "$C_OFF" "$C_BAD" "$N_BAD" "$C_OFF" \
    "$C_WARN" "$N_OFF" "$C_OFF" "$C_DIM" "$N_NA" "$C_OFF"

if [ "$N_BAD" -gt 0 ]; then
    printf '\n%s%d feature(s) expected but not present.%s\n' "$C_BAD" "$N_BAD" "$C_OFF"
    echo "Re-run with -v for detail, and compare against this build's PATCH_STATUS.json."
    exit 1
fi

printf '\n%sNothing missing.%s ' "$C_OK" "$C_OFF"
echo "NOT BUILT items are flags that were off for this build, not faults."
exit 0
