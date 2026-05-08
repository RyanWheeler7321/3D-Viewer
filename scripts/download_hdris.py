#!/usr/bin/env python3
"""Download optional Poly Haven HDRIs for 3D Viewer.

The viewer works without these files. If they are missing, HDRI slots fall back
cleanly to flat lighting/backgrounds.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runtime" / "hdri_cache"
ASSETS = [
    ("cloud", "kloppenheim_03_puresky", "Cloud Sky", "kloppenheim_03_puresky_2k.hdr"),
    ("studio", "cyclorama_hard_light", "Studio", "cyclorama_hard_light_2k.hdr"),
    ("nature", "forest_slope", "Nature", "forest_slope_2k.hdr"),
    ("night_desert", "rogland_moonlit_night", "Night Desert", "rogland_moonlit_night_2k.hdr"),
    ("city", "canary_wharf", "City", "canary_wharf_2k.hdr"),
    ("night_city", "neuer_zollhof", "Night City", "neuer_zollhof_2k.hdr"),
]
BASE = "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/{filename}"


def download(url: str, path: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "r7321-3d-viewer/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response, path.open("wb") as fh:
        fh.write(response.read())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"source": "Poly Haven", "resolution": "2k", "format": "hdr", "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "assets": []}
    for slot, asset_id, label, filename in ASSETS:
        path = OUT / filename
        url = BASE.format(filename=filename)
        if not path.exists():
            print(f"downloading {label}: {filename}")
            download(url, path)
        data = path.read_bytes()
        manifest["assets"].append({"slot": slot, "asset_id": asset_id, "label": label, "path": str(path), "url": url, "size": len(data), "md5": hashlib.md5(data).hexdigest()})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"HDRIs ready: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
