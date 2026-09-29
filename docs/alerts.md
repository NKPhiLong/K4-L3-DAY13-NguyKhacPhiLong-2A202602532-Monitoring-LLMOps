# Template Alert và Runbook

Mỗi alert phải dựa trên triệu chứng người dùng hoặc SLO, không dựa trực tiếp vào tên implementation nội bộ.

Rule máy đọc được nằm ở [`config/alert_rules.yaml`](../config/alert_rules.yaml); SLO và error budget ở [`config/slo.yaml`](../config/slo.yaml). Dashboard dùng để kiểm tra: `python scripts/build_dashboard.py` (mở `data/dashboard.html`).

Quy trình chung cho mọi alert: **Metrics → Logs → Traces**. Dùng dashboard xác định khoảng thời gian, lọc `data/logs.jsonl` trong khoảng đó lấy `correlation_id`, rồi mở trace Langfuse có metadata `correlation_id` trùng để xem span nào chậm/lỗi.

```bash
# Lọc log bất thường (ví dụ latency > 2000 ms) và lấy correlation_id
python - <<'EOF'
import json
for line in open("data/logs.jsonl", encoding="utf-8"):
    e = json.loads(line)
    if e.get("event") == "response_sent" and e.get("latency_ms", 0) > 2000:
        print(e["ts"], e["correlation_id"], e["feature"], e["latency_ms"])
EOF
```

## Alert 1

- Tên: `HighLatencyP95`
- Severity: P2-warning (cảnh báo sớm; nâng P1 nếu P95 > 3000 ms, tức vi phạm SLO, kéo dài 10 phút)
- Duration: 5m
- Kênh thông báo: Slack `#k4-l3a-day13-alerts`
- SLI/SLO liên quan: latency của SLO `fast_successful_requests` (good khi `latency_ms <= 3000`, mục tiêu 99.5%/28 ngày); panel **Latency percentiles and TTFT**.
- Điều kiện và thời gian duy trì: P95 `response_sent.latency_ms` trong 5 phút gần nhất > 2000 ms, có ít nhất 10 response, và điều kiện đúng liên tục 5 phút.
- Ảnh hưởng tới người dùng: câu trả lời chậm rõ rệt (baseline ≈ 160 ms); nếu tiếp tục tăng sẽ vượt 3000 ms và bắt đầu tiêu error budget.
- Ba bước kiểm tra đầu tiên:
  1. Dashboard: so sánh P50/P95/P99 với TTFT P95. TTFT bình thường nhưng P95 cao → chậm ở trước bước generation (retrieval/prompt); TTFT cũng cao → chậm ở LLM. Xem chậm tập trung ở `feature` nào.
  2. Logs: lọc `response_sent` có `latency_ms > 2000` trong khoảng alert, lấy vài `correlation_id`, kiểm tra có `incident_enabled` hoặc deploy/prompt label mới ngay trước đó.
  3. Traces: mở trace có cùng `correlation_id` trên Langfuse, so thời lượng span `retrieval` với `llm-generation` để khoanh vùng span chậm.
- Mitigation tạm thời: nếu span `retrieval` chậm → chuyển sang fallback doc/giảm top-k, tắt nguồn retrieval lỗi (practice: `python scripts/inject_incident.py --scenario rag_slow --disable`); nếu `llm-generation` chậm → giảm max output tokens hoặc chuyển model nhanh hơn; nếu do prompt mới → rollback label `production` về version trước.
- Owner: nguykhacphilong (on-call AI platform)

## Alert 2

- Tên: `HighErrorRate`
- Severity: P1-critical
- Duration: 5m
- Kênh thông báo: Slack `#k4-l3a-day13-alerts` (kèm page on-call)
- SLI/SLO liên quan: availability của SLO `fast_successful_requests` (mọi `request_failed` là bad event); guardrail `error_rate_pct_max: 2`; panel **Error rate and retrieval success**.
- Điều kiện và thời gian duy trì: `count(request_failed) / count(request_received) * 100 > 2` trong cửa sổ 5 phút, có ít nhất 20 request, đúng liên tục 5 phút.
- Ảnh hưởng tới người dùng: người dùng nhận HTTP 500, không có câu trả lời; ở 2% lỗi/5 phút error budget 0.5% bị tiêu nhanh gấp 4 lần mức cho phép.
- Ba bước kiểm tra đầu tiên:
  1. Dashboard: xem breakdown `error_type` và `retrieval success`. Retrieval success giảm cùng lúc → lỗi nằm ở tool retrieval.
  2. Logs: lọc `request_failed` trong khoảng alert, đọc `error_type`, `tool_name`, `tool_success`, `payload.detail` (đã scrub PII) và lấy `correlation_id`.
  3. Traces: mở trace cùng `correlation_id`, tìm observation có level `ERROR` (ví dụ span `retrieval` báo `Vector store timeout`) và status message.
- Mitigation tạm thời: tắt/cách ly dependency lỗi (practice: `python scripts/inject_incident.py --scenario tool_fail --disable`), trả lời fallback không dùng retrieval thay vì 500, rollback prompt/model vừa đổi; thông báo trạng thái trong kênh Slack.
- Owner: nguykhacphilong (on-call AI platform)

## Alert 3

- Tên: `CostBudgetBurn`
- Severity: P2-warning
- Duration: 15m
- Kênh thông báo: Slack `#k4-l3a-day13-alerts`
- SLI/SLO liên quan: guardrail chi phí `daily_cost_usd_max: 2.5`; panel **Cost over time** và **Input and output tokens**.
- Điều kiện và thời gian duy trì: tổng `cost_usd` trong 1 giờ > 0.21 USD (gấp 2 nhịp chi đều 2.5 USD/ngày) **hoặc** chi phí trung bình mỗi request trong 15 phút > 0.004 USD (≈ 2× baseline 0.0019 USD), đúng liên tục 15 phút.
- Ảnh hưởng tới người dùng: chưa ảnh hưởng trực tiếp tới trải nghiệm nhưng sẽ vượt ngân sách ngày; response dài bất thường thường đi kèm latency tăng và chất lượng giảm.
- Ba bước kiểm tra đầu tiên:
  1. Dashboard: so traffic với cost. Cost tăng mà traffic không tăng → cost/request tăng; xem `tokens_out` hay `tokens_in` tăng.
  2. Logs: lọc `response_sent` có `cost_usd` hoặc `tokens_out` cao nhất, lấy `correlation_id`, xem `feature` và `model` chung.
  3. Traces: mở trace cùng `correlation_id`, kiểm tra generation `usage`/`cost` và `prompt_version`/`prompt_label` để biết có prompt mới làm output dài hơn hay model đắt hơn.
- Mitigation tạm thời: rollback label `production` về prompt version trước, đặt giới hạn `max_tokens`, chuyển feature bị ảnh hưởng sang model rẻ hơn, bật cache cho câu hỏi lặp (practice: `python scripts/inject_incident.py --scenario cost_spike --disable`).
- Owner: nguykhacphilong (LLMOps / FinOps)
