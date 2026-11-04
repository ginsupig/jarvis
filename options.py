"""JARVIS studying the options market: what options price in, and how often it was right.

    read      option chains with greeks: Tradier's (real time, with open
              interest) when its token is present, else Alpaca's indicative
              feed (delayed, no open interest). Both clients know GET
              endpoints for quotes and chains and nothing else
    measure   for one underlying: at-the-money implied volatility across the
              term structure (front, ~30 and ~60 days), the expected move the
              straddle prices to each expiration, 25-delta skew, put/call
              ratios by volume and open interest, the strikes with the most
              open interest either side of the price, and max pain
    record    after every close, one row per followed symbol (`option_days`)
              and the front weekly expected move (`option_moves`)
    learn     every recorded expected move is scored once its expiration has
              closed (inside or outside), and every 30-day implied volatility
              is compared with the volatility that actually followed over the
              next 21 sessions. Those two records are what JARVIS says when
              asked whether options are rich or cheap, and they only grow

Everything is arithmetic on quotes. Implied volatility is an input, never a
forecast JARVIS endorses; the forward record is what says how far to trust it.
"""
from __future__ import annotations

import logging
import math
import os
import re
import statistics
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

import market

log = logging.getLogger("jarvis.options")

TRADIER_URL = "https://api.tradier.com/v1"
ALPACA_DATA_URL = "https://data.alpaca.markets"
CORE = ("SPY", "QQQ", "IWM")
RECORD_MAX = 25                     # underlyings recorded per close
IV_HISTORY_MIN = 20                 # days before an IV percentile is quoted
RV_FORWARD = 21                     # sessions an implied vol is scored over
OCC_RE = re.compile(r"([A-Z]{1,5})(\d{6})([CP])(\d{8})")


def enabled() -> bool:
    return os.getenv("JARVIS_OPTIONS", "1").strip().lower() not in ("0", "false", "no", "off")


class OptionsDataError(RuntimeError):
    pass


# ── chain sources ───────────────────────────────────────────────────────────
# A contract is a dict: symbol, type ('call'/'put'), strike, expiration,
# bid, ask, mid, volume, oi (None when unknown), delta, gamma, theta, vega, iv.

def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _mid(bid, ask) -> Optional[float]:
    if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
        return None
    return (bid + ask) / 2


class TradierChains:
    """Read-only: quotes, expirations and chains. No account, no order."""
    name, realtime = "Tradier", True

    def __init__(self, token: str, client: httpx.Client | None = None):
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._client = client or httpx.Client(timeout=30)

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self._client.get(TRADIER_URL + path, params=params, headers=self._headers)
        except httpx.HTTPError as e:
            raise OptionsDataError(f"could not reach Tradier: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise OptionsDataError("Tradier refused the token")
        if r.status_code >= 400:
            raise OptionsDataError(f"Tradier answered {r.status_code}")
        return r.json() or {}

    def quote(self, symbol: str) -> Optional[float]:
        q = (self._get("/markets/quotes", {"symbols": symbol}).get("quotes") or {}).get("quote")
        q = q[0] if isinstance(q, list) and q else q
        return _num((q or {}).get("last")) if isinstance(q, dict) else None

    def expirations(self, symbol: str) -> list[str]:
        got = (self._get("/markets/options/expirations", {"symbol": symbol}).get("expirations") or {})
        dates = got.get("date") if isinstance(got, dict) else None
        dates = [dates] if isinstance(dates, str) else dates or []
        return sorted(d for d in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)))

    def chain(self, symbol: str, expiration: str) -> list[dict]:
        got = (self._get("/markets/options/chains",
                         {"symbol": symbol, "expiration": expiration, "greeks": "true"})
               .get("options") or {})
        rows = got.get("option") if isinstance(got, dict) else None
        rows = [rows] if isinstance(rows, dict) else rows or []
        out = []
        for o in rows:
            g = o.get("greeks") or {}
            bid, ask = _num(o.get("bid")), _num(o.get("ask"))
            strike = _num(o.get("strike"))
            kind = o.get("option_type")
            if strike is None or kind not in ("call", "put"):
                continue
            out.append({"symbol": str(o.get("symbol", "")), "type": kind, "strike": strike,
                        "expiration": expiration, "bid": bid, "ask": ask, "mid": _mid(bid, ask),
                        "volume": _num(o.get("volume")) or 0.0, "oi": _num(o.get("open_interest")),
                        "delta": _num(g.get("delta")), "gamma": _num(g.get("gamma")),
                        "theta": _num(g.get("theta")), "vega": _num(g.get("vega")),
                        "iv": _num(g.get("mid_iv")) or _num(g.get("smv_vol"))})
        return out


class AlpacaChains:
    """Alpaca's indicative option feed: delayed quotes, greeks, no open interest."""
    name, realtime = "Alpaca (indicative, delayed)", False

    def __init__(self, key_id: str, secret: str, client: httpx.Client | None = None):
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self._client = client or httpx.Client(timeout=30)
        self._cache: dict[str, list[dict]] = {}

    def _get(self, url: str, params: dict) -> dict:
        try:
            r = self._client.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as e:
            raise OptionsDataError(f"could not reach Alpaca: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise OptionsDataError("Alpaca refused the API keys")
        if r.status_code >= 400:
            raise OptionsDataError(f"Alpaca answered {r.status_code}")
        return r.json() or {}

    def quote(self, symbol: str) -> Optional[float]:
        body = self._get(f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades/latest", {"feed": "iex"})
        return _num((body.get("trade") or {}).get("p"))

    def _all(self, symbol: str) -> list[dict]:
        if symbol in self._cache:
            return self._cache[symbol]
        today = datetime.now(timezone.utc).date()
        params = {"feed": "indicative", "limit": 1000,
                  "expiration_date_gte": today.isoformat(),
                  "expiration_date_lte": (today + timedelta(days=75)).isoformat()}
        spot = self.quote(symbol)
        if spot:
            params.update(strike_price_gte=round(spot * 0.8, 2), strike_price_lte=round(spot * 1.2, 2))
        out = []
        for _ in range(20):
            body = self._get(f"{ALPACA_DATA_URL}/v1beta1/options/snapshots/{symbol}", params)
            for occ, v in (body.get("snapshots") or {}).items():
                m = OCC_RE.fullmatch(occ)
                if not m:
                    continue
                q, g = v.get("latestQuote") or {}, v.get("greeks") or {}
                bid, ask = _num(q.get("bp")), _num(q.get("ap"))
                ymd = m.group(2)
                out.append({"symbol": occ, "type": "call" if m.group(3) == "C" else "put",
                            "strike": int(m.group(4)) / 1000,
                            "expiration": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}",
                            "bid": bid, "ask": ask, "mid": _mid(bid, ask),
                            "volume": _num((v.get("dailyBar") or {}).get("v")) or 0.0, "oi": None,
                            "delta": _num(g.get("delta")), "gamma": _num(g.get("gamma")),
                            "theta": _num(g.get("theta")), "vega": _num(g.get("vega")),
                            "iv": _num(v.get("impliedVolatility"))})
            token = body.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
        self._cache[symbol] = out
        return out

    def expirations(self, symbol: str) -> list[str]:
        return sorted({c["expiration"] for c in self._all(symbol)})

    def chain(self, symbol: str, expiration: str) -> list[dict]:
        return [c for c in self._all(symbol) if c["expiration"] == expiration]


def source(client: httpx.Client | None = None):
    """Tradier when its token is present, else Alpaca's indicative feed, else None."""
    import accounts
    token, _ = accounts.tradier_keys()
    if token:
        return TradierChains(token, client)
    key_id, secret = market.keys()
    if key_id and secret:
        return AlpacaChains(key_id, secret, client)
    return None


# ── measuring one underlying ────────────────────────────────────────────────

def _dte(expiration: str, today: date) -> int:
    return (date.fromisoformat(expiration) - today).days


def pick_expirations(expirations: list[str], today: date) -> dict[str, str]:
    """front (first with a day left), weekly (4–10 days, for the expected move),
    m30 and m60 (closest to 30 and 60 days)."""
    live = [e for e in expirations if _dte(e, today) >= 1]
    if not live:
        return {}
    out = {"front": live[0]}
    weekly = [e for e in live if 4 <= _dte(e, today) <= 10]
    if weekly:
        out["weekly"] = weekly[0]
    for key, target in (("m30", 30), ("m60", 60)):
        best = min(live, key=lambda e: abs(_dte(e, today) - target))
        if abs(_dte(best, today) - target) <= target / 2:
            out[key] = best
    return out


def atm(chain: list[dict], spot: float) -> Optional[dict]:
    """The strike nearest the price that has both a call and a put quoted."""
    by_strike: dict[float, dict] = {}
    for c in chain:
        if c["mid"] is not None:
            by_strike.setdefault(c["strike"], {})[c["type"]] = c
    pairs = [(k, v) for k, v in by_strike.items() if "call" in v and "put" in v]
    if not pairs:
        return None
    strike, pair = min(pairs, key=lambda kv: abs(kv[0] - spot))
    ivs = [pair[t]["iv"] for t in ("call", "put") if pair[t]["iv"]]
    straddle = pair["call"]["mid"] + pair["put"]["mid"]
    return {"strike": strike, "iv": sum(ivs) / len(ivs) if ivs else None,
            "straddle": straddle, "move_pct": straddle / spot if spot else None}


def _nearest_delta(chain: list[dict], kind: str, target: float) -> Optional[dict]:
    got = [c for c in chain if c["type"] == kind and c["delta"] is not None and c["iv"]]
    return min(got, key=lambda c: abs(c["delta"] - target)) if got else None


def max_pain(chain: list[dict]) -> Optional[float]:
    with_oi = [c for c in chain if c["oi"]]
    if not with_oi:
        return None
    strikes = sorted({c["strike"] for c in with_oi})

    def pain(px: float) -> float:
        return sum(c["oi"] * (max(0.0, px - c["strike"]) if c["type"] == "call"
                              else max(0.0, c["strike"] - px)) for c in with_oi)
    return min(strikes, key=pain)


def walls(chains: list[list[dict]], spot: float, n: int = 2) -> dict:
    """The strikes holding the most open interest: calls above, puts below."""
    oi: dict[tuple[str, float], float] = {}
    for chain in chains:
        for c in chain:
            if c["oi"]:
                oi[(c["type"], c["strike"])] = oi.get((c["type"], c["strike"]), 0.0) + c["oi"]
    calls = sorted(((k[1], v) for k, v in oi.items() if k[0] == "call" and k[1] > spot),
                   key=lambda kv: -kv[1])[:n]
    puts = sorted(((k[1], v) for k, v in oi.items() if k[0] == "put" and k[1] < spot),
                  key=lambda kv: -kv[1])[:n]
    return {"calls": [{"strike": k, "oi": int(v)} for k, v in calls],
            "puts": [{"strike": k, "oi": int(v)} for k, v in puts]}


def realised(symbol: str, bars: dict | None = None) -> Optional[float]:
    rows = (bars if bars is not None else market.load_bars("1Day", [symbol])).get(symbol) or []
    closes = [r["c"] for r in rows]
    return market.realised_vol(closes, len(closes) - 1, 20) if len(closes) > 21 else None


def measure(src, symbol: str, today: date | None = None, bars: dict | None = None) -> dict:
    """Everything the options market says about one underlying right now."""
    today = today or market.to_et(datetime.now(timezone.utc)).date()
    spot = src.quote(symbol)
    if not spot:
        raise OptionsDataError(f"no price for {symbol}")
    picks = pick_expirations(src.expirations(symbol), today)
    if not picks:
        raise OptionsDataError(f"no listed options for {symbol}")
    chains = {e: src.chain(symbol, e) for e in sorted(set(picks.values()))}
    out: dict = {"symbol": symbol, "spot": round(spot, 2), "source": src.name,
                 "realtime": src.realtime, "day": today.isoformat(), "term": {}}
    for key, exp in picks.items():
        a = atm(chains[exp], spot)
        if a:
            out["term"][key] = {"expiration": exp, "dte": _dte(exp, today), "strike": a["strike"],
                                "iv": a["iv"], "straddle": round(a["straddle"], 2),
                                "move_pct": a["move_pct"]}
    m30 = chains.get(picks.get("m30") or picks["front"]) or []
    put25, call25 = _nearest_delta(m30, "put", -0.25), _nearest_delta(m30, "call", 0.25)
    out["skew25"] = (put25["iv"] - call25["iv"]) if put25 and call25 else None
    every = [c for ch in chains.values() for c in ch]
    vol = {t: sum(c["volume"] for c in every if c["type"] == t) for t in ("call", "put")}
    oi = {t: sum(c["oi"] or 0 for c in every if c["type"] == t) for t in ("call", "put")}
    out["pcr_vol"] = vol["put"] / vol["call"] if vol["call"] else None
    out["pcr_oi"] = oi["put"] / oi["call"] if oi["call"] else None
    out["walls"] = walls(list(chains.values()), spot)
    out["max_pain"] = max_pain(chains[picks["front"]])
    out["rv20"] = realised(symbol, bars)
    iv30 = (out["term"].get("m30") or out["term"].get("front") or {}).get("iv")
    out["iv30"] = iv30
    out["iv_premium"] = (iv30 - out["rv20"]) if iv30 and out["rv20"] else None
    out["chains"] = chains                              # for the planner; not stored
    return out


# ── the record ──────────────────────────────────────────────────────────────

def init_db() -> None:
    market.init_db()
    with closing(market._connect()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS option_days (
                symbol TEXT NOT NULL,
                day    TEXT NOT NULL,
                spot   REAL NOT NULL,
                iv30   REAL,
                iv_front REAL,
                iv60   REAL,
                rv20   REAL,
                skew25 REAL,
                pcr_vol REAL,
                pcr_oi  REAL,
                PRIMARY KEY (symbol, day)
            );
            CREATE TABLE IF NOT EXISTS option_moves (
                symbol     TEXT NOT NULL,
                day        TEXT NOT NULL,
                expiration TEXT NOT NULL,
                spot       REAL NOT NULL,
                move       REAL NOT NULL,       -- dollars either way, from the straddle
                close      REAL,                -- the expiration day's close, once known
                inside     INTEGER,
                PRIMARY KEY (symbol, day, expiration)
            );
        """)


def save(m: dict) -> None:
    term = m["term"]
    with closing(market._connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO option_days VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (m["symbol"], m["day"], m["spot"], m.get("iv30"),
                      (term.get("front") or {}).get("iv"), (term.get("m60") or {}).get("iv"),
                      m.get("rv20"), m.get("skew25"), m.get("pcr_vol"), m.get("pcr_oi")))
        weekly = term.get("weekly")
        if weekly:
            conn.execute("INSERT OR IGNORE INTO option_moves (symbol, day, expiration, spot, move) "
                         "VALUES (?,?,?,?,?)", (m["symbol"], m["day"], weekly["expiration"],
                                                m["spot"], weekly["straddle"]))


def followed() -> list[str]:
    return list(dict.fromkeys(list(CORE) + market.watchlist()))[:RECORD_MAX]


def record_day(day: str, src=None, symbols: list[str] | None = None, pause: float = 0.3) -> dict:
    """After a close: measure and store every followed underlying, then score."""
    init_db()
    src = src or source()
    if src is None:
        return {"recorded": 0, "failed": [], "why": "no options data source"}
    done, failed = 0, []
    for sym in symbols or followed():
        try:
            m = measure(src, sym, date.fromisoformat(day))
            m["day"] = day
            save(m)
            done += 1
        except Exception as e:
            failed.append(sym)
            log.info("options record failed for %s: %s", sym, e)
        time.sleep(pause)
    scored = score_moves()
    market.set_state("options_recorded", day)
    return {"recorded": done, "failed": failed, "scored": scored}


def score_moves(bars: dict | None = None) -> int:
    """Settle each recorded expected move whose expiration has a daily close."""
    with closing(market._connect()) as conn:
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM option_moves WHERE inside IS NULL")]
    if not open_rows:
        return 0
    bars = bars if bars is not None else market.load_bars(
        "1Day", sorted({r["symbol"] for r in open_rows}))
    closes = {sym: {r["t"]: r["c"] for r in rows} for sym, rows in bars.items()}
    n = 0
    with closing(market._connect()) as conn, conn:
        for r in open_rows:
            close = closes.get(r["symbol"], {}).get(r["expiration"])
            if close is None:
                continue
            inside = int(abs(close - r["spot"]) <= r["move"])
            conn.execute("UPDATE option_moves SET close=?, inside=? WHERE symbol=? AND day=? "
                         "AND expiration=?", (close, inside, r["symbol"], r["day"], r["expiration"]))
            n += 1
    return n


def track_record(symbol: str, bars: dict | None = None) -> dict:
    """How the options market's pricing has held up for this underlying."""
    init_db()
    with closing(market._connect()) as conn:
        moves = [dict(r) for r in conn.execute(
            "SELECT * FROM option_moves WHERE symbol=? AND inside IS NOT NULL", (symbol,))]
        days = [dict(r) for r in conn.execute(
            "SELECT day, iv30 FROM option_days WHERE symbol=? AND iv30 IS NOT NULL ORDER BY day",
            (symbol,))]
    out: dict = {"moves": len(moves),
                 "inside": sum(m["inside"] for m in moves) / len(moves) if moves else None,
                 "iv_days": len(days)}
    rows = (bars if bars is not None else market.load_bars("1Day", [symbol])).get(symbol) or []
    index = {r["t"]: i for i, r in enumerate(rows)}
    closes = [r["c"] for r in rows]
    gaps = []
    for d in days:
        i = index.get(d["day"])
        if i is not None and i + RV_FORWARD < len(closes):
            later = market.realised_vol(closes, i + RV_FORWARD, RV_FORWARD)
            if later is not None:
                gaps.append(d["iv30"] - later)
    out["vrp_n"] = len(gaps)
    out["vrp_mean"] = statistics.fmean(gaps) if gaps else None
    out["vrp_over"] = sum(1 for g in gaps if g > 0) / len(gaps) if gaps else None
    return out


def iv_percentile(symbol: str, iv: float) -> Optional[float]:
    with closing(market._connect()) as conn:
        past = [r[0] for r in conn.execute(
            "SELECT iv30 FROM option_days WHERE symbol=? AND iv30 IS NOT NULL "
            "ORDER BY day DESC LIMIT 252", (symbol,))]
    if len(past) < IV_HISTORY_MIN:
        return None
    return 100 * sum(1 for v in past if v <= iv) / len(past)


# ── saying it ───────────────────────────────────────────────────────────────

def _p(x: Optional[float], digits: int = 1) -> str:
    return "unknown" if x is None else f"{100 * x:.{digits}f}%"


def richness(m: dict) -> str:
    """'rich', 'cheap' or 'fair', from implied against realised volatility."""
    prem = m.get("iv_premium")
    if prem is None:
        return "unknown"
    return "rich" if prem > 0.04 else "cheap" if prem < -0.02 else "fair"


def describe(symbol: str, src=None) -> str:
    init_db()
    src = src or source()
    if src is None:
        return ("I have no options data: Tradier's token (C:/MOM/.env) or an Alpaca key "
                "is needed.")
    try:
        m = measure(src, symbol)
    except OptionsDataError as e:
        return f"I couldn't read {symbol}'s options: {e}."
    lines = [f"{symbol} at {m['spot']} — options from {m['source']}."]
    term = m["term"]
    parts = [f"{k.replace('m', '') + '-day' if k.startswith('m') else k} "
             f"({t['expiration']}, {t['dte']}d) {_p(t['iv'])}"
             for k, t in term.items() if k != "weekly" and t["iv"]]
    if parts:
        shape = ""
        # A one- or two-day expiry's IV swings on any event; the weekly is the near term.
        near = (term.get("weekly") or term.get("front") or {}).get("iv")
        m30 = (term.get("m30") or {}).get("iv")
        if near and m30:
            front = near
            shape = (" — inverted: the near term is priced for trouble or an event"
                     if front > m30 + 0.02 else " — normal upward slope" if m30 > front else "")
        lines.append("Implied volatility: " + ", ".join(parts) + shape + ".")
    for k in ("weekly", "m30"):
        t = term.get(k)
        if t:
            lines.append(f"Expected move to {t['expiration']}: ±${t['straddle']:.2f} "
                         f"({_p(t['move_pct'])}), so {m['spot'] - t['straddle']:.2f} to "
                         f"{m['spot'] + t['straddle']:.2f}.")
    if m["iv_premium"] is not None:
        pct = iv_percentile(symbol, m["iv30"])
        lines.append(f"30-day implied {_p(m['iv30'])} against {_p(m['rv20'])} realised over 20 days: "
                     f"options look {richness(m)}"
                     + (f"; {pct:.0f}th percentile of the implied I've recorded." if pct is not None
                        else "; I need 20 recorded days before I can rank it."))
    if m["skew25"] is not None:
        lines.append(f"Skew: 25-delta puts {_p(m['skew25'])} over calls"
                     + (" — heavy demand for protection." if m["skew25"] > 0.06 else "."))
    pcr = [f"{name} {v:.2f}" for name, v in (("by volume", m["pcr_vol"]), ("by open interest", m["pcr_oi"]))
           if v is not None]
    if pcr:
        lines.append("Put/call ratio " + ", ".join(pcr) + ".")
    w = m["walls"]
    if w["calls"] or w["puts"]:
        lines.append("Open-interest walls: calls at " + (", ".join(f"{x['strike']:g}" for x in w["calls"]) or "none")
                     + "; puts at " + (", ".join(f"{x['strike']:g}" for x in w["puts"]) or "none")
                     + (f"; max pain {m['max_pain']:g}" if m["max_pain"] else "") + ".")
    rec = track_record(symbol)
    if rec["moves"]:
        lines.append(f"Track record: the price finished inside the weekly expected move "
                     f"{_p(rec['inside'], 0)} of {rec['moves']} times.")
    if rec["vrp_n"]:
        lines.append(f"Implied volatility overstated what followed {_p(rec['vrp_over'], 0)} of "
                     f"{rec['vrp_n']} days, by {100 * rec['vrp_mean']:+.1f} points on average.")
    if not rec["moves"] and not rec["vrp_n"]:
        lines.append("I've only begun recording these; the track record builds after each close.")
    if not m["realtime"]:
        lines.append("These quotes are Alpaca's indicative feed: delayed, and without open interest.")
    return "\n".join(lines)
