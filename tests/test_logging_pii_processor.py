from __future__ import annotations

import json
from pathlib import Path

import structlog

from app import logging_config
from app.logging_config import scrub_event


def test_scrub_event_scrubs_nested_payload_and_top_level_fields() -> None:
    event = {
        "event": "request_failed",
        "correlation_id": "req-1a2b3c4d",
        "detail": "user 0987654321 failed",
        "payload": {
            "message_preview": "mail student@vinuni.edu.vn",
            "items": ["card 4111 1111 1111 1111", {"cccd": "001099012345"}],
            "count": 3,
        },
    }

    out = scrub_event(None, "info", event)
    rendered = json.dumps(out, ensure_ascii=False)

    for raw in ("0987654321", "student@vinuni.edu.vn", "4111 1111 1111 1111", "001099012345"):
        assert raw not in rendered
    assert out["correlation_id"] == "req-1a2b3c4d"
    assert out["payload"]["count"] == 3


def test_pii_is_scrubbed_before_jsonl_file_is_written(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "logs.jsonl"
    monkeypatch.setattr(logging_config, "LOG_PATH", log_path)
    logging_config.configure_logging()

    structlog.get_logger().info(
        "request_received",
        service="api",
        payload={"message_preview": "Here is my phone 0987654321 and a@b.vn"},
    )

    written = log_path.read_text(encoding="utf-8")
    assert "0987654321" not in written
    assert "a@b.vn" not in written
    assert "REDACTED_PHONE_VN" in written


def test_scrub_processor_runs_before_file_writer() -> None:
    logging_config.configure_logging()
    processors = structlog.get_config()["processors"]
    scrub_index = processors.index(scrub_event)
    writer_index = next(
        i for i, p in enumerate(processors) if isinstance(p, logging_config.JsonlFileProcessor)
    )

    assert scrub_index < writer_index
    assert scrub_index > processors.index(structlog.processors.format_exc_info)
