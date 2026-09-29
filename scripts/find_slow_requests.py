"""Bước Logs trong quy trình Metrics → Logs → Traces.

Lọc data/logs.jsonl trong một khoảng thời gian, in các request chậm hơn ngưỡng
hoặc bị lỗi kèm correlation_id, cùng các sự kiện control (incident bật/tắt).
Dùng correlation_id in ra để tìm trace cùng ID trên Langfuse.

Ví dụ:
    python scripts/find_slow_requests.py --threshold-ms 2000
    python scripts/find_slow_requests.py --since 2026-09-29T17:28:00Z --until 2026-09-29T17:30:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.cli import configure_utf8_stdio

CONTROL_EVENTS = {"incident_enabled", "incident_disabled"}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Tìm request chậm/lỗi và correlation_id trong structured log")
    parser.add_argument("--logs", type=Path, default=Path("data/logs.jsonl"))
    parser.add_argument("--threshold-ms", type=int, default=2000)
    parser.add_argument("--since", help="ISO timestamp (UTC)")
    parser.add_argument("--until", help="ISO timestamp (UTC)")
    parser.add_argument("--feature", help="Chỉ lấy một feature")
    args = parser.parse_args()

    since = parse_ts(args.since) if args.since else None
    until = parse_ts(args.until) if args.until else None
    matches = 0
    print(f"{'ts (UTC)':<28} {'event':<18} {'correlation_id':<14} {'feature':<11} {'latency_ms':>10} {'ttft_ms':>7}  detail")
    for line in args.logs.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        ts = parse_ts(event["ts"])
        if (since and ts < since) or (until and ts > until):
            continue
        name = event.get("event")
        if name in CONTROL_EVENTS:
            print(f"{event['ts']:<28} {name:<18} {'-':<14} {'-':<11} {'-':>10} {'-':>7}  {event.get('payload')}")
            continue
        if args.feature and event.get("feature") != args.feature:
            continue
        slow = name == "response_sent" and event.get("latency_ms", 0) > args.threshold_ms
        failed = name == "request_failed"
        if not (slow or failed):
            continue
        matches += 1
        detail = f"error_type={event.get('error_type')}" if failed else f"tool_success={event.get('tool_success')}"
        print(
            f"{event['ts']:<28} {name:<18} {event.get('correlation_id', '-'):<14} {event.get('feature', '-'):<11} "
            f"{event.get('latency_ms', '-'):>10} {event.get('ttft_ms', '-'):>7}  {detail}"
        )
    print(f"-- {matches} request vượt {args.threshold_ms} ms hoặc lỗi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
