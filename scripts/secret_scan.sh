#!/usr/bin/env bash
# Secret scan: gitleaks over the full git history, then a canary check that proves the scanner
# reports a planted credential. The canary is generated at run time in a temporary directory
# outside the repository, so no credential-shaped string is ever committed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
command -v gitleaks >/dev/null || { echo "gitleaks not installed" >&2; exit 1; }
gitleaks git "$ROOT" --log-opts="--all" --config "$ROOT/.gitleaks.toml" --redact --no-banner
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# Assemble an AWS-access-key-shaped canary from parts (AKIA + 16 uppercase alphanumerics).
python3 -c 'import random,string; r=random.Random(7); print("aws_access_key_id = AKIA" + "".join(r.choice(string.ascii_uppercase+"234567") for _ in range(16)))' > "$TMP/canary.txt"
mkdir -p "$TMP/out"
gitleaks dir "$TMP/canary.txt" --config "$ROOT/.gitleaks.toml" --redact --no-banner \
  --report-format json --report-path "$TMP/out/report.json" >/dev/null 2>&1 || true
rules=$(python3 -c 'import json,sys; print(",".join(sorted({f["RuleID"] for f in json.load(open(sys.argv[1]))})))' "$TMP/out/report.json" 2>/dev/null || true)
[ -n "$rules" ] || { echo "canary NOT detected: the scanner configuration is broken" >&2; exit 1; }
echo "canary detected as expected (rules: $rules)"
