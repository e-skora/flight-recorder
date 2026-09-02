"""Application factory."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

from flight_recorder.collector.api import router as collector_router
from flight_recorder.collector.schema import envelope_schema
from flight_recorder.collector.service import Collector
from flight_recorder.ledger.database import db_path_from_env, make_engine
from flight_recorder.web.routes import router as web_router

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(db_path: Path | str | None = None) -> FastAPI:
    engine = make_engine(db_path if db_path is not None else db_path_from_env())
    app = FastAPI(
        title="GTM Flight Recorder",
        version="0.1.0",
        description=(
            "Observability for automated revenue decisions. "
            "All accounts, vendor-like events, decisions, and outcomes are synthetic."
        ),
    )
    app.state.engine = engine
    app.state.collector = Collector(engine)
    app.include_router(collector_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        # The envelope is validated from the raw body, so its component
        # schemas are registered here to keep /docs as the published contract.
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(envelope_schema().get("$defs", {}))
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
