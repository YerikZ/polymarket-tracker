from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["alerts"])
ws_router = APIRouter(tags=["alerts-ws"])


@router.get("/alerts")
async def get_alerts(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    since_id: int = Query(0, ge=0),
    wallet_address: str = Query("", alias="wallet_address"),
):
    storage = request.app.state.storage
    alerts = await asyncio.to_thread(_fetch_alerts, storage, limit, since_id, wallet_address or None)
    return alerts


def _fetch_alerts(storage, limit: int, since_id: int, wallet_address: str | None = None) -> list[dict]:
    import psycopg2.extras
    from polymarket import db

    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []
            if since_id:
                conditions.append("id > %s")
                params.append(since_id)
            if wallet_address:
                conditions.append("wallet_address = %s")
                params.append(wallet_address)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)
            cur.execute(
                f"SELECT * FROM alerts {where} ORDER BY id DESC LIMIT %s",
                params,
            )
            from polymarket.storage import _row_to_dict
            return [_row_to_dict(r) for r in cur.fetchall()]


def _fetch_copier_updates(storage, ids: list[int]) -> list[dict]:
    """Re-fetch rows whose copier_status has been written after they were sent."""
    if not ids:
        return []
    import psycopg2.extras
    from polymarket import db
    from polymarket.storage import _row_to_dict

    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM alerts WHERE id = ANY(%s) AND copier_status IS NOT NULL",
                (ids,),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


@ws_router.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket):
    await websocket.accept()
    storage = websocket.app.state.storage

    # Seed with last 20 signals
    seed = await asyncio.to_thread(_fetch_alerts, storage, 20, 0)
    await websocket.send_text(json.dumps({"type": "seed", "data": seed}))

    # Track max id seen; also keep a short window of recently-sent ids that
    # may not yet have a copier_status written (copier runs just after insert).
    last_id = seed[0]["id"] if seed else 0
    # ids sent without copier_status → watch for the update for up to ~5 polls
    pending_copier: dict[int, int] = {}   # id → polls_remaining

    try:
        while True:
            await asyncio.sleep(1)

            # 1. New rows
            new_rows = await asyncio.to_thread(_fetch_alerts, storage, 20, last_id)
            if new_rows:
                last_id = new_rows[0]["id"]
                await websocket.send_text(json.dumps({"type": "new", "data": new_rows}))
                # Track rows that were sent without a copier decision yet
                for row in new_rows:
                    if row.get("copier_status") is None:
                        pending_copier[row["id"]] = 30  # retry for up to 30 seconds

            # 2. Copier updates for recently-sent rows still missing a status
            if pending_copier:
                updated = await asyncio.to_thread(
                    _fetch_copier_updates, storage, list(pending_copier.keys())
                )
                if updated:
                    await websocket.send_text(
                        json.dumps({"type": "update", "data": updated})
                    )
                # Decrement countdown; drop ids that got an update or timed out
                updated_ids = {r["id"] for r in updated}
                pending_copier = {
                    rid: count - 1
                    for rid, count in pending_copier.items()
                    if rid not in updated_ids and count > 1
                }

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WS signals error: %s", exc)
