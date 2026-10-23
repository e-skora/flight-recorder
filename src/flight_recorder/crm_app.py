"""Explicit local CRM app only; default/public app factories remain untouched."""

from contextlib import asynccontextmanager

from flight_recorder.adapters.crm.contracts import require
from flight_recorder.adapters.crm.oauth import OAuthSessionStore
from flight_recorder.adapters.crm.routes import crm_router, ingress_secret
from flight_recorder.adapters.crm.runtime import CRMRuntime
from flight_recorder.app import create_app
from flight_recorder.web.crm_decision_view import crm_status_router


def create_crm_app(config, *, runtime=None, background=True, writer_capable=False, read_mode=False):
    require(type(writer_capable) is bool, "invalid_writer_mode")
    runtime = CRMRuntime(config, writer_capable=writer_capable) if runtime is None else runtime
    require(
        type(runtime) is CRMRuntime
        and runtime.config.canonical_bytes == config.canonical_bytes
        and runtime.writer_capable == writer_capable,
        "invalid_crm_runtime",
    )
    app = create_app(config.domain_path)
    unused_engine = app.state.engine
    app.state.engine = runtime.collector.engine
    app.state.collector = runtime.collector
    unused_engine.dispose()
    app.state.crm_runtime = runtime
    app.state.crm_mode = True
    app.description = (
        "Local CRM history for fictional test records. Each observation names its origin."
    )
    app.state.oauth_sessions = OAuthSessionStore()
    app.include_router(crm_router(read_mode=read_mode))
    app.include_router(crm_status_router())

    @asynccontextmanager
    async def lifespan(app):
        try:
            app.state.ingress_secret = ingress_secret(config)
            runtime.start(background=background)
            yield
        finally:
            runtime.close()
            app.state.ingress_secret = None
            app.state.engine.dispose()

    app.router.lifespan_context = lifespan
    return app


def create_crm_network_check_app(config, qualification_path, evidence_directory, *, clock=None):
    from datetime import UTC, datetime

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse

    from flight_recorder.adapters.crm.contracts import CRMRefused
    from flight_recorder.adapters.crm.read_control import NetworkCheckGate
    from flight_recorder.adapters.crm.routes import _body

    gate = NetworkCheckGate(
        config,
        qualification_path,
        evidence_directory,
        clock=clock if clock is not None else lambda: datetime.now(UTC),
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ingress_secret = gate.secret

    @app.post("/api/internal/crm/network-check")
    async def network_check(request: Request):
        try:
            gate.check()
        except CRMRefused:
            raise HTTPException(403, "network_qualification_refused") from None
        body = await _body(request)
        try:
            result = gate.observe(body)
        except CRMRefused:
            raise HTTPException(400, "network_probe_refused") from None
        return JSONResponse(
            result, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )

    return app


def create_crm_read_app(gate, *, runtime=None):
    from flight_recorder.adapters.crm.read_control import ReadSessionGate
    from flight_recorder.adapters.crm.read_control_routes import read_control_router

    require(type(gate) is ReadSessionGate, "live_read_authority_required")
    runtime = CRMRuntime(gate.config) if runtime is None else runtime
    runtime.read_gate = gate
    app = create_crm_app(
        gate.config, runtime=runtime, background=False, writer_capable=False, read_mode=True
    )
    app.include_router(read_control_router(runtime))

    @app.middleware("http")
    async def private_read_response(request, call_next):
        response = await call_next(request)
        if (
            request.url.path.startswith("/crm/read")
            or request.url.path == "/api/internal/crm/oauth/salesforce/callback"
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self' https:; frame-ancestors 'none'; base-uri 'none'"
            )
        return response

    return app
