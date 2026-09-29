"""Dựng dashboard 6 panel từ data/logs.jsonl theo contract config/dashboard.yaml.

Ví dụ:
    python scripts/build_dashboard.py                       # ghi data/dashboard.html
    python scripts/build_dashboard.py --end latest          # cửa sổ 60' kết thúc ở log mới nhất
    python scripts/build_dashboard.py --watch               # tự dựng lại mỗi refresh_seconds
    python scripts/build_dashboard.py --screenshot submission/evidence/11-dashboard-overview.png
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml

from app.cli import configure_utf8_stdio
from app.metrics import percentile
from scripts.validate_dashboard import load_dashboard_config

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)

SERIES = ("var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)")


# --------------------------------------------------------------------------- data


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            record["_ts"] = parse_ts(record["ts"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        events.append(record)
    return events


@dataclass
class Window:
    start: datetime
    end: datetime
    minutes: int

    def bucket(self, ts: datetime) -> int:
        return int((ts - self.start).total_seconds() // 60)

    def label(self, index: int) -> str:
        return (self.start + timedelta(minutes=index)).astimezone().strftime("%H:%M")


def resolve_window(events: list[dict], minutes: int, end: str) -> Window:
    if end == "latest" and events:
        end_ts = max(e["_ts"] for e in events)
    elif end in ("now", "latest"):
        end_ts = datetime.now(timezone.utc)
    else:
        end_ts = parse_ts(end)
    # Làm tròn lên đầu phút kế tiếp để bucket 1 phút thẳng hàng với đồng hồ.
    end_ts = end_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return Window(start=end_ts - timedelta(minutes=minutes), end=end_ts, minutes=minutes)


def by_minute(events: list[dict], window: Window) -> list[list[dict]]:
    buckets: list[list[dict]] = [[] for _ in range(window.minutes)]
    for event in events:
        index = window.bucket(event["_ts"])
        if 0 <= index < window.minutes:
            buckets[index].append(event)
    return buckets


def pct(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


@dataclass
class Panel:
    config: dict
    stats: list[tuple[str, str]] = field(default_factory=list)
    series: list[tuple[str, list[float | None]]] = field(default_factory=list)
    threshold_value: float | None = None
    status_value: float | None = None
    extra_html: str = ""
    chart_kind: str = "line"
    # False khi threshold áp lên tổng cả cửa sổ (cost/tokens): hiển thị bằng meter
    # thay vì vẽ lên trục theo phút, vì hai đại lượng khác thang đo.
    threshold_on_chart: bool = True
    # Series vẽ sau cùng (nằm trên) khi nhiều series trùng giá trị.
    emphasis: str | None = None
    # Đường alert phụ (ví dụ HighLatencyP95), khác với threshold/SLO của contract.
    alert_line: tuple[str, float] | None = None


def budget_meter(label: str, used: float, limit: float, fmt: str) -> str:
    ratio = used / limit if limit else 0.0
    state = "good" if ratio <= 1 else "bad"
    return (
        f"<div class='meter'><div class='meter-head'><span>{html.escape(label)}</span>"
        f"<span>{fmt.format(used)} / {fmt.format(limit)} ({ratio * 100:.1f}%)</span></div>"
        f"<div class='meter-track'><div class='meter-fill meter-{state}' "
        f"style='width:{min(ratio, 1) * 100:.2f}%'></div></div></div>"
    )


def load_alert_lines(path: Path = REPO_ROOT / "config" / "slo.yaml") -> dict[str, tuple[str, float]]:
    try:
        guardrails = yaml.safe_load(path.read_text(encoding="utf-8")).get("guardrails", {})
    except (OSError, AttributeError, yaml.YAMLError):
        return {}
    lines = {}
    if isinstance(guardrails.get("latency_p95_warning_ms"), (int, float)):
        lines["latency"] = ("alert HighLatencyP95: p95 > ", float(guardrails["latency_p95_warning_ms"]))
    return lines


def compute(config: dict, events: list[dict], window: Window) -> tuple[list[Panel], dict]:
    in_window = [e for e in events if window.start <= e["_ts"] < window.end]
    responses = [e for e in in_window if e.get("event") == "response_sent"]
    received = [e for e in in_window if e.get("event") == "request_received"]
    failed = [e for e in in_window if e.get("event") == "request_failed"]
    resp_buckets = by_minute(responses, window)
    recv_buckets = by_minute(received, window)
    fail_buckets = by_minute(failed, window)

    def values(items: list[dict], key: str) -> list:
        return [e[key] for e in items if isinstance(e.get(key), (int, float))]

    def per_minute(fn, buckets=resp_buckets) -> list[float | None]:
        return [fn(bucket) if bucket else None for bucket in buckets]

    latencies = values(responses, "latency_ms")
    ttfts = values(responses, "ttft_ms")
    costs = values(responses, "cost_usd")
    tokens_in = values(responses, "tokens_in")
    tokens_out = values(responses, "tokens_out")
    quality = values(responses, "quality_score")
    tool_flags = [e["tool_success"] for e in in_window if isinstance(e.get("tool_success"), bool)]
    error_types: dict[str, int] = {}
    for e in failed:
        error_types[e.get("error_type") or "unknown"] = error_types.get(e.get("error_type") or "unknown", 0) + 1

    active = [i for i, bucket in enumerate(recv_buckets) if bucket]
    active_minutes = (active[-1] - active[0] + 1) if active else 0
    rpm = round(len(received) / active_minutes, 2) if active_minutes else 0.0

    summary = {
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "requests": len(received),
        "responses": len(responses),
        "failed": len(failed),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "latency_p99_ms": percentile(latencies, 99),
        "ttft_p95_ms": percentile(ttfts, 95),
        "rate_per_minute": rpm,
        "error_rate_pct": pct(len(failed), len(received)) or 0.0,
        "error_breakdown": error_types,
        "retrieval_success_pct": pct(sum(tool_flags), len(tool_flags)),
        "cost_total_usd": round(sum(costs), 6),
        "tokens_in_total": sum(tokens_in),
        "tokens_out_total": sum(tokens_out),
        "quality_mean": round(mean(quality), 4) if quality else None,
    }

    panels = {p["id"]: Panel(config=p, threshold_value=p["threshold"]["value"]) for p in config["panels"]}

    p = panels["latency"]
    p.stats = [
        ("P50", f"{summary['latency_p50_ms']:,.0f}"),
        ("P95", f"{summary['latency_p95_ms']:,.0f}"),
        ("P99", f"{summary['latency_p99_ms']:,.0f}"),
        ("TTFT P95", f"{summary['ttft_p95_ms']:,.0f}"),
    ]
    p.series = [
        ("P50", per_minute(lambda b: percentile(values(b, "latency_ms"), 50))),
        ("P95", per_minute(lambda b: percentile(values(b, "latency_ms"), 95))),
        ("P99", per_minute(lambda b: percentile(values(b, "latency_ms"), 99))),
        ("TTFT P95", per_minute(lambda b: percentile(values(b, "ttft_ms"), 95))),
    ]
    p.status_value = summary["latency_p95_ms"] if latencies else None
    p.emphasis = "P95"
    p.alert_line = load_alert_lines().get("latency")

    p = panels["traffic"]
    p.stats = [("Requests", f"{len(received):,}"), ("Avg rate", f"{rpm:g}/min")]
    p.series = [("Requests", [float(len(b)) if b else None for b in recv_buckets])]
    p.status_value = rpm if received else None
    p.chart_kind = "column"

    p = panels["errors"]
    retrieval = summary["retrieval_success_pct"]
    p.stats = [
        ("Error rate", f"{summary['error_rate_pct']:g}%"),
        ("Failed", f"{len(failed):,}"),
        ("Retrieval success", "–" if retrieval is None else f"{retrieval:g}%"),
    ]
    p.series = [
        (
            "Error rate",
            [pct(len(f), len(r)) if r else None for f, r in zip(fail_buckets, recv_buckets)],
        )
    ]
    p.status_value = summary["error_rate_pct"] if received else None
    rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td class='num'>{count:,}</td></tr>"
        for name, count in sorted(error_types.items(), key=lambda kv: -kv[1])
    ) or "<tr><td colspan='2' class='muted'>No request_failed events in window</td></tr>"
    p.extra_html = (
        "<table class='breakdown'><thead><tr><th>error_type</th><th class='num'>count</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
    )

    p = panels["cost"]
    p.stats = [
        ("Total", f"${summary['cost_total_usd']:.4f}"),
        ("Avg / request", f"${(summary['cost_total_usd'] / len(costs)) if costs else 0:.5f}"),
    ]
    p.series = [("Cost per minute", per_minute(lambda b: round(sum(values(b, "cost_usd")), 6)))]
    p.status_value = summary["cost_total_usd"] if costs else None
    p.chart_kind = "column"
    p.threshold_on_chart = False
    p.extra_html = budget_meter(
        "Window total vs budget", summary["cost_total_usd"], p.threshold_value, "${:.4f}"
    )

    p = panels["tokens"]
    p.stats = [("Input total", f"{sum(tokens_in):,}"), ("Output total", f"{sum(tokens_out):,}")]
    p.series = [
        ("Input", per_minute(lambda b: float(sum(values(b, "tokens_in"))))),
        ("Output", per_minute(lambda b: float(sum(values(b, "tokens_out"))))),
    ]
    p.status_value = max(sum(tokens_in), sum(tokens_out)) if responses else None
    p.threshold_on_chart = False
    p.extra_html = budget_meter("Input vs limit", sum(tokens_in), p.threshold_value, "{:,.0f}") + budget_meter(
        "Output vs limit", sum(tokens_out), p.threshold_value, "{:,.0f}"
    )

    p = panels["quality"]
    q = summary["quality_mean"]
    p.stats = [("Mean", "–" if q is None else f"{q:.3f}"), ("Scored", f"{len(quality):,}")]
    p.series = [("Mean quality", per_minute(lambda b: round(mean(values(b, "quality_score")), 3)))]
    p.status_value = q

    ordered = [panels[p["id"]] for p in config["panels"]]
    return ordered, summary


# --------------------------------------------------------------------------- render

W, H = 560, 200
PAD_L, PAD_R, PAD_T, PAD_B = 52, 16, 12, 26


def nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exponent = 10 ** (len(str(int(value))) - 1) if value >= 1 else 0.1
    # Chỉ các mốc chia 4 ra số tròn (5 gridline).
    for step in (1, 2, 4, 5, 8, 10):
        candidate = step * exponent
        if candidate >= value:
            return candidate
    return 10 * exponent


def fmt_tick(value: float, unit: str) -> str:
    if unit == "usd":
        return f"${value:.3f}" if value < 1 else f"${value:,.2f}"
    if unit == "percent":
        return f"{value:g}%"
    if value >= 1000:
        return f"{value / 1000:g}k"
    return f"{value:g}"


def render_chart(panel: Panel, window: Window) -> str:
    unit = panel.config["unit"]
    threshold = panel.threshold_value if panel.threshold_on_chart else None
    observed = [v for _, s in panel.series for v in s if v is not None]
    if panel.alert_line:
        observed.append(panel.alert_line[1])
    # Trục luôn chứa threshold để người đọc thấy khoảng cách tới SLO.
    y_max = nice_max(max(observed + [threshold or 0]) * 1.1)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    step = plot_w / window.minutes

    def x(i: int) -> float:
        return PAD_L + step * (i + 0.5)

    def y(v: float) -> float:
        return PAD_T + plot_h * (1 - v / y_max)

    parts = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='{html.escape(panel.config['title'])}'>"]
    for k in range(5):
        v = y_max * k / 4
        parts.append(
            f"<line class='grid' x1='{PAD_L}' x2='{W - PAD_R}' y1='{y(v):.1f}' y2='{y(v):.1f}'/>"
            f"<text class='tick' x='{PAD_L - 6}' y='{y(v) + 4:.1f}' text-anchor='end'>{fmt_tick(v, unit)}</text>"
        )
    for i in range(0, window.minutes + 1, 10):
        xi = PAD_L + step * i
        label = window.label(i)
        parts.append(f"<text class='tick' x='{xi:.1f}' y='{H - 6}' text-anchor='middle'>{label}</text>")
    parts.append(f"<line class='axis' x1='{PAD_L}' x2='{W - PAD_R}' y1='{y(0):.1f}' y2='{y(0):.1f}'/>")

    if panel.chart_kind == "column":
        name, series = panel.series[0]
        bar_w = max(2.0, min(24.0, step - 2))
        for i, v in enumerate(series):
            if v is None:
                continue
            top, bottom = y(v), y(0)
            height = max(bottom - top, 1.0)
            r = min(4.0, bar_w / 2, height)
            x0 = x(i) - bar_w / 2
            parts.append(
                f"<path class='mark' style='fill:{SERIES[0]}' d='M{x0:.1f},{bottom:.1f} V{top + r:.1f} "
                f"Q{x0:.1f},{top:.1f} {x0 + r:.1f},{top:.1f} H{x0 + bar_w - r:.1f} "
                f"Q{x0 + bar_w:.1f},{top:.1f} {x0 + bar_w:.1f},{top + r:.1f} V{bottom:.1f} Z'>"
                f"<title>{window.label(i)} · {name}: {fmt_tick(v, unit) if unit in ('usd', 'percent') else f'{v:,.4g}'}</title></path>"
            )
    else:
        draw_order = sorted(enumerate(panel.series), key=lambda item: item[1][0] == panel.emphasis)
        for s_index, (name, series) in draw_order:
            color = SERIES[s_index % len(SERIES)]
            segment: list[str] = []
            segments: list[list[str]] = []
            for i, v in enumerate(series):
                if v is None:
                    if segment:
                        segments.append(segment)
                    segment = []
                    continue
                segment.append(f"{x(i):.1f},{y(v):.1f}")
            if segment:
                segments.append(segment)
            for seg in segments:
                if len(seg) > 1:
                    parts.append(f"<polyline class='line' style='stroke:{color}' points='{' '.join(seg)}'/>")
            for i, v in enumerate(series):
                if v is None:
                    continue
                shown = fmt_tick(v, unit) if unit in ("usd", "percent") else f"{v:,.4g}"
                parts.append(
                    f"<circle class='dot' style='fill:{color}' cx='{x(i):.1f}' cy='{y(v):.1f}' r='4'>"
                    f"<title>{window.label(i)} · {html.escape(name)}: {shown} {html.escape(unit)}</title></circle>"
                )

    if panel.alert_line:
        label, value = panel.alert_line
        ay = y(value)
        parts.append(
            f"<line class='alert-line' x1='{PAD_L}' x2='{W - PAD_R}' y1='{ay:.1f}' y2='{ay:.1f}'/>"
            f"<text class='alert-label' x='{PAD_L + 6}' y='{ay - 5:.1f}' text-anchor='start'>"
            f"{html.escape(label)}{fmt_tick(value, unit)}</text>"
        )
    if threshold is not None:
        ty = y(threshold)
        op = "≤" if panel.config["threshold"]["operator"] == "lte" else "≥"
        parts.append(
            f"<line class='threshold' x1='{PAD_L}' x2='{W - PAD_R}' y1='{ty:.1f}' y2='{ty:.1f}'/>"
            f"<text class='threshold-label' x='{PAD_L + 6}' y='{ty - 5:.1f}' text-anchor='start'>"
            f"{html.escape(panel.config['threshold']['aggregation'])} {op} {fmt_tick(threshold, unit)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def status_chip(panel: Panel) -> str:
    rule = panel.config["threshold"]
    if panel.status_value is None:
        return "<span class='chip chip-muted'>◌ No data</span>"
    ok = panel.status_value <= rule["value"] if rule["operator"] == "lte" else panel.status_value >= rule["value"]
    return (
        "<span class='chip chip-good'>✓ Within threshold</span>"
        if ok
        else "<span class='chip chip-bad'>✕ Threshold breached</span>"
    )


def render_panel(panel: Panel, window: Window) -> str:
    cfg = panel.config
    legend = ""
    if len(panel.series) > 1:
        legend = "<div class='legend'>" + "".join(
            f"<span><i style='background:{SERIES[i]}'></i>{html.escape(name)}</span>"
            for i, (name, _) in enumerate(panel.series)
        ) + "</div>"
    stats = "".join(
        f"<div class='stat'><div class='stat-label'>{html.escape(label)}</div>"
        f"<div class='stat-value'>{html.escape(value)}</div></div>"
        for label, value in panel.stats
    )
    op = "≤" if cfg["threshold"]["operator"] == "lte" else "≥"
    return f"""
<section class="panel" id="panel-{cfg['id']}">
  <header>
    <div>
      <h2>{html.escape(cfg['title'])}</h2>
      <p class="meta">Unit: <b>{html.escape(cfg['unit'])}</b> · Threshold: {html.escape(cfg['threshold']['aggregation'])} {op} {cfg['threshold']['value']:g} · Source: <code>{html.escape(cfg['source'])}</code> · events: {html.escape(', '.join(cfg['events']))}</p>
    </div>
    {status_chip(panel)}
  </header>
  <div class="stats">{stats}</div>
  {legend}
  {render_chart(panel, window)}
  {panel.extra_html}
</section>"""


CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;
--series-4:#eda100;--serious:#ec835a;--good:#0ca30c;--good-ink:#006300;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;
--ink:#fff;--ink-2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;
--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--good-ink:#0ca30c}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--ink:#fff;--ink-2:#c3c2b7;--grid:#2c2c2a;
--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--good-ink:#0ca30c}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1360px;margin:0 auto;padding:20px 16px 32px}
.top{display:flex;flex-wrap:wrap;gap:8px 24px;align-items:baseline;justify-content:space-between;margin-bottom:14px}
h1{font-size:20px;margin:0}.top p{margin:0;color:var(--ink-2)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,600px),1fr));gap:14px}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.panel header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
h2{font-size:15px;margin:0}.meta{margin:2px 0 0;color:var(--muted);font-size:12px}code{font-size:11px}
.chip{white-space:nowrap;font-size:12px;font-weight:600;border-radius:999px;padding:2px 10px;border:1px solid currentColor}
.chip-good{color:var(--good-ink)}.chip-bad{color:var(--critical)}.chip-muted{color:var(--muted)}
.stats{display:flex;flex-wrap:wrap;gap:6px 28px;margin:10px 0 4px}
.stat-label{font-size:12px;color:var(--ink-2)}.stat-value{font-size:22px;font-weight:600}
.legend{display:flex;gap:14px;font-size:12px;color:var(--ink-2);margin:4px 0}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
svg{width:100%;height:auto;display:block}
svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--axis);stroke-width:1}
svg .tick{fill:var(--muted);font-size:10px;font-variant-numeric:tabular-nums}
svg .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .dot{stroke:var(--surface-1);stroke-width:2}
svg .threshold{stroke:var(--critical);stroke-width:1.5}
svg .alert-line{stroke:var(--serious);stroke-width:1.5}svg .alert-label{fill:var(--ink-2);font-size:11px;font-weight:600}
svg .threshold-label{fill:var(--critical);font-size:11px;font-weight:600}
.breakdown{border-collapse:collapse;font-size:12px;margin-top:6px;min-width:220px}
.breakdown th,.breakdown td{text-align:left;padding:3px 10px 3px 0;border-bottom:1px solid var(--grid)}
.meter{margin-top:8px;font-size:12px;color:var(--ink-2)}.meter-head{display:flex;justify-content:space-between;margin-bottom:3px;font-variant-numeric:tabular-nums}
.meter-track{height:8px;border-radius:4px;background:var(--grid);overflow:hidden}.meter-fill{height:100%;border-radius:4px;min-width:2px}
.meter-good{background:var(--series-1)}.meter-bad{background:var(--critical)}
.num{text-align:right!important;font-variant-numeric:tabular-nums}.muted{color:var(--muted)}
"""


def render_html(
    config: dict,
    panels: list[Panel],
    summary: dict,
    window: Window,
    source: Path,
    theme: str,
    only: list[str] | None = None,
) -> str:
    tz_local = datetime.now().astimezone().tzinfo
    start_local = window.start.astimezone(tz_local).strftime("%Y-%m-%d %H:%M")
    end_local = window.end.astimezone(tz_local).strftime("%H:%M %Z")
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    body = "".join(render_panel(p, window) for p in panels if not only or p.config["id"] in only)
    return f"""<!doctype html>
<html lang="en"{f' data-theme="{theme}"' if theme in ("light", "dark") else ""}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{config['refresh_seconds']}">
<title>{html.escape(config['title'])}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<div class="top">
  <h1>{html.escape(config['title'])}</h1>
  <p>Time range: <b>last {config['time_range_minutes']} min</b> ({start_local} → {end_local}) · Auto-refresh: <b>{config['refresh_seconds']}s</b> · Bucket: 1 min · Requests: <b>{summary['requests']:,}</b> · Generated {generated} from <code>{html.escape(str(source))}</code></p>
</div>
<div class="grid">{body}</div>
</div></body></html>"""


# --------------------------------------------------------------------------- cli


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def screenshot(html_path: Path, png_path: Path, width: int = 1360, height: int = 1440) -> None:
    chrome = find_chrome()
    if chrome is None:
        raise SystemExit("Không tìm thấy Chrome/Chromium để chụp dashboard; mở file HTML và chụp thủ công.")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-prefers-reduced-motion",
            f"--window-size={width},{height}",
            f"--screenshot={png_path.resolve()}",
            html_path.resolve().as_uri(),
        ],
        check=True,
        capture_output=True,
    )


def build(args: argparse.Namespace) -> dict:
    payload = load_dashboard_config(args.config)
    config = payload["dashboard"]
    events = load_events(args.logs)
    window = resolve_window(events, config["time_range_minutes"], args.end)
    panels, summary = compute(config, events, window)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(config, panels, summary, window, args.logs, args.theme, getattr(args, "only", None)), encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Dựng dashboard 6 panel từ structured log")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config" / "dashboard.yaml")
    parser.add_argument("--logs", type=Path, default=Path("data/logs.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("data/dashboard.html"))
    parser.add_argument("--summary", type=Path, help="Ghi số liệu tổng hợp ra JSON")
    parser.add_argument("--end", default="now", help="'now' (mặc định), 'latest' hoặc ISO timestamp")
    parser.add_argument("--screenshot", type=Path, help="Chụp PNG bằng headless Chrome")
    parser.add_argument("--only", nargs="+", help="Chỉ render các panel id này (ảnh zoom cho incident)")
    parser.add_argument("--screenshot-height", type=int, default=1440)
    parser.add_argument("--theme", choices=["auto", "light", "dark"], default="auto")
    parser.add_argument("--watch", action="store_true", help="Dựng lại mỗi refresh_seconds")
    args = parser.parse_args()

    summary = build(args)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Dashboard: {args.out}")
    if args.screenshot:
        screenshot(args.out, args.screenshot, height=args.screenshot_height)
        print(f"Screenshot: {args.screenshot}")
    if args.watch:
        refresh = load_dashboard_config(args.config)["dashboard"]["refresh_seconds"]
        while True:
            time.sleep(refresh)
            build(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
