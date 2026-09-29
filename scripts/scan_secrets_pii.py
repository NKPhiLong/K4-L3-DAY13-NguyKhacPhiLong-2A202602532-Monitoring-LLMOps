"""Quét secret và PII thô trước khi commit/push.

- Quét mọi file được git theo dõi hoặc đang staged (bỏ qua file nhị phân).
- Tùy chọn quét thêm log runtime: --logs data/logs.jsonl
- Thoát mã 1 nếu phát hiện vấn đề, để dùng được trong CI hoặc pre-commit hook.

Dữ liệu mẫu có PII giả do đề bài cung cấp (data/sample_queries.jsonl, tests/, docs/)
được allowlist cho PII nhưng KHÔNG được allowlist cho secret.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.cli import configure_utf8_stdio
from scripts.validate_logs import PII_DETECTORS

SECRET_DETECTORS = {
    "langfuse_secret_key": re.compile(r"sk-lf-[A-Za-z0-9-]{8,}"),
    "langfuse_public_key": re.compile(r"pk-lf-[A-Za-z0-9-]{8,}"),
    "anthropic_api_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "openai_api_key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Dòng kiểu .env/YAML: TÊN_VIẾT_HOA chứa SECRET/TOKEN/PASSWORD/API_KEY được gán giá trị dài.
    "assigned_secret": re.compile(
        r"^\s*(?:export\s+)?[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)[A-Z0-9_]*\s*[=:]\s*['\"]?[A-Za-z0-9/+_.-]{12,}"
    ),
}
FORBIDDEN_PATHS = (".env", "config/challenge.json", "data/logs.jsonl", "data/audit.jsonl")
PII_ALLOWLIST_PREFIXES = (
    "data/sample_queries.jsonl",
    "tests/",
    "docs/",
    "scripts/scan_secrets_pii.py",
    # Evidence 05 phải chứa input PII GIẢ để đối chiếu với log đã redact.
    "submission/evidence/05-pii-redaction",
)


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return sorted({line for line in out.splitlines() if line})


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_file(rel: str, text: str, check_pii: bool) -> list[str]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern in SECRET_DETECTORS.items():
            if pattern.search(line):
                findings.append(f"{rel}:{lineno}: secret:{name}")
        if check_pii:
            for name, pattern in PII_DETECTORS.items():
                if pattern.search(line):
                    findings.append(f"{rel}:{lineno}: pii:{name}")
    return findings


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Quét secret và PII thô trong repository")
    parser.add_argument("--logs", type=Path, action="append", default=[], help="Log runtime cần quét thêm PII")
    args = parser.parse_args()

    findings: list[str] = []
    files = tracked_files()
    for rel in files:
        if rel in FORBIDDEN_PATHS:
            findings.append(f"{rel}: file không được commit")
            continue
        text = read_text(REPO_ROOT / rel)
        if text is None:
            continue
        findings += scan_file(rel, text, check_pii=not rel.startswith(PII_ALLOWLIST_PREFIXES))

    for log_path in args.logs:
        text = read_text(log_path)
        if text is None:
            findings.append(f"{log_path}: không đọc được")
            continue
        findings += scan_file(str(log_path), text, check_pii=True)

    print(f"Đã quét {len(files)} file trong repo" + (f" và {len(args.logs)} log runtime" if args.logs else ""))
    if findings:
        print(f"PHÁT HIỆN {len(findings)} vấn đề:")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print("SẠCH: không có secret, PII thô hoặc file cấm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
