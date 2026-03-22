import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


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

    def token_prices(self, condition_ids: list[str]) -> dict[str, float]:
        """Return {token_id: current_price} for all tokens in the given markets."""
        if not condition_ids:
            return {}
        try:
            raw = self.markets(condition_ids)
        except Exception as exc:
            logger.warning("markets fetch failed: %s", exc)
            return {}

        prices: dict[str, float] = {}
        for market in raw if isinstance(raw, list) else [raw]:
            closed = market.get("closed", False)
            for token in market.get("tokens", []):
                tid = token.get("token_id") or token.get("tokenId", "")
                if not tid:
                    continue
                if token.get("winner"):
                    prices[tid] = 1.0   # resolved winner → full payout
                elif closed:
                    prices[tid] = 0.0   # resolved loser → worthless
                else:
                    prices[tid] = float(token.get("price", 0))
        return prices
