"""Audit log riêng cho thao tác control-plane (bật/tắt incident).

Tách khỏi data/logs.jsonl vì mục đích khác nhau: log ứng dụng phục vụ debug và
dashboard, audit log trả lời "ai đã thay đổi gì, lúc nào" và có retention riêng.
Schema: config/audit_schema.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .pii import scrub_text

SCHEMA_VERSION = 1
_LOCK = threading.Lock()


def audit_path() -> Path:
    return Path(os.getenv("AUDIT_LOG_PATH", "data/audit.jsonl"))


def retention_days() -> int:
    return int(os.getenv("AUDIT_RETENTION_DAYS", "90"))


def hash_value(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None


def write_audit(
    *,
    action: str,
    target: str,
    result: str,
    correlation_id: str,
    actor: str | None,
    client_ip: str | None,
    previous_state: Any = None,
    new_state: Any = None,
    path: Path | None = None,
) -> dict:
    record = {
        "schema_version": SCHEMA_VERSION,
        "audit_id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": os.getenv("APP_NAME", "day13-l3a-monitoring-llmops-lab"),
        "env": os.getenv("APP_ENV", "dev"),
        "actor": scrub_text(actor)[:64] if actor else "anonymous",
        "client_ip_hash": hash_value(client_ip),
        "action": action,
        "target": target,
        "result": result,
        "correlation_id": correlation_id,
        "previous_state": previous_state,
        "new_state": new_state,
    }
    target_path = path or audit_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, target_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_audit(path: Path | None = None) -> list[dict]:
    target_path = path or audit_path()
    if not target_path.exists():
        return []
    records = []
    for line in target_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def prune_audit(days: int | None = None, path: Path | None = None, now: datetime | None = None) -> int:
    """Xóa bản ghi cũ hơn `days` ngày; trả về số bản ghi đã xóa."""
    target_path = path or audit_path()
    keep_days = retention_days() if days is None else days
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    with _LOCK:
        records = read_audit(target_path)
        kept = [r for r in records if datetime.fromisoformat(r["ts"]) >= cutoff]
        if len(kept) != len(records):
            tmp = target_path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
            tmp.replace(target_path)
    return len(records) - len(kept)


def query_audit(
    records: Iterable[dict],
    *,
    action: str | None = None,
    target: str | None = None,
    since: datetime | None = None,
    result: str | None = None,
) -> list[dict]:
    out = []
    for record in records:
        if action and record.get("action") != action:
            continue
        if target and record.get("target") != target:
            continue
        if result and record.get("result") != result:
            continue
        if since and datetime.fromisoformat(record["ts"]) < since:
            continue
        out.append(record)
    return out
