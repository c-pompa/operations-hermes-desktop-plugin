"""The catalog entry's images stay pinned to its sha and reachable; the docs site and the
Desktop catalog render them at that sha, so an unpinned or dead URL is a broken listing."""

import sys
import urllib.request
from pathlib import Path

import yaml

entry = yaml.safe_load(Path("docs/catalog/operations.yaml").read_text(encoding="utf-8"))
sha = entry["sha"]
urls = [u for u in [entry.get("image") or "", *(entry.get("screenshots") or [])] if u]

bad = []
for url in urls:
    if sha not in url:
        bad.append(f"not pinned to {sha}: {url}")
        continue
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            if resp.status != 200:
                bad.append(f"HTTP {resp.status}: {url}")
    except Exception as exc:
        bad.append(f"{exc}: {url}")
if not entry.get("image"):
    bad.append("no card image")
if not urls:
    bad.append("no images at all")

for line in bad:
    print(f"FAIL {line}")
print(f"{len(urls)} image(s) checked, {len(bad)} failure(s)")
sys.exit(1 if bad else 0)
