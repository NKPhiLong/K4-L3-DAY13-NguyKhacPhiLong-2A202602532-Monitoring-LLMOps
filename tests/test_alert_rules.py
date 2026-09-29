from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("name", "severity", "condition", "duration", "type", "channel", "slack_channel", "owner", "runbook")


def test_three_complete_symptom_based_alerts_with_runbooks() -> None:
    alerts = yaml.safe_load((REPO_ROOT / "config" / "alert_rules.yaml").read_text(encoding="utf-8"))["alerts"]
    runbook_doc = (REPO_ROOT / "docs" / "alerts.md").read_text(encoding="utf-8")

    assert len(alerts) == 3
    assert len({alert["name"] for alert in alerts}) == 3
    for alert in alerts:
        for field in REQUIRED:
            assert alert.get(field), f"{alert.get('name')} thiếu {field}"
            assert "TODO" not in str(alert[field])
        assert alert["type"] == "symptom-based"
        assert alert["channel"] == "slack"
        assert alert["slack_channel"].startswith("#")
        assert re.fullmatch(r"\d+[mh]", alert["duration"])
        path, anchor = alert["runbook"].split("#")
        assert path == "docs/alerts.md"
        heading = anchor.replace("-", " ").title()
        assert f"## {heading}" in runbook_doc
        assert alert["name"] in runbook_doc


def test_slo_defines_target_and_error_budget() -> None:
    slo = yaml.safe_load((REPO_ROOT / "config" / "slo.yaml").read_text(encoding="utf-8"))
    primary = slo["primary_slo"]

    assert primary["target_percent"] + primary["error_budget_percent"] == 100
    example = primary["error_budget"]["example"]
    assert example["allowed_bad_requests"] == round(
        example["total_requests_28d"] * primary["error_budget_percent"] / 100
    )
