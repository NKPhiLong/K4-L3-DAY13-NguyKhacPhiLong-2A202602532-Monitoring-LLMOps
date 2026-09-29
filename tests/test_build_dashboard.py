from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts import build_dashboard

REPO_ROOT = Path(__file__).resolve().parents[1]


def _event(ts: str, event: str, **fields) -> dict:
    return {"ts": ts, "event": event, "service": "api", "correlation_id": "req-00000000", **fields}


def test_dashboard_aggregations_follow_contract(tmp_path: Path) -> None:
    events = [
        _event("2026-09-29T10:00:05Z", "request_received"),
        _event("2026-09-29T10:00:06Z", "response_sent", latency_ms=100, ttft_ms=50, cost_usd=0.001,
               tokens_in=10, tokens_out=100, quality_score=0.8, tool_name="retrieval", tool_success=True),
        _event("2026-09-29T10:01:05Z", "request_received"),
        _event("2026-09-29T10:01:06Z", "response_sent", latency_ms=2600, ttft_ms=55, cost_usd=0.003,
               tokens_in=20, tokens_out=200, quality_score=0.9, tool_name="retrieval", tool_success=True),
        _event("2026-09-29T10:02:05Z", "request_received"),
        _event("2026-09-29T10:02:06Z", "request_failed", error_type="RuntimeError",
               tool_name="retrieval", tool_success=False),
        _event("2026-09-29T08:00:00Z", "request_received"),  # ngoài cửa sổ 60 phút
    ]
    logs = tmp_path / "logs.jsonl"
    logs.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    config = yaml.safe_load((REPO_ROOT / "config" / "dashboard.yaml").read_text(encoding="utf-8"))["dashboard"]
    loaded = build_dashboard.load_events(logs)
    window = build_dashboard.resolve_window(loaded, config["time_range_minutes"], "latest")
    panels, summary = build_dashboard.compute(config, loaded, window)

    assert [p.config["id"] for p in panels] == ["latency", "traffic", "errors", "cost", "tokens", "quality"]
    assert summary["requests"] == 3
    assert summary["latency_p95_ms"] == 2600
    assert summary["ttft_p95_ms"] == 55
    assert summary["error_rate_pct"] == 33.33
    assert summary["error_breakdown"] == {"RuntimeError": 1}
    assert summary["retrieval_success_pct"] == 66.67
    assert summary["cost_total_usd"] == 0.004
    assert (summary["tokens_in_total"], summary["tokens_out_total"]) == (30, 300)
    assert summary["quality_mean"] == 0.85
    assert summary["rate_per_minute"] == 1.0


def test_dashboard_html_shows_units_thresholds_and_time_range(tmp_path: Path) -> None:
    logs = tmp_path / "logs.jsonl"
    logs.write_text(
        json.dumps(_event("2026-09-29T10:00:06Z", "response_sent", latency_ms=100, ttft_ms=50, cost_usd=0.001,
                          tokens_in=10, tokens_out=100, quality_score=0.8)) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "dashboard.html"

    class Args:
        config = REPO_ROOT / "config" / "dashboard.yaml"
        end = "latest"
        summary = None
        theme = "light"

    Args.logs, Args.out = logs, out
    build_dashboard.build(Args)
    page = out.read_text(encoding="utf-8")

    assert "last 60 min" in page
    assert 'http-equiv="refresh" content="30"' in page
    for title in ("Latency percentiles and TTFT", "Request traffic", "Error rate and retrieval success",
                  "Cost over time", "Input and output tokens", "Quality proxy"):
        assert title in page
    assert "p95 ≤ 3k" in page
    assert "Unit: <b>ms</b>" in page
