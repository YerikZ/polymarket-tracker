from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["alerts"])


@router.get("/alerts")
async def get_alerts(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    since_id: int = Query(0, ge=0),
):
    storage = request.app.state.storage
    alerts = await asyncio.to_thread(_fetch_alerts, storage, limit, since_id)
    return alerts


def _fetch_alerts(storage, limit: int, since_id: int) -> list[dict]:
    import psycopg2.extras
    from polymarket import db

    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if since_id:
                cur.execute(
                    "SELECT * FROM alerts WHERE id > %s ORDER BY id DESC LIMIT %s",
                    (since_id, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM alerts ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
            from polymarket.storage import _row_to_dict
            return [_row_to_dict(r) for r in cur.fetchall()]


@router.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket, request: Request):
    await websocket.accept()
    storage = websocket.app.state.storage

    # Seed with last 20 signals
    seed = await asyncio.to_thread(_fetch_alerts, storage, 20, 0)
    await websocket.send_text(json.dumps({"type": "seed", "data": seed}))

    # Track max id seen
    last_id = seed[0]["id"] if seed else 0

    try:
        while True:
            await asyncio.sleep(1)
            new_rows = await asyncio.to_thread(_fetch_alerts, storage, 20, last_id)
            if new_rows:
                last_id = new_rows[0]["id"]
                await websocket.send_text(
                    json.dumps({"type": "new", "data": new_rows})
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WS signals error: %s", exc)
