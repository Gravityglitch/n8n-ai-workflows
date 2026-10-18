"""FastAPI app factory. `uvicorn sidecar.api:app` builds everything from Settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from sidecar.adapters import Adapters
from sidecar.approvals import ActionNotFound, ActionNotPending, ApprovalQueue
from sidecar.auth import require_api_key
from sidecar.config import Settings, get_settings
from sidecar.db import Database
from sidecar.llm import LLM, ClaudeLLM, FixtureLLM
from sidecar.models import (
    ActionCreate,
    ActionOut,
    Decision,
    Lead,
    ScoreResponse,
    SummariseRequest,
    SummariseResponse,
    TriageRequest,
    TriageResponse,
)
from sidecar.observability import RequestIdMiddleware, configure_logging
from sidecar.services import IdempotencyConflict, Services

log = structlog.get_logger(__name__)


def build_llm(settings: Settings) -> LLM:
    if settings.llm_backend == "claude" and settings.anthropic_api_key:
        return ClaudeLLM(
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            api_key=settings.anthropic_api_key,
        )
    if settings.llm_backend == "claude":
        log.warning("llm.no_api_key", fallback="fixture")
    return FixtureLLM()


def create_app(
    settings: Settings | None = None,
    llm: LLM | None = None,
    adapters: Adapters | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    llm = llm or build_llm(settings)
    adapters = adapters or Adapters.from_settings(settings)
    db = Database(settings.database_url)
    services = Services(llm, db, settings.score_threshold)
    queue = ApprovalQueue(db, adapters)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            app_env=settings.app_env,
            llm=llm.name,
            auth_enabled=settings.auth_enabled,
            crm_mode=settings.crm_mode,
            slack_mode=settings.slack_mode,
            email_mode=settings.email_mode,
        )
        yield

    app = FastAPI(title="n8n AI sidecar", version="1.0.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.state.settings = settings
    app.state.services = services
    app.state.queue = queue
    app.state.llm = llm
    app.state.adapters = adapters

    auth = Depends(require_api_key)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "llm": llm.name, "auth_enabled": settings.auth_enabled}

    @app.post("/score", response_model=ScoreResponse, dependencies=[auth])
    def score(
        lead: Lead,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ScoreResponse:
        try:
            result, replayed = services.score(lead, idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(409, f"Idempotency-Key {exc} reused with a different body") from exc
        response.headers["Idempotent-Replay"] = "true" if replayed else "false"
        return result

    @app.post("/triage", response_model=TriageResponse, dependencies=[auth])
    def triage(request: TriageRequest) -> TriageResponse:
        return services.triage(request)

    @app.post("/summarise", response_model=SummariseResponse, dependencies=[auth])
    def summarise(request: SummariseRequest) -> SummariseResponse:
        return services.summarise(request)

    @app.post("/actions", response_model=ActionOut, status_code=201, dependencies=[auth])
    def create_action(request: ActionCreate) -> ActionOut:
        return queue.create(request)

    @app.get("/pending", response_model=list[ActionOut], dependencies=[auth])
    def pending() -> list[ActionOut]:
        return queue.pending()

    @app.get("/actions/{action_id}", response_model=ActionOut, dependencies=[auth])
    def get_action(action_id: str) -> ActionOut:
        try:
            return queue.get(action_id)
        except ActionNotFound as exc:
            raise HTTPException(404, "action not found") from exc

    @app.post("/approve/{action_id}", response_model=ActionOut, dependencies=[auth])
    def approve(action_id: str, decision: Decision | None = None) -> ActionOut:
        decision = decision or Decision()
        return _decide(queue.approve, action_id, decision)

    @app.post("/reject/{action_id}", response_model=ActionOut, dependencies=[auth])
    def reject(action_id: str, decision: Decision | None = None) -> ActionOut:
        decision = decision or Decision()
        return _decide(queue.reject, action_id, decision)

    return app


def _decide(fn, action_id: str, decision: Decision) -> ActionOut:  # noqa: ANN001
    try:
        return fn(action_id, decision.decided_by, decision.note)
    except ActionNotFound as exc:
        raise HTTPException(404, "action not found") from exc
    except ActionNotPending as exc:
        raise HTTPException(409, str(exc)) from exc


def get_queue(request: Request) -> ApprovalQueue:
    return request.app.state.queue


app = create_app()
