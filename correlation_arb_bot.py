"""
Kalshi Correlation Arb Bot
Exploits logical pricing inconsistencies between correlated Kalshi markets.

Example: If "SPY > 500" has YES=70% and "SPY > 520" has YES=65%, that's impossible —
the 520 market must be ≤ the 500 market. Trade the mispriced leg.

Also checks: If P(A) + P(not-A) ≠ 100% (should always equal 100% via YES+NO).
And: If P(A AND B) > min(P(A), P(B)) — logical impossibility.

These are pure arbitrage with no directional risk.
"""

import asyncio
import os
from flask import Flask, jsonify
import threading
import json
import time
import logging
import base64
import re
import uuid
from dataclasses import dataclass
from typing import Optional
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
import httpx
from dotenv import load_dotenv
from risk_guard import RiskManager

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("correlation_arb_bot")


def _normalize_market(m: dict) -> dict:
    """Normalize Kalshi API v2 dollar-denominated fields to legacy field names."""
    if "yes_bid_dollars" in m and "yes_bid" not in m:
        m["yes_bid"] = m.get("yes_bid_dollars")
        m["yes_ask"] = m.get("yes_ask_dollars")
        m["no_bid"] = m.get("no_bid_dollars")
        m["no_ask"] = m.get("no_ask_dollars")
        m["last_price"] = m.get("last_price_dollars")
        m["volume"] = m.get("volume_fp") or m.get("volume_24h_fp") or m.get("volume", 0)
        m["open_interest"] = m.get("open_interest_fp") or m.get("open_interest", 0)
    for k in ["yes_bid", "yes_ask", "no_bid", "no_ask", "last_price"]:
        v = m.get(k)
        if isinstance(v, str):
            try: m[k] = float(v)
            except: pass
    return m


# ── CONFIG ────────────────────────────────────────────────────────────────────
KALSHI_BASE       = os.getenv("KALSHI_BASE", "https://api.elections.kalshi.com")
KALSHI_API_URL    = os.getenv("KALSHI_API_URL", f"{KALSHI_BASE}/trade-api/v2")
KALSHI_API_KEY    = os.getenv("KALSHI_API_KEY", "")
KALSHI_KEY_ID     = os.getenv("KALSHI_KEY_ID", "")
PAPER_MODE        = os.getenv("PAPER_MODE", "true").lower() == "true"
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "600"))   # 10 min
BET_SIZE_CENTS    = int(os.getenv("BET_SIZE_CENTS", "1000"))      # $10
KELLY_FRACTION    = float(os.getenv("KELLY_FRACTION", "1.0"))
MIN_ARB_CENTS     = int(os.getenv("MIN_ARB_CENTS", "8"))          # min 8¢ mispricing to trade
MAX_PRICE         = int(os.getenv("MAX_PRICE", "95"))             # don't buy >95¢

# Series with threshold-type markets (monotonic: higher threshold = lower prob)
THRESHOLD_SERIES = [
    "KXSPY",   # SPY price above X
    "KXQQQ",   # QQQ price above X
    "KXBTC",   # BTC above X
    "KXETH",   # ETH above X
    "KXNVDA",  # NVDA above X
    "KXAAPL",  # AAPL above X
    "KXHIGH",  # temperature high above X
    "KXLOW",   # temperature low above X
    "KXCPI",   # CPI above X%
    "KXNFP",   # NFP above X
]

# ── AUTH ──────────────────────────────────────────────────────────────────────
def _load_private_key():
    pem_str = os.getenv("KALSHI_PRIVATE_KEY", "")
    if not pem_str:
        return None
    if "\\n" in pem_str:
        pem_str = pem_str.replace("\\n", "\n")
    return serialization.load_pem_private_key(pem_str.encode(), password=None)

_PRIVATE_KEY = _load_private_key()

def _sign_request(method, path, ts, body=""):
    if not _PRIVATE_KEY:
        return ""
    try:
        msg = f"{ts}{method.upper()}{path}{body}".encode()
        sig = _PRIVATE_KEY.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return base64.b64encode(sig).decode()
    except Exception:
        return ""

def _auth_headers(method, path, body=""):
    ts = int(time.time() * 1000)
    return {"Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": _sign_request(method, path, ts, body)}

# ── KALSHI API ────────────────────────────────────────────────────────────────
async def get_series_markets(client: httpx.AsyncClient, series: str) -> list:
    """Get all open markets for a series, sorted by close time."""
    path = f"/markets?series_ticker={series}&status=open&limit=100"
    try:
        r = await client.get(f"{KALSHI_API_URL}{path}",
                             headers=_auth_headers("GET", path), timeout=10)
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            return sorted(markets, key=lambda m: m.get("close_time", ""))
    except Exception:
        pass
    return []

async def place_order(client: httpx.AsyncClient, ticker: str, side: str,
                      price_cents: int, count: int) -> Optional[dict]:
    path = "/portfolio/orders"
    body = json.dumps({
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "action": "buy",
        "type": "limit",
        "yes_price": price_cents if side == "yes" else 100 - price_cents,
        "no_price": 100 - price_cents if side == "yes" else price_cents,
        "count": count,
        "expiration_ts": int(time.time()) + 300,
    })
    try:
        r = await client.post(f"{KALSHI_API_URL}{path}",
                              headers=_auth_headers("POST", path, body),
                              content=body, timeout=10)
        return r.json() if r.status_code in (200, 201) else None
    except Exception:
        return None

# ── ARB DETECTION ─────────────────────────────────────────────────────────────
@dataclass
class MarketInfo:
    ticker: str
    title: str
    threshold: float    # extracted numeric threshold
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    close_time: str

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def no_mid(self) -> float:
        return (self.no_bid + self.no_ask) / 2

def extract_threshold(title: str) -> Optional[float]:
    """Extract numeric threshold from market title."""
    # Match patterns like: "> 500", "above 500", ">= 5.25%", "over 500"
    patterns = [
        r'(?:above|over|>|>=|at least)\s*\$?([\d,]+\.?\d*)\s*%?',
        r'\$([\d,]+\.?\d*)',
        r'([\d,]+\.?\d*)\s*(?:or above|or higher|\+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, title, re.IGNORECASE)
        if m:
            val_str = m.group(1).replace(",", "")
            try:
                return float(val_str)
            except ValueError:
                pass
    return None

def group_by_threshold(markets: list) -> list[MarketInfo]:
    """Convert markets to MarketInfo, filtering to threshold-type markets."""
    result = []
    for m in markets:
        title = m.get("title", "")
        threshold = extract_threshold(title)
        if threshold is None:
            continue
        result.append(MarketInfo(
            ticker=m.get("ticker", ""),
            title=title,
            threshold=threshold,
            yes_bid=m.get("yes_bid", 0),
            yes_ask=m.get("yes_ask", 100),
            no_bid=100 - m.get("yes_ask", 100),
            no_ask=100 - m.get("yes_bid", 0),
            close_time=m.get("close_time", ""),
        ))
    return sorted(result, key=lambda x: x.threshold)

def find_threshold_violations(markets: list[MarketInfo]) -> list[dict]:
    """
    For "above X" markets: P(above X) must be monotonically decreasing in X.
    If P(above 520) > P(above 500), that's impossible → arbitrage.

    Trade: sell the overpriced higher threshold (buy NO), buy the underpriced lower.
    """
    arbs = []
    # Group by same close_time (same event)
    by_date = {}
    for m in markets:
        key = m.close_time[:10]
        by_date.setdefault(key, []).append(m)

    for date, group in by_date.items():
        group = sorted(group, key=lambda x: x.threshold)
        if len(group) < 2:
            continue

        for i in range(len(group) - 1):
            lower = group[i]   # lower threshold → should have HIGHER probability
            higher = group[i + 1]  # higher threshold → should have LOWER probability

            # Violation: higher threshold has higher YES price than lower threshold
            # lower.yes_mid should be >= higher.yes_mid
            violation = lower.yes_mid - higher.yes_mid

            if violation < -MIN_ARB_CENTS:
                # lower is underpriced, higher is overpriced
                # Buy YES on lower (should be ≥ higher)
                # Buy NO on higher (it can't be higher than lower)
                profit_potential = abs(violation)
                log.info(f"ARB FOUND: {lower.ticker}({lower.threshold}) mid={lower.yes_mid:.0f}¢ "
                         f"< {higher.ticker}({higher.threshold}) mid={higher.yes_mid:.0f}¢ "
                         f"(impossible) gap={abs(violation):.1f}¢")
                arbs.append({
                    "type": "threshold_violation",
                    "leg1_ticker": lower.ticker,
                    "leg1_side": "yes",
                    "leg1_price": lower.yes_ask,
                    "leg2_ticker": higher.ticker,
                    "leg2_side": "no",
                    "leg2_price": 100 - higher.yes_bid,
                    "profit_potential": profit_potential,
                    "reason": (f"Threshold arb: {lower.ticker}(≥{lower.threshold}) "
                               f"priced BELOW {higher.ticker}(≥{higher.threshold}) — impossible"),
                })

    return sorted(arbs, key=lambda x: x["profit_potential"], reverse=True)

def find_yes_no_violations(markets: list) -> list[dict]:
    """
    YES + NO for same market should sum to ~100¢.
    If YES ask + NO ask < 92¢, there's a spread arbitrage:
    buy both YES and NO, guaranteed to collect ~8¢+ profit.
    """
    arbs = []
    for m in markets:
        yes_ask = m.get("yes_ask", 100)
        no_ask = 100 - m.get("yes_bid", 0)  # NO ask = 100 - YES bid
        total_cost = yes_ask + no_ask
        if total_cost < (100 - MIN_ARB_CENTS):
            profit = 100 - total_cost
            arbs.append({
                "type": "yes_no_spread",
                "ticker": m.get("ticker", ""),
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "profit": profit,
                "reason": f"YES+NO cost={total_cost}¢, profit={profit}¢ at settlement",
            })
    return sorted(arbs, key=lambda x: x["profit"], reverse=True)

async def scan_series(client: httpx.AsyncClient, series: str) -> tuple[list, list]:
    """Scan a series for all arb types."""
    markets = await get_series_markets(client, series)
    if not markets:
        return [], []

    for m in markets:
        _normalize_market(m)

    threshold_markets = group_by_threshold(markets)
    threshold_arbs = find_threshold_violations(threshold_markets)
    yes_no_arbs = find_yes_no_violations(markets)

    return threshold_arbs, yes_no_arbs

# ── MAIN ──────────────────────────────────────────────────────────────────────
# ── Stats HTTP server ─────────────────────────────────────────────────────────
_stats_app = Flask(__name__)
_bot_stats = {"trades": 0, "wins": 0, "pnl": 0.0, "balance": 0.0, "start": time.time()}

@_stats_app.route("/stats")
def _stats_endpoint():
    t = _bot_stats
    total = t["trades"]
    return jsonify({"bot": "kalshi-correlation-arb-bot", "paper_mode": True,
        "balance": t["balance"], "trades": total, "wins": t["wins"],
        "losses": total - t["wins"], "win_rate": round(t["wins"]/max(total,1), 4),
        "pnl": t["pnl"], "uptime_hours": round((time.time()-t["start"])/3600, 2)})

@_stats_app.route("/health")
def _health_endpoint():
    return jsonify({"status": "ok"})

def _run_stats_server():
    _stats_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


async def main():
    threading.Thread(target=_run_stats_server, daemon=True).start()
    risk_manager = RiskManager(starting_balance=5000.0)
    mode = "PAPER" if PAPER_MODE else "LIVE"
    log.info(f"=== Correlation Arb Bot starting [{mode} MODE] ===")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                all_threshold_arbs = []
                all_yes_no_arbs = []

                for series in THRESHOLD_SERIES:
                    t_arbs, yn_arbs = await scan_series(client, series)
                    all_threshold_arbs.extend(t_arbs)
                    all_yes_no_arbs.extend(yn_arbs)
                    await asyncio.sleep(0.5)

                log.info(f"Found {len(all_threshold_arbs)} threshold arbs, "
                         f"{len(all_yes_no_arbs)} YES/NO spread arbs")

                # Execute threshold arbs (2-leg trades)
                for arb in all_threshold_arbs[:2]:
                    log.info(f"THRESHOLD ARB: {arb['reason']}")
                    log.info(f"  Leg1: BUY {arb['leg1_side'].upper()} {arb['leg1_ticker']} @ {arb['leg1_price']}¢")
                    log.info(f"  Leg2: BUY {arb['leg2_side'].upper()} {arb['leg2_ticker']} @ {arb['leg2_price']}¢")
                    log.info(f"  Potential: {arb['profit_potential']:.1f}¢/contract")

                    if arb["leg1_price"] > MAX_PRICE or arb["leg2_price"] > MAX_PRICE:
                        log.info("  Skipped — price too high")
                        continue

                    # Kelly: arb profit is near-certain, size by profit potential vs balance
                    profit_pct = arb["profit_potential"] / max(arb["leg1_price"], arb["leg2_price"])
                    kelly_bet_cents = max(1, min(int(5000 * 100 * profit_pct * KELLY_FRACTION), BET_SIZE_CENTS * 5))
                    contracts = max(1, kelly_bet_cents // max(arb["leg1_price"], arb["leg2_price"]))

                    # Risk guard check (check leg1)
                    if not PAPER_MODE:
                        allowed, rg_reason, capped = risk_manager.pre_trade_check(
                            arb["leg1_ticker"], arb["leg1_price"], contracts,
                            arb["leg1_side"], bot_name="correlation-arb-bot")
                        if not allowed:
                            log.warning(f"Risk guard blocked: {rg_reason}")
                            continue
                        contracts = capped or contracts
                    else:
                        allowed, rg_reason, capped = risk_manager.pre_trade_check(
                            arb["leg1_ticker"], arb["leg1_price"], contracts,
                            arb["leg1_side"], bot_name="correlation-arb-bot")
                        if not allowed:
                            log.info(f"[PAPER] Risk guard would block: {rg_reason}")

                    if not PAPER_MODE:
                        r1 = await place_order(client, arb["leg1_ticker"], arb["leg1_side"],
                                               arb["leg1_price"], contracts)
                        await asyncio.sleep(0.3)
                        r2 = await place_order(client, arb["leg2_ticker"], arb["leg2_side"],
                                               arb["leg2_price"], contracts)
                        log.info(f"  Orders: {bool(r1)}, {bool(r2)}")
                    else:
                        log.info(f"  [PAPER] Would place 2-leg arb, {contracts} contracts each")

                # Execute YES/NO spread arbs (single market, both sides)
                for arb in all_yes_no_arbs[:2]:
                    log.info(f"YES/NO SPREAD ARB: {arb['ticker']} profit={arb['profit']}¢")
                    log.info(f"  Buy YES @ {arb['yes_ask']}¢, Buy NO @ {arb['no_ask']}¢")

                    if arb["yes_ask"] > MAX_PRICE or arb["no_ask"] > MAX_PRICE:
                        continue

                    # Kelly: arb profit is near-certain, size by profit vs balance
                    profit_pct = arb["profit"] / (arb["yes_ask"] + arb["no_ask"])
                    kelly_bet_cents = max(1, min(int(5000 * 100 * profit_pct * KELLY_FRACTION), BET_SIZE_CENTS * 5))
                    contracts = max(1, kelly_bet_cents // max(arb["yes_ask"], arb["no_ask"]))

                    # Risk guard check
                    if not PAPER_MODE:
                        allowed, rg_reason, capped = risk_manager.pre_trade_check(
                            arb["ticker"], arb["yes_ask"], contracts, "yes",
                            bot_name="correlation-arb-bot")
                        if not allowed:
                            log.warning(f"Risk guard blocked: {rg_reason}")
                            continue
                        contracts = capped or contracts
                    else:
                        allowed, rg_reason, capped = risk_manager.pre_trade_check(
                            arb["ticker"], arb["yes_ask"], contracts, "yes",
                            bot_name="correlation-arb-bot")
                        if not allowed:
                            log.info(f"[PAPER] Risk guard would block: {rg_reason}")

                    if not PAPER_MODE:
                        r1 = await place_order(client, arb["ticker"], "yes",
                                               arb["yes_ask"], contracts)
                        await asyncio.sleep(0.3)
                        r2 = await place_order(client, arb["ticker"], "no",
                                               arb["no_ask"], contracts)
                        log.info(f"  Orders: {bool(r1)}, {bool(r2)}")
                    else:
                        log.info(f"  [PAPER] Would place YES+NO arb, {contracts} contracts")

            except Exception as e:
                log.error(f"Bot error: {e}", exc_info=True)

            log.info(f"Sleeping {POLL_INTERVAL_SEC}s...")
            await asyncio.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    asyncio.run(main())
