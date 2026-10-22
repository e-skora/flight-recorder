"""Explicit local CRM app only; default/public app factories remain untouched."""

from contextlib import asynccontextmanager

from flight_recorder.adapters.crm.contracts import require
from flight_recorder.adapters.crm.oauth import OAuthSessionStore
from flight_recorder.adapters.crm.routes import crm_router, ingress_secret
from flight_recorder.adapters.crm.runtime import CRMRuntime
from flight_recorder.app import create_app
from flight_recorder.web.crm_decision_view import crm_status_router


def create_crm_app(config, *, runtime=None, background=True):
    runtime = CRMRuntime(config) if runtime is None else runtime
    require(
        type(runtime) is CRMRuntime and runtime.config.canonical_bytes == config.canonical_bytes,
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
    app.include_router(crm_router())
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
