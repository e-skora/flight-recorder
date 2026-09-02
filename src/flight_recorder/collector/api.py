"""HTTP surface of the collector."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from flight_recorder.collector.schema import envelope_schema
from flight_recorder.collector.service import Collector, CollectorError

router = APIRouter(prefix="/api/v1", tags=["collector"])


@router.post(
    "/decision-events",
    summary="Submit one decision-event envelope (schema version 1)",
    status_code=201,
    responses={
        200: {"description": "Duplicate: identical canonical content already stored"},
        201: {"description": "Created"},
        409: {"description": "Identity conflict; nothing written"},
        422: {"description": "Rejected by strict validation or account rules; nothing written"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": envelope_schema()}},
        }
    },
)
async def post_decision_event(request: Request) -> JSONResponse:
    collector: Collector = request.app.state.collector
    body = await request.body()
    try:
        result = collector.ingest_json(body)
    except CollectorError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)
    return JSONResponse(status_code=result.http_status, content=result.body())
