"""Download the example model weights (pinned revision + SHA-256) and build model.tar.gz.

Runs inside CodeBuild during deployment (buildspec/build.yml); can also run locally. Nothing it
downloads is committed (see .gitignore). Layout expected by the SageMaker AI PyTorch container:
    model.tar.gz
      weights.bin           # state_dict, loaded with torch.load(weights_only=True)
      code/inference.py

    python3 scripts/package_model.py --out build/model.tar.gz [--weights <EXISTING_FILE>]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Hugging Face model repository timm/resnet18.a1_in1k, license Apache-2.0 (model card metadata),
# checked 2026-10-21. Pinned to a commit so the bytes cannot change underneath the checksum.
REPO = "timm/resnet18.a1_in1k"
REVISION = "491b427b45c94c7fb0e78b5474cc919aff584bbf"  # pragma: allowlist secret (public git commit hash)
FILE = "pytorch_model.bin"
SHA256 = "1d92fca7bc19313523ad23dff7f89800791baa592f40366537a829bd436e5e14"  # pragma: allowlist secret
URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{FILE}"
MAX_BYTES = 200 * 1024 * 1024


def download(url: str = URL) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("https only")
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - https URL checked or fixed  # nosec B310
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("download larger than expected")
    return data


def verify(data: bytes, expected: str = SHA256) -> None:
    got = hashlib.sha256(data).hexdigest()
    if got != expected:
        raise SystemExit(f"checksum mismatch for {FILE}: refusing to package")


def build_tar(weights: bytes, out: Path) -> str:
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("weights.bin", weights),
                              ("code/inference.py", (ROOT / "model/code/inference.py").read_bytes())):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(payload), 0, 0o644
            tar.addfile(info, io.BytesIO(payload))
    out.write_bytes(buf.getvalue())
    return hashlib.sha256(buf.getvalue()).hexdigest()


def code_hash() -> str:
    """Identifies the artifact: inference code + pinned weights checksum (used in the S3 key)."""
    h = hashlib.sha256((ROOT / "model/code/inference.py").read_bytes())
    h.update(SHA256.encode())
    return h.hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="build/model.tar.gz")
    ap.add_argument("--weights", help="use an already downloaded file (still checksum-verified)")
    ap.add_argument("--print-key", action="store_true", help="print the artifact key and exit")
    args = ap.parse_args(argv)
    if args.print_key:
        print(f"model/{code_hash()}/model.tar.gz")
        return 0
    data = Path(args.weights).read_bytes() if args.weights else download()
    verify(data)
    digest = build_tar(data, Path(args.out))
    print(f"packaged {args.out} sha256={digest[:12]}... key=model/{code_hash()}/model.tar.gz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
