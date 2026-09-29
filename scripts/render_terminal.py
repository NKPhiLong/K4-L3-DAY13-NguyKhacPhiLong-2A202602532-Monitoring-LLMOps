"""Chuyển output text thật (tests, validators, log) thành ảnh PNG kiểu terminal.

Nội dung được giữ nguyên từng ký tự; script chỉ thêm khung và tiêu đề ghi rõ
file nguồn + commit để người chấm đối chiếu với file .txt đi kèm.

Ví dụ:
    python scripts/render_terminal.py submission/evidence/01-pytest.txt
    python scripts/render_terminal.py a.txt --out a.png --title "Structured log"
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.cli import configure_utf8_stdio
from scripts.build_dashboard import screenshot

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#0d0d0d;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#e6e6e3}}
.bar{{background:#2c2c2a;color:#c3c2b7;padding:8px 14px;font:12px system-ui,-apple-system,sans-serif}}
.bar b{{color:#fff}}pre{{margin:0;padding:12px 16px;white-space:pre-wrap;word-break:break-all}}
.cmd{{color:#86b6ef}}.ok{{color:#0ca30c}}.bad{{color:#e66767}}
</style></head><body><div class="bar"><b>{title}</b> · source: {source} · commit {commit}</div><pre>{body}</pre></body></html>"""


def colorize(line: str) -> str:
    escaped = html.escape(line)
    if line.startswith("$ "):
        return f"<span class='cmd'>{escaped}</span>"
    if any(word in line for word in ("[PASSED]", "HỢP LỆ", "SẠCH", " passed")) and "failed" not in line:
        return f"<span class='ok'>{escaped}</span>"
    if any(word in line for word in ("[FAILED]", "KHÔNG HỢP LỆ", "PHÁT HIỆN", " failed")):
        return f"<span class='bad'>{escaped}</span>"
    return escaped


def current_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Render file text thành ảnh terminal")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--title")
    parser.add_argument("--width", type=int, default=1100)
    args = parser.parse_args()

    text = args.source.read_text(encoding="utf-8")
    lines = text.rstrip("\n").splitlines()
    body = "\n".join(colorize(line) for line in lines)
    page = PAGE.format(
        title=html.escape(args.title or args.source.stem),
        source=html.escape(str(args.source)),
        commit=current_commit(),
        body=body,
    )
    chars_per_line = max(40, (args.width - 32) // 8)
    visual_lines = sum(max(1, -(-len(line) // chars_per_line)) for line in lines)
    height = 60 + visual_lines * 20

    out = args.out or args.source.with_suffix(".png")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(page)
        page_path = Path(f.name)
    try:
        screenshot(page_path, out, width=args.width, height=height)
    finally:
        page_path.unlink(missing_ok=True)
    print(f"Ảnh: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
