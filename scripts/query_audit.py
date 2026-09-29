"""Truy vấn audit log control-plane.

Ví dụ:
    python scripts/query_audit.py                               # toàn bộ
    python scripts/query_audit.py --action incident.enable --since-minutes 60
    python scripts/query_audit.py --target rag_slow --result success
    python scripts/query_audit.py --prune --retention-days 90
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audit import audit_path, prune_audit, query_audit, read_audit
from app.cli import configure_utf8_stdio


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Truy vấn data/audit.jsonl")
    parser.add_argument("--path", type=Path, default=None)
    parser.add_argument("--action", choices=["incident.enable", "incident.disable"])
    parser.add_argument("--target")
    parser.add_argument("--result", choices=["success", "rejected"])
    parser.add_argument("--since-minutes", type=int)
    parser.add_argument("--prune", action="store_true", help="Xóa bản ghi quá hạn retention trước khi truy vấn")
    parser.add_argument("--retention-days", type=int)
    args = parser.parse_args()

    path = args.path or audit_path()
    if args.prune:
        removed = prune_audit(args.retention_days, path=path)
        print(f"Đã prune {removed} bản ghi quá hạn")
    since = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes) if args.since_minutes else None
    rows = query_audit(read_audit(path), action=args.action, target=args.target, result=args.result, since=since)

    print(f"{'ts':<32} {'action':<17} {'target':<11} {'result':<9} {'actor':<16} correlation_id")
    for r in rows:
        print(f"{r['ts']:<32} {r['action']:<17} {r['target']:<11} {r['result']:<9} {r['actor']:<16} {r['correlation_id']}")
    print(f"-- {len(rows)} bản ghi ({path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
