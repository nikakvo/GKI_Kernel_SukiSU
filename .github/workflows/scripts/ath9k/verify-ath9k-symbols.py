#!/usr/bin/env python3
"""
Проверява дали билднатите ath9k модули могат да се заредят срещу
вендорския wireless стек на устройството — ПРЕДИ да се флашне нещо.

ВАЖНО, установено на реално устройство (Poco F6 Pro, 5.15.211-r00-lts):
този кернел НЕ налага CRC проверката при зареждане на модули. Доказано
е директно — вендорските cfg80211.ko/mac80211.ko са заредени и работят,
въпреки че имат съответно 54 и 130 символа с CRC-та, които не съвпадат
с този кернел. Нашият ath.ko също се зареди с три разминати CRC-та.
Затова разминаването е ПРЕДУПРЕЖДЕНИЕ, не отказ.

Причината за разминаването е дрейф в декларациите между 5.15.78 (срещу
който Qualcomm е компилирал) и 5.15.211 — 133 LTS издания преименувани
полета и променени типове, които местят genksyms хеша, без да местят
нито един офсет. Че layout-ът наистина не се е променил, се вижда от
самия вендорски стек: той обменя sk_buff и net_device с този vmlinux
непрекъснато и WiFi работи.

Това, което ОСТАВА фатално, е символ, който никой не предоставя —
нито vmlinux (след TRIM_UNUSED_KSYMS), нито вендорските модули, нито
нашите. Той дава "Unknown symbol" при insmod и модулът не се зарежда.
Точно това се случи при първия тест: ath9k_htc.ko поиска usb_get_urb и
rfkill_pause_polling, а старият кернел ги беше отрязал, защото нито
един in-tree модул не ги ползваше.

Изходен код: 0 = чисто, 1 = фатално, 2 = само предупреждения.
"""

import argparse
import re
import struct
import sys
from pathlib import Path

VENDOR_MODULES = {"cfg80211", "mac80211"}
OURS_PREFIX = ("ath", "ath9k")

# struct modversion_info { unsigned long crc; char name[MODULE_NAME_LEN]; }
REC = 64
NAME_LEN = REC - 8


def elf_section(ko: Path, want: str):
    """Минимален ELF64 четец — без objcopy, за да работи на всяка хост
    архитектура (модулите са aarch64, хостът обикновено не е)."""
    b = ko.read_bytes()
    if b[:4] != b"\x7fELF" or b[4] != 2:
        raise SystemExit(f"{ko}: не е ELF64")
    little = b[5] == 1
    e = "<" if little else ">"
    shoff, = struct.unpack_from(e + "Q", b, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", b, 0x3A)

    def sh(i):
        o = shoff + i * shentsize
        name, = struct.unpack_from(e + "I", b, o)
        off, size = struct.unpack_from(e + "QQ", b, o + 0x18)
        return name, off, size

    _, stroff, _ = sh(shstrndx)
    for i in range(shnum):
        nameoff, off, size = sh(i)
        end = b.index(b"\0", stroff + nameoff)
        if b[stroff + nameoff:end].decode() == want:
            return b[off:off + size], e
    return None, e


def read_versions(ko: Path):
    """Връща [(name, crc), ...] от секцията __versions на модула."""
    blob, e = elf_section(ko, "__versions")

    if not blob:
        raise SystemExit(
            f"{ko.name}: няма секция __versions.\n"
            "  Това значи билд БЕЗ CONFIG_MODVERSIONS. Такъв модул няма да\n"
            "  бъде проверен от кернела, но и няма да е съвместим по случайност —\n"
            "  спри и оправи конфигурацията, вместо да го зареждаш.")
    if len(blob) % REC:
        raise SystemExit(f"{ko.name}: __versions не е кратно на {REC} байта")

    out = []
    for off in range(0, len(blob), REC):
        crc, = struct.unpack_from(e + "Q", blob, off)
        name = blob[off + 8:off + REC].split(b"\0", 1)[0].decode()
        out.append((name, crc))
    return out


def read_symvers(path: Path):
    """symbol -> кой обект го предоставя (базово име без път/разширение)."""
    prov = {}
    for line in path.read_text(errors="replace").splitlines():
        f = line.split("\t")
        if len(f) < 3:
            f = line.split()
        if len(f) < 3:
            continue
        sym, mod = f[1], f[2]
        prov[sym] = Path(mod).name.replace(".ko", "") or "vmlinux"
    return prov


def read_vendor(path: Path):
    """Таблицата с вендорските експорти: symbol -> crc."""
    tab = {}
    pat = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+([0-9A-Fa-f]{1,16})\s*$")
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = pat.match(line)
        if m:
            tab[m.group(1)] = int(m.group(2), 16)
    return tab


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symvers", required=True, type=Path)
    ap.add_argument("--vendor-crc", required=True, type=Path)
    ap.add_argument("modules", nargs="+", type=Path)
    a = ap.parse_args()

    prov = read_symvers(a.symvers)
    vendor = read_vendor(a.vendor_crc)
    print(f"Module.symvers: {len(prov)} символа | "
          f"вендорска таблица: {len(vendor)} символа\n")

    mismatch, unknown, unresolved, checked = [], [], [], set()
    fatal = False

    for ko in a.modules:
        if not ko.exists():
            raise SystemExit(f"липсва: {ko}")
        vers = read_versions(ko)

        from_vendor = []
        for name, crc in vers:
            src = prov.get(name)
            if src is None and name not in vendor:
                # Никой не го предоставя: нито vmlinux след trimming,
                # нито наш модул, нито вендорският стек. Това е
                # "Unknown symbol" при insmod - модулът няма да се
                # зареди изобщо.
                unresolved.append((ko.name, name))
                fatal = True
            elif src in VENDOR_MODULES or (src is None and name in vendor):
                from_vendor.append((name, crc))

        print(f"{ko.name}: {len(vers)} символа общо, "
              f"{len(from_vendor)} от вендорския стек")

        for name, crc in from_vendor:
            checked.add(name)
            want = vendor.get(name)
            if want is None:
                unknown.append((ko.name, name))
                fatal = True
            elif want != crc:
                mismatch.append((ko.name, name, crc, want))

    print()
    if unresolved:
        print("НЕРАЗРЕШЕНИ СИМВОЛИ — insmod ще каже 'Unknown symbol' и "
              "модулът НЯМА да се зареди:")
        for m, n in unresolved:
            print(f"  {m}: {n}")
        print("\n  Нито vmlinux, нито вендорските модули ги предоставят.\n"
              "  Ако символът е обикновена кернел функция, най-вероятната\n"
              "  причина е CONFIG_TRIM_UNUSED_KSYMS - провери дали е в\n"
              "  Module.symvers на ТОЗИ билд, а не на предишния.\n")
    if unknown:
        print("СИМВОЛИ, КОИТО ВЕНДОРСКИЯТ СТЕК НЕ ЕКСПОРТИРА — "
              "insmod ще каже 'Unknown symbol':")
        for m, n in unknown:
            print(f"  {m}: {n}")
        print()
    if mismatch:
        print(f"CRC не съвпадат за {len(mismatch)} символа. Това е "
              f"ОЧАКВАНО и не блокира зареждането —")
        print("този кернел не налага modversions (виж коментара в "
              "началото на скрипта).")
        print("Списък за сведение:")
        for m, n, got, want in mismatch:
            print(f"  {m}: {n}  наш {got:016X} | вендор {want:016X}")
        print()

    if fatal:
        return 1
    if mismatch:
        print(f"OK с предупреждения: {len(checked)} символа от вендорския "
              f"стек, всички се разрешават.")
        return 2
    print(f"ОК: {len(checked)} символа от вендорския стек, "
          "всички CRC-та съвпадат.")
    print("Това не гарантира, че драйверът работи — гарантира само, че "
          "модулът ще се зареди.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
