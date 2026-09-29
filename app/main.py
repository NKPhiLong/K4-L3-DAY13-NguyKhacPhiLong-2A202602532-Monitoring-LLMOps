from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from structlog.contextvars import bind_contextvars

from .agent import LabAgent
from .audit import prune_audit, write_audit
from .incidents import disable, enable, status
from .logging_config import configure_logging, get_logger
from .metrics import record_error, snapshot
from .middleware import CorrelationIdMiddleware
from .pii import hash_user_id, summarize_text
from .schemas import ChatRequest, ChatResponse
from .tracing import tracing_enabled

configure_logging()
log = get_logger()
agent = LabAgent()


@asynccontextmanager
async def lifespan(_: FastAPI):
    pruned = prune_audit()
    log.info(
        "app_started",
        service=os.getenv("APP_NAME", "day13-monitoring-llmops-lab"),
        env=os.getenv("APP_ENV", "dev"),
        payload={"tracing_enabled": tracing_enabled(), "audit_records_pruned": pruned},
    )
    yield


app = FastAPI(title="Day 13 Monitoring & LLMOps Lab", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "tracing_enabled": tracing_enabled(), "incidents": status()}


@app.get("/metrics")
async def metrics() -> dict:
    return snapshot()


@app.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    # Bind một lần trước request_received để mọi log sau của request dùng chung context.
    bind_contextvars(
        user_id_hash=hash_user_id(body.user_id),
        session_id=body.session_id,
        feature=body.feature,
        model=agent.model,
        env=os.getenv("APP_ENV", "dev"),
    )

    log.info(
        "request_received",
        service="api",
        payload={"message_preview": summarize_text(body.message)},
    )
    try:
        # agent.run là code đồng bộ (retrieval + LLM). Chạy trong threadpool để một
        # dependency chậm không chặn event loop và xếp hàng mọi request khác.
        result = await run_in_threadpool(
            agent.run,
            user_id=body.user_id,
            feature=body.feature,
            session_id=body.session_id,
            message=body.message,
            correlation_id=request.state.correlation_id,
        )
        log.info(
            "response_sent",
            service="api",
            latency_ms=result.latency_ms,
            ttft_ms=result.ttft_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            quality_score=result.quality_score,
            tool_name="retrieval",
            tool_success=True,
            payload={"answer_preview": summarize_text(result.answer)},
        )
        return ChatResponse(
            answer=result.answer,
            correlation_id=request.state.correlation_id,
            latency_ms=result.latency_ms,
            ttft_ms=result.ttft_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            quality_score=result.quality_score,
        )
    except Exception as exc:  # pragma: no cover
        error_type = type(exc).__name__
        record_error(error_type)
        log.error(
            "request_failed",
            service="api",
            error_type=error_type,
            tool_name="retrieval" if isinstance(exc, RuntimeError) else None,
            tool_success=False if isinstance(exc, RuntimeError) else None,
            payload={"detail": str(exc), "message_preview": summarize_text(body.message)},
        )
        raise HTTPException(status_code=500, detail=error_type) from exc


def _toggle_incident(request: Request, name: str, turn_on: bool) -> JSONResponse:
    action = "incident.enable" if turn_on else "incident.disable"
    previous = status().get(name)
    audit_fields = dict(
        action=action,
        target=name,
        correlation_id=request.state.correlation_id,
        actor=request.headers.get("x-actor"),
        client_ip=request.client.host if request.client else None,
        previous_state=previous,
    )
    try:
        (enable if turn_on else disable)(name)
    except KeyError as exc:
        write_audit(result="rejected", new_state=None, **audit_fields)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    write_audit(result="success", new_state=status()[name], **audit_fields)
    log.warning(
        "incident_enabled" if turn_on else "incident_disabled",
        service="control",
        payload={"name": name},
    )
    return JSONResponse({"ok": True, "incidents": status()})


@app.post("/incidents/{name}/enable")
async def enable_incident(request: Request, name: str) -> JSONResponse:
    return _toggle_incident(request, name, turn_on=True)


@app.post("/incidents/{name}/disable")
async def disable_incident(request: Request, name: str) -> JSONResponse:
    return _toggle_incident(request, name, turn_on=False)
