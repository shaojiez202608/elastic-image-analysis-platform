"""Generate the synthetic sample set into sample-data/generated/ (git-ignored; nothing real, nothing committed).

    uv run python scripts/gen_images.py [--count 20] [--out sample-data/generated]

Writes good JPEG/PNG images (random shapes on gradients) and one file per bad case, plus manifest.json
with the expected outcome of each file (analyzed / rejected:<REASON>).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from imaging.synthetic import BAD_CASES, bad_file, good_image  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--out", default=str(ROOT / "sample-data" / "generated"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i in range(a.count):
        fmt = "PNG" if i % 4 == 3 else "JPEG"
        name = f"good-{i:03d}.{'png' if fmt == 'PNG' else 'jpg'}"
        (out / name).write_bytes(good_image(i, size=(320 + 16 * (i % 5), 240 + 8 * (i % 3)), fmt=fmt))
        manifest.append({"file": name, "content_type": f"image/{'png' if fmt == 'PNG' else 'jpeg'}", "expect": "analyzed"})
    for case in sorted(BAD_CASES):
        data, ctype, reason = bad_file(case)
        name = f"bad-{case}.{'png' if ctype == 'image/png' else 'jpg'}"
        (out / name).write_bytes(data)
        manifest.append({"file": name, "content_type": ctype, "expect": f"rejected:{reason}"})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(manifest)} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
