"""
CRUD endpoints for basket (consensus copy group) management.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/baskets", tags=["baskets"])


# ── Request bodies ────────────────────────────────────────────────────────────

class BasketCreate(BaseModel):
    name: str
    category: str = ""
    wallet_addresses: list[str] = Field(default_factory=list)
    consensus_threshold: float = Field(0.8, ge=0.5, le=1.0)


class BasketUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    wallet_addresses: list[str] | None = None
    consensus_threshold: float | None = Field(None, ge=0.5, le=1.0)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_baskets(request: Request):
    """Return all active baskets."""
    storage = request.app.state.storage
    return await asyncio.to_thread(storage.get_baskets, True)


@router.post("", status_code=201)
async def create_basket(body: BasketCreate, request: Request):
    """Create a new basket."""
    storage = request.app.state.storage
    return await asyncio.to_thread(
        storage.create_basket,
        body.name,
        body.category,
        body.wallet_addresses,
        body.consensus_threshold,
    )


@router.put("/{basket_id}")
async def update_basket(basket_id: int, body: BasketUpdate, request: Request):
    """Update basket fields (only non-null fields are changed)."""
    storage = request.app.state.storage
    result = await asyncio.to_thread(
        storage.update_basket,
        basket_id,
        body.name,
        body.category,
        body.wallet_addresses,
        body.consensus_threshold,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Basket not found")
    return result


@router.delete("/{basket_id}", status_code=204)
async def delete_basket(basket_id: int, request: Request):
    """Soft-delete a basket (sets active=FALSE)."""
    storage = request.app.state.storage
    found = await asyncio.to_thread(storage.delete_basket, basket_id)
    if not found:
        raise HTTPException(status_code=404, detail="Basket not found")


@router.get("/{basket_id}/consensus")
async def check_basket_consensus(
    basket_id: int,
    request: Request,
    condition_id: str = Query(..., description="Market condition_id to check"),
    outcome: str = Query(..., description="Outcome label, e.g. 'Yes'"),
    within_hours: int = Query(48, ge=1, le=168, description="Look-back window in hours"),
):
    """
    Check current consensus for a given market+outcome across all basket wallets.
    Used by the BasketManager UI 'Check Consensus' button.
    """
    from polymarket.basket import check_consensus

    storage = request.app.state.storage

    def _run():
        basket = storage.get_basket(basket_id)
        if not basket:
            return None
        addresses = basket.get("wallet_addresses") or []
        recent_buys = storage.get_recent_buys_for_condition(
            addresses, condition_id, within_hours=within_hours
        )
        return check_consensus(basket, condition_id, outcome, recent_buys)

    result = await asyncio.to_thread(_run)
    if result is None:
        raise HTTPException(status_code=404, detail="Basket not found")
    return result
