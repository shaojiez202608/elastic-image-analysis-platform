"""Repository-wide guards: no price/cost/saving wording (the evaluation is about relative behaviour, not
money), no real account IDs, no local paths or internal names, no committed data or weights."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_MONEY = re.compile(r"价格|成本|节省|\bprices?\b|\bpricing\b|\bcosts?\b|\bcostly\b|\bsavings?\b|\bcheaper\b",
                             re.IGNORECASE)
ACCOUNT = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
LOCAL = re.compile(r"/Users/|163\.com", re.IGNORECASE)
BINARY = {".png", ".jpg", ".jpeg", ".gz", ".bin", ".pth", ".safetensors", ".zip"}


def repo_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / f for f in out if (ROOT / f).is_file()]


def texts():
    for f in repo_files():
        if f.suffix.lower() in BINARY:
            continue
        yield f, f.read_text(encoding="utf-8", errors="replace")


def test_no_money_wording_anywhere():
    hits = [f"{f.relative_to(ROOT)}:{n}: {m.group(0)}" for f, t in texts()
            for n, line in enumerate(t.splitlines(), 1) for m in [FORBIDDEN_MONEY.search(line)] if m
            and f.name != "test_repo_hygiene.py"]
    assert hits == []


def test_no_account_ids_or_local_paths():
    bad = []
    for f, t in texts():
        if f.name == "test_repo_hygiene.py":
            continue
        for n, line in enumerate(t.splitlines(), 1):
            for m in ACCOUNT.finditer(line):
                if m.group(0) != "111122223333" and not re.search(r"[0-9a-f]{20,}", line):
                    bad.append(f"{f.relative_to(ROOT)}:{n}")
            if LOCAL.search(line):
                bad.append(f"{f.relative_to(ROOT)}:{n} local path")
    assert bad == []


def test_no_images_weights_or_data_committed():
    assert [f for f in repo_files() if f.suffix.lower() in BINARY] == []


def test_the_guard_detects_a_planted_example():
    # Negative control: the pattern must fire on the words it is meant to block.
    for s in ("estimated cost", "价格", "节省 30%", "Savings plan", "pricing page"):
        assert FORBIDDEN_MONEY.search(s)
    assert ACCOUNT.search("arn:aws:iam::123456789012:root")
