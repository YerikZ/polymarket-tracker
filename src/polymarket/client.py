import json
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


class PolymarketClient:
    def __init__(self, request_delay: float = 0.5, max_retries: int = 3):
        self._delay = request_delay
        self._max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._last_call: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        wait = self._delay - elapsed
        if wait > 0:
            time.sleep(wait)

    def get(self, base: str, path: str, params: dict | None = None) -> Any:
        url = base.rstrip("/") + "/" + path.lstrip("/")
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            self._throttle()
            self._last_call = time.monotonic()
            try:
                resp = self._session.get(url, params=params or {}, timeout=15)
                if resp.status_code == 404:
                    # Not found — no point retrying, raise immediately
                    resp.raise_for_status()
                if resp.status_code == 429:
                    wait = 2 ** attempt * 2
                    logger.warning("Rate limited. Sleeping %ss (attempt %d)", wait, attempt + 1)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = 2 ** attempt
                    logger.warning("Server error %d. Retrying in %ss", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("Request failed (%s). Attempt %d/%d", exc, attempt + 1, self._max_retries)
                time.sleep(2 ** attempt)

        raise RuntimeError(f"Failed after {self._max_retries} attempts: {last_exc}")

    # --- Convenience wrappers ---

    def leaderboard(
        self,
        limit: int = 50,
        order_by: str = "PNL",
        time_period: str = "ALL",
        category: str = "OVERALL",
    ) -> list[dict]:
        return self.get(
            DATA_API,
            "/v1/leaderboard",
            params={
                "limit": limit,
                "orderBy": order_by,
                "timePeriod": time_period,
                "category": category,
            },
        )

    def profile(self, username: str) -> dict | None:
        """Resolve a username to its profile (contains proxyWallet)."""
        try:
            result = self.get(DATA_API, "/profiles", params={"username": username})
            # API may return a list or a single object
            if isinstance(result, list):
                return result[0] if result else None
            return result
        except Exception as exc:
            logger.debug("Profile lookup failed for %s: %s", username, exc)
            return None

    def positions(self, address: str, limit: int = 500) -> list[dict]:
        return self.get(
            DATA_API,
            "/positions",
            params={
                "user": address,
                "limit": limit,
                "sizeThreshold": "0",
                "sortBy": "CURRENT",
                "sortDirection": "DESC",
            },
        )

    def activity(self, address: str, limit: int = 100, trade_type: str = "TRADE") -> list[dict]:
        return self.get(
            DATA_API,
            "/activity",
            params={"user": address, "limit": limit, "type": trade_type, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        )

    def markets(self, condition_ids: list[str]) -> list[dict]:
        return self.get(
            GAMMA_API,
            "/markets",
            params={"condition_ids": ",".join(condition_ids)},
        )

    def market_questions(
        self,
        condition_ids: list[str] | None = None,
        token_ids: list[str] | None = None,
    ) -> dict[str, str]:
        """Return {key: question_title} for the given markets.

        Keys are condition_ids and/or token_ids — both are included so callers
        can look up by either without caring about format.

        Important: the Gamma API sometimes returns markets without a `tokens`
        array (common for older markets).  When querying by token_id we
        therefore ALWAYS map the *queried* token_id → title directly so the
        caller can reliably look up by the original token_id.
        """
        results: dict[str, str] = {}

        def _index_market(m: dict, extra_tid: str = "") -> None:
            """Add conditionId, all token token_ids, and extra_tid to results."""
            title = (
                m.get("question") or m.get("title") or m.get("name") or ""
            ).strip()
            if not title:
                return
            cid = m.get("conditionId", "")
            if cid:
                results[cid] = title
            for tok in m.get("tokens", []):
                tid = tok.get("token_id") or tok.get("tokenId", "")
                if tid:
                    results[str(tid)] = title
            # Always alias the queried token_id even if 'tokens' array is absent
            if extra_tid:
                results[extra_tid] = title

        if condition_ids:
            try:
                raw = self.markets(condition_ids)
                for m in (raw if isinstance(raw, list) else [raw]):
                    _index_market(m)
            except Exception as exc:
                logger.warning("market_questions (by conditionId) failed: %s", exc)

        # Query individually for token_ids not already covered by conditionId lookup
        for tid in (token_ids or []):
            if not tid or tid in results:
                continue
            try:
                raw = self.get(GAMMA_API, "/markets", {"token_id": tid})
                for m in (raw if isinstance(raw, list) else [raw]):
                    _index_market(m, extra_tid=tid)
            except Exception as exc:
                logger.debug("market_questions token lookup failed %s…: %s", tid[:16], exc)

        return results

    def market_statuses(self, condition_ids: list[str]) -> dict[str, dict]:
        """Return resolution status for each condition_id from the GAMMA API.

        Result shape per condition_id:
        {
            "closed":            bool,   # market no longer accepting orders
            "accepting_orders":  bool,   # True only while live
            "resolved":          bool,   # UMA oracle has settled the market
            "winner_outcome":    str,    # e.g. "Yes", "No", "Yokohama F·Marinos"
            "winner_token_id":   str,    # clobTokenId of the winning outcome
        }
        """
        if not condition_ids:
            return {}

        statuses: dict[str, dict] = {}

        def _parse_market(m: dict) -> None:
            cid = m.get("conditionId", "")
            if not cid:
                return

            closed           = bool(m.get("closed", False))
            accepting_orders = bool(m.get("acceptingOrders", True))
            uma_status       = m.get("umaResolutionStatus") or ""
            resolved         = uma_status == "resolved" or (closed and not accepting_orders)

            # Determine winner from outcomePrices + clobTokenIds
            winner_outcome  = ""
            winner_token_id = ""
            try:
                raw_prices  = m.get("outcomePrices") or "[]"
                raw_outcomes = m.get("outcomes") or "[]"
                raw_tokens  = m.get("clobTokenIds") or "[]"

                prices   = json.loads(raw_prices)  if isinstance(raw_prices,  str) else raw_prices
                outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                tokens   = json.loads(raw_tokens)  if isinstance(raw_tokens,  str) else raw_tokens

                if prices:
                    winner_idx = max(range(len(prices)), key=lambda i: float(prices[i]))
                    # Only declare a winner if the max price is clearly dominant (>= 0.95)
                    if float(prices[winner_idx]) >= 0.95:
                        winner_outcome  = outcomes[winner_idx] if winner_idx < len(outcomes) else ""
                        winner_token_id = str(tokens[winner_idx])  if winner_idx < len(tokens)  else ""
            except Exception as exc:
                logger.debug("Could not parse resolution for %s: %s", cid[:16], exc)

            statuses[cid] = {
                "closed":           closed,
                "accepting_orders": accepting_orders,
                "resolved":         resolved,
                "winner_outcome":   winner_outcome,
                "winner_token_id":  winner_token_id,
            }

        # Batch fetch — GAMMA supports comma-separated condition_ids
        chunk_size = 20
        for i in range(0, len(condition_ids), chunk_size):
            chunk = condition_ids[i : i + chunk_size]
            try:
                raw = self.markets(chunk)
                for m in (raw if isinstance(raw, list) else [raw]):
                    _parse_market(m)
            except Exception as exc:
                logger.warning("market_statuses batch failed: %s", exc)

        return statuses

    def token_prices(self, token_ids: list[str]) -> dict[str, float]:
        """Return {token_id: current_price} using the CLOB midpoint API.

        The GAMMA markets API does not reliably return token prices (tokens array
        is frequently null for older markets, and the bulk condition_ids parameter
        silently returns empty results). The CLOB /midpoint endpoint is queried
        per token_id and is the authoritative real-time price source.
        """
        prices: dict[str, float] = {}
        for tid in token_ids:
            if not tid:
                continue
            try:
                raw = self.get(CLOB_API, "/midpoint", params={"token_id": tid})
                mid = raw.get("mid") if isinstance(raw, dict) else None
                if mid is not None:
                    prices[tid] = float(mid)
            except Exception as exc:
                # 404 is expected for resolved/expired tokens — suppress to debug
                logger.debug("midpoint lookup skipped for token %s…: %s", tid[:16], exc)
        return prices
