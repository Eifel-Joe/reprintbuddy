"""GET /live: change notices for an open page, as server-sent events (Spec 10.5.2)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from reprintbuddy.web import live as live_channel

router = APIRouter()


@router.get("/live")
async def live(request: Request, v: str | None = None) -> StreamingResponse:
    # No database session: the gate has checked the sign-in and closes its session as soon
    # as this response starts, so an open page holds no connection. The stream carries
    # topics, a state and the version, never contents. The time limits are read now, from
    # the module, so tests can shorten them.
    stream = live_channel.event_stream(
        request.app.state.live,
        version=request.app.state.version,
        page_version=v,
        heartbeat=live_channel.HEARTBEAT_SECONDS,
        coalesce=live_channel.COALESCE_SECONDS,
        lifetime=live_channel.MAX_STREAM_SECONDS,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
