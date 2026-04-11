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

# ── Quant Fund Shadow Evaluators ─────────────────────────────────────────
try:
    from bayesian_updater import BayesianUpdater
    from ensemble_model import EnsembleModel
    from time_decay_edge import calculate_time_weighted_edge
    from correlation_matrix import CorrelationTracker
    from vpin_toxicity import VPINTracker
    from market_impact import estimate_market_impact
    from feature_engine import FeatureEngine
    from portfolio_optimizer import PortfolioOptimizer
    _quant_modules_available = True
    _bayesian = BayesianUpdater()
    _ensemble = EnsembleModel()
    _correlation = CorrelationTracker()
    _vpin = VPINTracker()
    _features = FeatureEngine()
    _portfolio = PortfolioOptimizer()
except ImportError:
    _quant_modules_available = False

# ── Critical Module Imports (10 modules) ───────────────────────────────────
# Each module is optional: bot keeps running if any module is missing or errors.

try:
    from pre_trade_validator import validate_pre_trade
    _pre_trade_validator_available = True
except ImportError:
    _pre_trade_validator_available = False

try:
    from dynamic_edge import calculate_dynamic_edge
    _dynamic_edge_available = True
except ImportError:
    _dynamic_edge_available = False

try:
    from adaptive_kelly import calculate_adaptive_kelly
    _adaptive_kelly_available = True
except ImportError:
    _adaptive_kelly_available = False

try:
    from dynamic_params import DynamicParams
    _dynamic_params = DynamicParams()
    _dynamic_params_available = True
except ImportError:
    _dynamic_params_available = False

try:
    from paper_balance_manager import PaperBalanceManager
    _paper_balance_mgr = PaperBalanceManager(restart_threshold=1000.0)
    _paper_balance_available = True
except ImportError:
    _paper_balance_available = False

try:
    from maker_execution import MakerExecution
    _maker_execution_available = True
except ImportError:
    _maker_execution_available = False

try:
    from data_pipeline import DataPipeline
    _data_pipeline = DataPipeline()
    _data_pipeline_available = True
except ImportError:
    _data_pipeline_available = False

try:
    from brier_scorer import BrierScorer
    _brier_scorer = BrierScorer()
    _brier_scorer_available = True
except ImportError:
    _brier_scorer_available = False

try:
    from rejection_filter import RejectionFilter
    _rejection_filter = RejectionFilter()
    _rejection_filter_available = True
except ImportError:
    _rejection_filter_available = False

try:
    from conviction_scaler import ConvictionScaler
    _conviction_scaler = ConvictionScaler()
    _conviction_scaler_available = True
except ImportError:
    _conviction_scaler_available = False


# ── Shadow Logging ────────────────────────────────────────────────────────────
SHADOW_LOG_FILE = os.getenv("SHADOW_LOG_FILE", "shadow_log.jsonl")

def shadow_log(opportunity: dict, taken: bool, reason: str = ""):
    entry = {"ts": time.time(), "taken": taken, "reason": reason, **opportunity}
    try:
        with open(SHADOW_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except:
        pass


# ── Virtual Portfolio Testing ─────────────────────────────────────────────
VIRTUAL_PORTFOLIO_FILE = os.getenv("VIRTUAL_PORTFOLIO_FILE", "virtual_portfolios.jsonl")

VIRTUAL_PORTFOLIOS = [
    {"name": "aggressive", "kelly": 1.0, "min_edge": 0.01, "early_exit": 0.99},
    {"name": "moderate", "kelly": 0.75, "min_edge": 0.02, "early_exit": 0.93},
    {"name": "conservative", "kelly": 0.5, "min_edge": 0.03, "early_exit": 0.90},
    {"name": "original", "kelly": 1.0, "min_edge": 0.02, "early_exit": 0.99},
]

def evaluate_virtual_portfolios(opportunity: dict):
    """Evaluate what each virtual portfolio would do with this opportunity."""
    import json, time as _time
    edge = opportunity.get("edge", 0)
    price = opportunity.get("price", 0)
    results = []
    for vp in VIRTUAL_PORTFOLIOS:
        would_trade = edge >= vp["min_edge"]
        would_exit_early = price >= vp["early_exit"] * 100
        results.append({
            "portfolio": vp["name"],
            "would_trade": would_trade,
            "would_exit_early": would_exit_early,
            "kelly": vp["kelly"],
            "min_edge": vp["min_edge"],
        })
    entry = {
        "ts": _time.time(),
        "opportunity": opportunity,
        "portfolios": results,
    }
    try:
        with open(VIRTUAL_PORTFOLIO_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except:
        pass

# ── Multi-strike: scan ALL strikes per event/series, not just one ────────────

# ─── Regime Detection — pause trading during extreme volatility ────────────
import statistics as _stats

REGIME_WINDOW = int(os.getenv("REGIME_WINDOW", "20"))
REGIME_THRESHOLD = float(os.getenv("REGIME_THRESHOLD", "3.0"))
_regime_prices: list[float] = []

def check_regime(price: float) -> str:
    """Returns 'CALM', 'ELEVATED', or 'CRASH'. Skip trades during CRASH."""
    _regime_prices.append(price)
    if len(_regime_prices) > REGIME_WINDOW:
        _regime_prices.pop(0)
    if len(_regime_prices) < 5:
        return "CALM"
    rets = [(b - a) / a for a, b in zip(_regime_prices[:-1], _regime_prices[1:])]
    if not rets:
        return "CALM"
    mu = _stats.mean(rets)
    sd = _stats.stdev(rets) if len(rets) > 1 else 0.01
    z = abs(rets[-1] - mu) / max(sd, 0.0001)
    if z > REGIME_THRESHOLD:
        return "CRASH"
    elif z > REGIME_THRESHOLD * 0.6:
        return "ELEVATED"
    return "CALM"



# ── Early Exit Logic ─────────────────────────────────────────────────────────
EARLY_EXIT_THRESHOLD = float(os.getenv("EARLY_EXIT_THRESHOLD", "0.99"))

def should_early_exit(current_price_cents: float) -> bool:
    """Exit position early at 93c+ to lock in profit instead of holding to settlement."""
    return current_price_cents >= EARLY_EXIT_THRESHOLD * 100

# ── Circuit Breakers ─────────────────────────────────────────────────────────
CONSECUTIVE_LOSS_PAUSE = int(os.getenv("CONSECUTIVE_LOSS_PAUSE", "5"))
DAILY_DRAWDOWN_PAUSE_PCT = float(os.getenv("DAILY_DRAWDOWN_PAUSE_PCT", "0.05"))

_consecutive_losses = 0
_daily_pnl = 0.0
_circuit_paused_until = 0

def check_circuit_breaker() -> bool:
    """Returns True if trading should be paused."""
    import time as _time
    global _consecutive_losses, _daily_pnl, _circuit_paused_until
    if _time.time() < _circuit_paused_until:
        return True
    if _consecutive_losses >= CONSECUTIVE_LOSS_PAUSE:
        return True
    # Use PAPER_BALANCE if available, else 5000
    _balance = globals().get("PAPER_BALANCE", 2000)
    if _daily_pnl < -DAILY_DRAWDOWN_PAUSE_PCT * _balance:
        return True
    return False


# ── BRIER SCORER + DATA PIPELINE: post-resolution (10-module integration) ──
try:
    if _brier_scorer_available:
        _brier_scorer.record(predicted_prob=locals().get("predicted_prob", locals().get("entry_price", 50)) / 100.0 if locals().get("predicted_prob", locals().get("entry_price", 50)) > 1 else locals().get("predicted_prob", 0.5), actual_outcome=1.0 if locals().get("won", locals().get("pnl", 0) > 0) else 0.0, asset=locals().get("asset", "correlation-arb"))
except Exception:
    pass
try:
    if _data_pipeline_available:
        _data_pipeline.record_snapshot({"bot": "correlation-arb", "event": "resolution", "pnl": locals().get("pnl", 0), "ts": time.time()})
except Exception:
    pass

def record_trade_result(won: bool, pnl: float):
    """Update circuit breaker state after each trade result."""
    global _consecutive_losses, _daily_pnl
    _daily_pnl += pnl
    if won:
        _consecutive_losses = 0
    else:
        _consecutive_losses += 1
MULTI_STRIKE = os.getenv("MULTI_STRIKE", "true").lower() == "true"
# When fetching markets, iterate through ALL contracts in each series/event
# and evaluate each strike independently. No single-ticker filtering.

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
KELLY_FRACTION    = float(os.getenv("KELLY_FRACTION", "0.75"))
MIN_ARB_CENTS     = int(os.getenv("MIN_ARB_CENTS", "8"))          # min 8¢ mispricing to trade
MAKER_FEE         = float(os.getenv("MAKER_FEE", "0.0175"))
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

# ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
try:
    if _pre_trade_validator_available:
        _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "correlation-arb"})
        if _ptv and _ptv.get("halt"):
            log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
except Exception:
    pass
try:
    if _rejection_filter_available:
        _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
        if _rej and _rej.get("reject"):
            log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
except Exception:
    pass
_min_edge_dynamic = 0.0
try:
    if _dynamic_edge_available:
        _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
except Exception:
    pass
_kelly_frac = 1.0
try:
    if _adaptive_kelly_available:
        _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
except Exception:
    pass
try:
    if _conviction_scaler_available:
        _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
        _kelly_frac *= _conv_mult
except Exception:
    pass

# ── MAKER EXECUTION: use maker orders when available (10-module integration) ──
try:
    if _maker_execution_available and not globals().get('PAPER_MODE', True):
        _maker = MakerExecution(locals().get("client", locals().get("kalshi", None)))
        if _maker:
            log.info("[MAKER_EXECUTION] Maker execution module available for live orders")
except Exception:
    pass

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

            # ── CYCLE START: Dynamic Params + Paper Balance (10-module integration) ──
            try:
                if _dynamic_params_available:
                    _cycle_params = _dynamic_params.get_all()
                    if "bet_size" in _cycle_params:
                        pass  # Override config if needed
            except Exception as _e:
                pass
            try:
                if _paper_balance_available:
                    _pbm_info = _paper_balance_mgr.check_and_restart(globals().get('paper_balance', 2000))
                    if _pbm_info and _pbm_info.get("restarted"):
                        log.info(f"[PAPER_BALANCE] Auto-restarted. Lifetime P&L: ${_pbm_info.get('lifetime_pnl', 0):.2f}")
            except Exception as _e:
                pass

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
                        shadow_log({"bot": "correlation_arb", "leg1": arb["leg1_ticker"], "leg2": arb["leg2_ticker"], "profit": arb["profit_potential"]}, taken=False, reason="price too high")
                        evaluate_virtual_portfolios({"bot": "correlation_arb", "leg1": arb["leg1_ticker"], "leg2": arb["leg2_ticker"], "profit": arb["profit_potential"]})
                        if _quant_modules_available:
                            try:
                                _features.extract({"price": locals().get("price", 0), "volume": locals().get("volume", 0), "bid": locals().get("bid", 0), "ask": locals().get("ask", 0)})
                                _bayesian.update(locals().get("market_id", locals().get("ticker", "unknown")), locals().get("price", 0), time.time())
                                _td_edge = calculate_time_weighted_edge(locals().get("edge", 0), locals().get("minutes_remaining", locals().get("time_remaining", 15)), 15)
                                _vpin.update(locals().get("price", 0), locals().get("volume", 0))
                                _mi = estimate_market_impact(locals().get("contracts", 1), locals().get("volume", 100))
                            except:
                                pass
                        continue

                    # Fee-aware EV gate — skip negative-EV trades after fees
                    PLATFORM_FEE = float(os.getenv("PLATFORM_FEE", "0.0175"))  # Kalshi 1.75% maker
                    fee_cost = PLATFORM_FEE * 100 * 2  # 2 legs, fee in cents
                    ev_after_fees = arb["profit_potential"] - fee_cost
                    if ev_after_fees <= 0:
                        log.info(f"  Skipped — profit {arb['profit_potential']:.1f}¢ <= fees {fee_cost:.1f}¢")
                        shadow_log({"bot": "correlation_arb", "leg1": arb["leg1_ticker"], "leg2": arb["leg2_ticker"], "profit": arb["profit_potential"], "fees": fee_cost, "ev_after_fees": ev_after_fees}, taken=False, reason=f"negative EV after {PLATFORM_FEE*100}% fee")
                        evaluate_virtual_portfolios({"bot": "correlation_arb", "leg1": arb["leg1_ticker"], "leg2": arb["leg2_ticker"], "profit": arb["profit_potential"], "fees": fee_cost, "ev_after_fees": ev_after_fees})
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

                    # ── Regime detection ──
                    regime = check_regime(float(price))
                    if regime == "CRASH":
                        log.warning("REGIME CRASH on kalshi_correlation_arb_bot — skipping trade")
                        shadow_log({"bot": "kalshi_correlation_arb_bot", "regime": regime}, taken=False, reason="crash regime")
                        evaluate_virtual_portfolios({"bot": "kalshi_correlation_arb_bot", "regime": regime})
                        continue
                    shadow_log({"bot": "correlation_arb", "leg1": arb["leg1_ticker"], "leg2": arb["leg2_ticker"], "profit": arb["profit_potential"], "contracts": contracts}, taken=True)
                    evaluate_virtual_portfolios({"bot": "correlation_arb", "leg1": arb["leg1_ticker"], "leg2": arb["leg2_ticker"], "profit": arb["profit_potential"], "contracts": contracts})
                    if not PAPER_MODE:
                        # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
                        try:
                            if _pre_trade_validator_available:
                                _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "correlation-arb"})
                                if _ptv and _ptv.get("halt"):
                                    log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        try:
                            if _rejection_filter_available:
                                _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
                                if _rej and _rej.get("reject"):
                                    log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        _min_edge_dynamic = 0.0
                        try:
                            if _dynamic_edge_available:
                                _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
                        except Exception:
                            pass
                        _kelly_frac = 1.0
                        try:
                            if _adaptive_kelly_available:
                                _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
                        except Exception:
                            pass
                        try:
                            if _conviction_scaler_available:
                                _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
                                _kelly_frac *= _conv_mult
                        except Exception:
                            pass

                        r1 = await place_order(client, arb["leg1_ticker"], arb["leg1_side"],
                                               arb["leg1_price"], contracts)
                        await asyncio.sleep(0.3)
                        # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
                        try:
                            if _pre_trade_validator_available:
                                _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "correlation-arb"})
                                if _ptv and _ptv.get("halt"):
                                    log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        try:
                            if _rejection_filter_available:
                                _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
                                if _rej and _rej.get("reject"):
                                    log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        _min_edge_dynamic = 0.0
                        try:
                            if _dynamic_edge_available:
                                _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
                        except Exception:
                            pass
                        _kelly_frac = 1.0
                        try:
                            if _adaptive_kelly_available:
                                _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
                        except Exception:
                            pass
                        try:
                            if _conviction_scaler_available:
                                _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
                                _kelly_frac *= _conv_mult
                        except Exception:
                            pass

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

                    # Fee-aware check: profit must cover maker fees on both sides
                    fee_cost = MAKER_FEE * 100 * 2  # 2 sides, fee in cents
                    if arb["profit"] <= fee_cost:
                        log.info(f"  Skipped — profit {arb['profit']}¢ <= fees {fee_cost:.1f}¢")
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
                        # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
                        try:
                            if _pre_trade_validator_available:
                                _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "correlation-arb"})
                                if _ptv and _ptv.get("halt"):
                                    log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        try:
                            if _rejection_filter_available:
                                _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
                                if _rej and _rej.get("reject"):
                                    log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        _min_edge_dynamic = 0.0
                        try:
                            if _dynamic_edge_available:
                                _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
                        except Exception:
                            pass
                        _kelly_frac = 1.0
                        try:
                            if _adaptive_kelly_available:
                                _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
                        except Exception:
                            pass
                        try:
                            if _conviction_scaler_available:
                                _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
                                _kelly_frac *= _conv_mult
                        except Exception:
                            pass

                        r1 = await place_order(client, arb["ticker"], "yes",
                                               arb["yes_ask"], contracts)
                        await asyncio.sleep(0.3)
                        # ── PRE-TRADE + REJECTION + DYNAMIC EDGE + SIZING (10-module integration) ──
                        try:
                            if _pre_trade_validator_available:
                                _ptv = validate_pre_trade({"ticker": locals().get("ticker", ""), "side": locals().get("side", ""), "bot": "correlation-arb"})
                                if _ptv and _ptv.get("halt"):
                                    log.info(f"[PRE_TRADE_VALIDATOR] Halted: {_ptv.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        try:
                            if _rejection_filter_available:
                                _rej = _rejection_filter.check(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), price_cents=locals().get("price_cents", locals().get("price", 50)))
                                if _rej and _rej.get("reject"):
                                    log.info(f"[REJECTION_FILTER] Rejected: {_rej.get('reason', 'unknown')}")
                        except Exception:
                            pass
                        _min_edge_dynamic = 0.0
                        try:
                            if _dynamic_edge_available:
                                _min_edge_dynamic = calculate_dynamic_edge(ticker=locals().get("ticker", ""), volume=locals().get("volume", 0), move_pct=locals().get("move_pct", locals().get("edge", 0)), time_remaining=locals().get("time_remaining", None))
                        except Exception:
                            pass
                        _kelly_frac = 1.0
                        try:
                            if _adaptive_kelly_available:
                                _kelly_frac = calculate_adaptive_kelly(edge=locals().get("edge", locals().get("ev_rate", 0.05)), price_cents=locals().get("price_cents", locals().get("price", 50)), volume=locals().get("volume", 0), win_rate=0.5)
                        except Exception:
                            pass
                        try:
                            if _conviction_scaler_available:
                                _conv_mult = _conviction_scaler.scale(move_pct=locals().get("move_pct", locals().get("edge", 0)), volume=locals().get("volume", 0), ev_after_fees=locals().get("ev_rate", locals().get("edge", 0.05)), direction=locals().get("direction", locals().get("side", "yes")))
                                _kelly_frac *= _conv_mult
                        except Exception:
                            pass

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
