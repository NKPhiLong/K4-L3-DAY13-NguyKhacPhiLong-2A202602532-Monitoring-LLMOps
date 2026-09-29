from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from app import logging_config
from app.main import app
from app.middleware import resolve_correlation_id

GENERATED_ID = re.compile(r"^req-[0-9a-f]{8}$")


async def _post_chat(client: httpx.AsyncClient, headers: dict | None = None, **body) -> httpx.Response:
    payload = {"user_id": "student-01", "session_id": "session-01", "feature": "qa", "message": "Explain logs"}
    payload.update(body)
    return await client.post("/chat", json=payload, headers=headers or {})


def _run(coro_factory):
    async def runner():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await coro_factory(client)

    return asyncio.run(runner())


def test_resolve_correlation_id_generates_or_accepts_safe_ids() -> None:
    assert GENERATED_ID.match(resolve_correlation_id(None))
    assert GENERATED_ID.match(resolve_correlation_id(""))
    assert resolve_correlation_id("req-1a2b3c4d") == "req-1a2b3c4d"
    # Header chứa ký tự lạ hoặc quá dài bị thay bằng ID mới để tránh log injection.
    assert GENERATED_ID.match(resolve_correlation_id('bad"id\ninjected'))
    assert GENERATED_ID.match(resolve_correlation_id("x" * 200))


def test_response_returns_generated_correlation_id_and_timing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logging_config, "LOG_PATH", tmp_path / "logs.jsonl")

    response = _run(lambda client: _post_chat(client))

    request_id = response.headers["x-request-id"]
    assert GENERATED_ID.match(request_id)
    assert response.json()["correlation_id"] == request_id
    assert float(response.headers["x-response-time-ms"]) >= 0


def test_incoming_request_id_is_propagated_to_logs(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "logs.jsonl"
    monkeypatch.setattr(logging_config, "LOG_PATH", log_path)

    response = _run(lambda client: _post_chat(client, headers={"x-request-id": "req-0badc0de"}))

    assert response.headers["x-request-id"] == "req-0badc0de"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    api_events = [event for event in events if event.get("service") == "api"]
    assert {event["event"] for event in api_events} == {"request_received", "response_sent"}
    for event in api_events:
        assert event["correlation_id"] == "req-0badc0de"
        assert event["session_id"] == "session-01"
        assert event["feature"] == "qa"
        assert event["model"]
        assert event["env"]
        assert len(event["user_id_hash"]) == 12


def test_context_does_not_leak_between_requests(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "logs.jsonl"
    monkeypatch.setattr(logging_config, "LOG_PATH", log_path)

    async def two_requests(client: httpx.AsyncClient):
        first = await _post_chat(client, session_id="session-A", feature="qa")
        second = await client.get("/health")
        third = await _post_chat(client, session_id="session-B", feature="summary")
        return first, second, third

    first, second, third = _run(two_requests)

    ids = {first.headers["x-request-id"], second.headers["x-request-id"], third.headers["x-request-id"]}
    assert len(ids) == 3
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    by_id: dict[str, set[str]] = {}
    for event in events:
        if event.get("service") == "api":
            by_id.setdefault(event["correlation_id"], set()).add(event["session_id"])
    assert by_id == {
        first.headers["x-request-id"]: {"session-A"},
        third.headers["x-request-id"]: {"session-B"},
    }


def test_raw_pii_in_message_never_reaches_log_file(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "logs.jsonl"
    monkeypatch.setattr(logging_config, "LOG_PATH", log_path)

    _run(lambda client: _post_chat(client, message="card 4111 1111 1111 1111, phone 0987654321"))

    written = log_path.read_text(encoding="utf-8")
    assert "4111 1111 1111 1111" not in written
    assert "0987654321" not in written
