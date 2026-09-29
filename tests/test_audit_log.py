from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app import audit, logging_config
from app.main import app

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "config" / "audit_schema.json").read_text(encoding="utf-8"))


def _call(path: str, headers: dict | None = None) -> httpx.Response:
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, headers=headers or {})

    return asyncio.run(run())


def test_incident_toggle_writes_audit_record_matching_schema(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(logging_config, "LOG_PATH", tmp_path / "logs.jsonl")

    enabled = _call("/incidents/rag_slow/enable", {"x-actor": "oncall@vinuni.edu.vn", "x-request-id": "req-a0d17001"})
    _call("/incidents/rag_slow/disable")
    rejected = _call("/incidents/not_real/enable")

    assert enabled.status_code == 200 and rejected.status_code == 404
    records = audit.read_audit()
    assert [(r["action"], r["result"]) for r in records] == [
        ("incident.enable", "success"),
        ("incident.disable", "success"),
        ("incident.enable", "rejected"),
    ]
    first = records[0]
    assert first["correlation_id"] == "req-a0d17001"
    assert (first["previous_state"], first["new_state"]) == (False, True)
    assert "oncall@" not in first["actor"]  # actor cũng được scrub PII
    for record in records:
        assert set(SCHEMA["required"]).issubset(record)
        assert set(record).issubset(SCHEMA["properties"])


def test_retention_prunes_old_records(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    old = {"ts": (now - timedelta(days=120)).isoformat(), "action": "incident.enable"}
    new = {"ts": (now - timedelta(days=1)).isoformat(), "action": "incident.disable"}
    path.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")

    removed = audit.prune_audit(90, path=path, now=now)

    assert removed == 1
    assert [r["action"] for r in audit.read_audit(path)] == ["incident.disable"]


def test_query_filters_by_action_and_time() -> None:
    now = datetime.now(timezone.utc)
    records = [
        {"ts": (now - timedelta(hours=3)).isoformat(), "action": "incident.enable", "target": "rag_slow", "result": "success"},
        {"ts": now.isoformat(), "action": "incident.enable", "target": "tool_fail", "result": "success"},
        {"ts": now.isoformat(), "action": "incident.disable", "target": "tool_fail", "result": "success"},
    ]

    out = audit.query_audit(records, action="incident.enable", since=now - timedelta(hours=1))

    assert [r["target"] for r in out] == ["tool_fail"]
