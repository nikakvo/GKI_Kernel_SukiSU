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
        return f"""## Features

- SUSFS v2.3.0
- KPM Support (Kernel Patch Module)
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
- ath9k_htc External WiFi Adapter Support (TL-WN722N v1 over OTG)
- Additional TCP Congestion Control Algorithms Support (BIC, Westwood, HTCP)
- TTL/Hop-Limit Target Support (netfilter)
- Connection Mark (connmark) Support (netfilter)
- CIFS/SMB Network Filesystem Support
- Ptrace Leak Fix (kernels < 5.16) ) and Unicode Fix
{lto_feature_line}

> **How do I check a feature is actually active on my device?**
> Every feature above has a step-by-step verification command (what to run over
> `adb`/a root shell and exactly what output means it's on) in the
> **[Features & verification](https://github.com/nikakvo/GKI_Kernel_SukiSU#features--verification)**
> section of the README. Kept there instead of duplicated in every release so
> there's a single, always-current source of truth.
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
