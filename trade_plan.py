"""JARVIS suggesting trades: levels, an entry, a stop, targets, and the option to use.

    levels    from daily bars: swing highs and lows of the last six months,
              the prior session's range, the 20-day and 52-week extremes and
              the 20/50/200-day averages, plus the options market's open-
              interest walls and expected-move bounds. Levels within a
              quarter ATR of each other merge, and a level touched more often
              is stronger
    bias      the trend, relative strength against the S&P, the market
              regime, and any CONFIRMED pattern from the market study that
              fired on this symbol within its holding period. Only a
              confirmed pattern is called an edge; the rest is a plan
    plan      entries at the nearest level (a pullback limit, or a breakout
              stop-entry) or at the market, a stop beyond the next level by
              ATR, targets at the next levels, and the reward against the
              risk. Under 1.5 to 1 the plan says to skip it
    options   the expiration that covers the holding period, the contract
              near the delta the style wants that has a tight spread and open
              interest, or a debit spread when options are rich, with the
              premium, breakeven, and estimated value at the stop and target
    learn     every suggestion is recorded (`trade_ideas`) and, after each
              close, scored on the daily bars: filled or not, then stop or
              target first (a bar that touches both counts as the stop), or
              closed at the end of its holding period. `track_record` is that
              tally, by style and setup, and it is what JARVIS says when asked
              how good his suggestions are

Nothing here can place an order. The user decides; JARVIS suggests.
"""
from __future__ import annotations

import logging
import math
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import market
import options

log = logging.getLogger("jarvis.trade_plan")

MIN_RR = 1.5
STYLES = {
    # holding sessions, stop in ATRs beyond the level, option delta, option days
    "day":   {"hold": 1, "stop_atr": 0.5, "delta": 0.50, "dte": (1, 7), "near_atr": 1.0},
    "swing": {"hold": 10, "stop_atr": 1.0, "delta": 0.60, "dte": (21, 60), "near_atr": 2.0},
}
FILL_SESSIONS = 3               # an unfilled entry is dropped after this many sessions
MAX_SPREAD = 0.10               # option spread as a fraction of its mid
MIN_OI = 100


# ── bars ────────────────────────────────────────────────────────────────────

def series_for(symbol: str, client: Optional[market.AlpacaData] = None) -> Optional[market.Series]:
    """Stored daily bars when the study follows the symbol, else fetched (not stored)."""
    rows = market.load_bars("1Day", [symbol]).get(symbol) or []
    if len(rows) < 60:
        client = client or market.MarketEngine._default_client()
        if client is None:
            return None
        end = datetime.now(timezone.utc) - timedelta(minutes=market.DATA_DELAY_MIN)
        got = client.bars([symbol], "1Day", end - timedelta(days=420), end).get(symbol) or []
        rows = [{"t": market.to_et(market.parse_utc(r["t"])).date().isoformat(), "o": r["o"],
                 "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]} for r in got]
    return market.Series.build(symbol, rows) if len(rows) >= 60 else None


# ── levels ──────────────────────────────────────────────────────────────────

def swing_points(s: market.Series, look: int = 126, k: int = 3) -> list[tuple[float, str]]:
    out = []
    start = max(k, len(s) - look)
    for i in range(start, len(s) - k):
        if s.h[i] == max(s.h[i - k:i + k + 1]):
            out.append((s.h[i], "swing high"))
        if s.l[i] == min(s.l[i - k:i + k + 1]):
            out.append((s.l[i], "swing low"))
    return out


def levels(s: market.Series, price: float, opt: dict | None = None) -> dict:
    """Support below the price and resistance above, nearest first."""
    i = len(s) - 1
    a = s.atr14[i] or price * 0.02
    raw = swing_points(s)
    raw += [(s.h[i], "prior high"), (s.l[i], "prior low"), (s.c[i], "prior close"),
            (max(s.h[-20:]), "20-day high"), (min(s.l[-20:]), "20-day low"),
            (max(s.h[-252:]), "52-week high"), (min(s.l[-252:]), "52-week low")]
    for avg, name in ((s.sma20, "20-day average"), (s.sma50, "50-day average"),
                      (s.sma200, "200-day average")):
        if avg[i] is not None:
            raw.append((avg[i], name))
    if opt:
        for w in opt.get("walls", {}).get("calls", []):
            raw.append((w["strike"], "call open-interest wall"))
        for w in opt.get("walls", {}).get("puts", []):
            raw.append((w["strike"], "put open-interest wall"))
        weekly = (opt.get("term") or {}).get("weekly")
        if weekly:
            raw += [(opt["spot"] + weekly["straddle"], "top of the weekly expected move"),
                    (opt["spot"] - weekly["straddle"], "bottom of the weekly expected move")]
    raw.sort()
    merged: list[dict] = []
    for px, why in raw:
        if merged and px - merged[-1]["hi"] <= 0.25 * a:
            m = merged[-1]
            m["hi"], m["n"] = px, m["n"] + 1
            m["px"] = (m["px"] * (m["n"] - 1) + px) / m["n"]
            if why not in m["why"]:
                m["why"].append(why)
        else:
            merged.append({"px": px, "hi": px, "n": 1, "why": [why]})
    zone = [dict(px=round(m["px"], 2), strength=m["n"], why=m["why"]) for m in merged]
    gap = 0.1 * a
    return {"atr": a,
            "support": sorted((z for z in zone if z["px"] < price - gap), key=lambda z: -z["px"]),
            "resistance": sorted((z for z in zone if z["px"] > price + gap), key=lambda z: z["px"])}


# ── bias ────────────────────────────────────────────────────────────────────

def confirmed_setups(symbol: str, horizon_days: int = 20) -> list[dict]:
    """Confirmed patterns that fired on this symbol and are still inside their horizon."""
    with closing(market._connect()) as conn:
        status = {r["name"]: r["status"] for r in conn.execute("SELECT name, status FROM market_patterns")}
        rows = [dict(r) for r in conn.execute(
            "SELECT pattern, day, direction, horizon FROM market_signals WHERE symbol=? "
            "ORDER BY day DESC LIMIT 40", (symbol,))]
    cutoff = (date.today() - timedelta(days=horizon_days * 2)).isoformat()
    out = []
    for r in rows:
        p = market.PATTERNS_BY_NAME.get(r["pattern"])
        if p and status.get(r["pattern"]) == market.CONFIRMED and r["day"] >= cutoff:
            out.append({"pattern": p.name, "label": p.label, "day": r["day"],
                        "direction": r["direction"], "horizon": r["horizon"]})
    return out


def bias(s: market.Series, price: float, regime_trend: str, setups: list[dict],
         spy: Optional[market.Series] = None) -> dict:
    trend = market.trend_label(s, price)
    score, why = 0, []
    t_score = {"uptrend": 2, "recovering": 1, "pullback": 1, "rebounding": -1,
               "weak": -1, "downtrend": -2}.get(trend, 0)
    if t_score:
        score += t_score
        why.append(f"{trend} ({t_score:+d})")
    if spy is not None and len(spy) > 20 and len(s) > 20:
        rs = (price / s.c[-20] - 1) - (spy.c[-1] / spy.c[-20] - 1)
        r_score = 1 if rs > 0.02 else -1 if rs < -0.02 else 0
        if r_score:
            score += r_score
            why.append(f"{100 * rs:+.1f}% against the S&P over 20 days ({r_score:+d})")
    g_score = {"uptrend": 1, "downtrend": -1}.get(regime_trend, 0)
    if g_score:
        score += g_score
        why.append(f"market {regime_trend} ({g_score:+d})")
    for st in setups:
        score += 2 * st["direction"]
        why.append(f"confirmed pattern '{st['label']}' on {st['day']} ({2 * st['direction']:+d})")
    direction = "long" if score >= 2 else "short" if score <= -2 else ""
    return {"score": score, "direction": direction, "trend": trend, "why": why,
            "edge": bool(setups) and all(st["direction"] == (1 if direction == "long" else -1)
                                         for st in setups) and bool(direction)}


# ── the plan ────────────────────────────────────────────────────────────────

def _targets(entry: float, risk: float, side: int, beyond: list[dict]) -> list[float]:
    """The next levels at least one R away, or measured 2R/3R when there are none."""
    far = [z["px"] for z in beyond if side * (z["px"] - entry) >= risk]
    t = far[:2]
    while len(t) < 2:
        t.append(entry + side * risk * (2 if not t else 3))
    if side * (t[1] - t[0]) <= 0:
        t[1] = t[0] + side * risk
    return [round(x, 2) for x in t]


def plan(s: market.Series, price: float, direction: str, style: str = "swing",
         opt: dict | None = None, risk_dollars: float | None = None) -> dict:
    cfg = STYLES[style]
    lv = levels(s, price, opt)
    a, side = lv["atr"], 1 if direction == "long" else -1
    behind = lv["support"] if side == 1 else lv["resistance"]     # where a pullback stops
    ahead = lv["resistance"] if side == 1 else lv["support"]
    near = cfg["near_atr"] * a
    entries = []
    if behind and abs(price - behind[0]["px"]) <= near:
        lvl = behind[0]
        entries.append({"kind": "pullback", "entry": lvl["px"],
                        "stop": lvl["px"] - side * cfg["stop_atr"] * a,
                        "how": f"a limit at {lvl['px']:.2f} ({', '.join(lvl['why'][:3])})"})
    if ahead and abs(ahead[0]["px"] - price) <= near:
        lvl = ahead[0]
        entry = lvl["px"] + side * 0.1 * a
        entries.append({"kind": "breakout", "entry": entry,
                        "stop": lvl["px"] - side * cfg["stop_atr"] * a,
                        "how": f"a stop order through {lvl['px']:.2f} ({', '.join(lvl['why'][:3])})"})
    if not entries:
        entries.append({"kind": "market", "entry": price,
                        "stop": price - side * (1.5 if style == "swing" else 0.75) * a,
                        "how": "at the market: no level is close enough to wait for"})
    for e in entries:
        risk = abs(e["entry"] - e["stop"])
        e["entry"], e["stop"] = round(e["entry"], 2), round(e["stop"], 2)
        beyond = [z for z in ahead if side * (z["px"] - e["entry"]) > 0]
        e["targets"] = _targets(e["entry"], risk, side, beyond)
        e["rr"] = round(abs(e["targets"][0] - e["entry"]) / risk, 2) if risk else 0.0
        e["ok"] = e["rr"] >= MIN_RR
        if risk_dollars and risk:
            e["shares"] = int(risk_dollars // risk)
    best = max(entries, key=lambda e: (e["ok"], e["rr"]))
    return {"style": style, "direction": direction, "atr": round(a, 2), "levels": lv,
            "entries": entries, "best": best, "hold": cfg["hold"]}


# ── options for a plan ──────────────────────────────────────────────────────

def _liquid(c: dict) -> bool:
    if not c["mid"] or not c["bid"] or c["delta"] is None:
        return False
    if (c["ask"] - c["bid"]) > max(0.05, MAX_SPREAD * c["mid"]):
        return False
    return c["oi"] is None or c["oi"] >= MIN_OI


def value_at(c: dict, move: float, days: float) -> float:
    """Delta-gamma-theta estimate of a contract's price after a move."""
    v = c["mid"] + c["delta"] * move + 0.5 * (c["gamma"] or 0) * move * move + (c["theta"] or 0) * days
    return max(0.0, v)


def option_choice(src, symbol: str, p: dict, opt: dict, today: date | None = None) -> dict:
    """The contract (or debit spread) to express the plan's best entry."""
    today = today or market.to_et(datetime.now(timezone.utc)).date()
    cfg = STYLES[p["style"]]
    lo, hi = cfg["dte"]
    exps = [e for e in src.expirations(symbol) if lo <= options._dte(e, today) <= hi]
    if not exps:
        return {"why": f"no expiration between {lo} and {hi} days"}
    target_dte = (lo + hi) / 2 if p["style"] == "swing" else lo
    exp = min(exps, key=lambda e: abs(options._dte(e, today) - target_dte))
    chain = opt["chains"].get(exp) or src.chain(symbol, exp)
    kind = "call" if p["direction"] == "long" else "put"
    want = cfg["delta"] if kind == "call" else -cfg["delta"]
    pool = [c for c in chain if c["type"] == kind and _liquid(c)]
    if not pool:
        return {"why": f"no {kind} on {exp} with a tight spread and open interest"}
    c = min(pool, key=lambda c: abs(c["delta"] - want))
    best, spot = p["best"], opt["spot"]
    held = 0 if p["style"] == "day" else min(5, p["hold"])
    at_entry = value_at(c, best["entry"] - spot, 0)
    at_stop = value_at(c, best["stop"] - spot, held)
    at_t1 = value_at(c, best["targets"][0] - spot, held)
    out = {"expiration": exp, "dte": options._dte(exp, today), "type": kind, "strike": c["strike"],
           "symbol": c["symbol"], "bid": c["bid"], "ask": c["ask"], "mid": round(c["mid"], 2),
           "delta": round(c["delta"], 2), "theta": round(c["theta"] or 0, 3), "iv": c["iv"],
           "oi": c["oi"], "at_entry": round(at_entry, 2), "at_stop": round(at_stop, 2),
           "at_t1": round(at_t1, 2),
           "breakeven": round(c["strike"] + c["mid"] if kind == "call" else c["strike"] - c["mid"], 2),
           "risk_per": round((at_entry - at_stop) * 100, 2)}
    if options.richness(opt) == "rich":
        t1 = best["targets"][0]
        shorts = [x for x in chain if x["type"] == kind and x["mid"] and x["bid"]
                  and (x["strike"] - c["strike"]) * (1 if kind == "call" else -1) > 0]
        if shorts:
            short = min(shorts, key=lambda x: abs(x["strike"] - t1))
            cost = c["mid"] - short["mid"]
            width = abs(short["strike"] - c["strike"])
            if 0 < cost < width:
                out["spread"] = {"short_strike": short["strike"], "cost": round(cost, 2),
                                 "max_gain": round(width - cost, 2), "width": width}
    return out


# ── the record of suggestions ───────────────────────────────────────────────

def init_db() -> None:
    market.init_db()
    with closing(market._connect()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_ideas (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL NOT NULL,
                day       TEXT NOT NULL,
                symbol    TEXT NOT NULL,
                direction TEXT NOT NULL,
                style     TEXT NOT NULL,
                kind      TEXT NOT NULL,          -- pullback / breakout / market
                entry     REAL NOT NULL,
                stop      REAL NOT NULL,
                target    REAL NOT NULL,
                hold      INTEGER NOT NULL,
                edge      INTEGER NOT NULL,       -- a confirmed pattern stood behind it
                status    TEXT NOT NULL DEFAULT 'open',   -- open/filled/won/lost/closed/unfilled
                filled    TEXT,
                result_r  REAL,
                UNIQUE (day, symbol, direction, style, kind)
            );
        """)


def record(symbol: str, day: str, p: dict, edge: bool) -> None:
    init_db()
    with closing(market._connect()) as conn, conn:
        for e in p["entries"]:
            if not e["ok"]:
                continue
            conn.execute("INSERT OR IGNORE INTO trade_ideas (ts, day, symbol, direction, style, kind, "
                         "entry, stop, target, hold, edge) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (time.time(), day, symbol, p["direction"], p["style"], e["kind"],
                          e["entry"], e["stop"], e["targets"][0], p["hold"], int(edge)))


def resolve_idea(idea: dict, bars: list) -> Optional[dict]:
    """Walk the sessions after the suggestion, from the start every time.
    None while nothing has happened yet."""
    side = 1 if idea["direction"] == "long" else -1
    after = [b for b in bars if b["t"] > idea["day"]]
    entry, stop, target = idea["entry"], idea["stop"], idea["target"]
    filled_i = None
    for i, b in enumerate(after):
        if filled_i is None:
            if i >= FILL_SESSIONS:
                return {"status": "unfilled"}
            if idea["kind"] == "market":
                entry = b["o"]
            elif not ((b["l"] <= entry if side == 1 else b["h"] >= entry) if idea["kind"] == "pullback"
                      else (b["h"] >= entry if side == 1 else b["l"] <= entry)):
                continue
            filled_i = i
        risk = abs(entry - stop)
        filled = after[filled_i]["t"]
        if (b["l"] <= stop) if side == 1 else (b["h"] >= stop):
            return {"status": "lost", "filled": filled, "result_r": -1.0}
        if (b["h"] >= target) if side == 1 else (b["l"] <= target):
            return {"status": "won", "filled": filled,
                    "result_r": round(abs(target - entry) / risk, 2) if risk else 0.0}
        if i - filled_i + 1 >= idea["hold"]:
            return {"status": "closed", "filled": filled,
                    "result_r": round(side * (b["c"] - entry) / risk, 2) if risk else 0.0}
    if filled_i is not None:
        return {"status": "filled", "filled": after[filled_i]["t"]}
    return {"status": "unfilled"} if len(after) >= FILL_SESSIONS else None


def score_ideas(load=None) -> int:
    """After a close: settle every open suggestion the bars now decide."""
    init_db()
    with closing(market._connect()) as conn:
        open_ideas = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_ideas WHERE status IN ('open', 'filled')")]
    if not open_ideas:
        return 0
    load = load or (lambda sym: market.load_bars("1Day", [sym]).get(sym) or [])
    n = 0
    bars_by: dict[str, list] = {}
    with closing(market._connect()) as conn, conn:
        for idea in open_ideas:
            sym = idea["symbol"]
            if sym not in bars_by:
                try:
                    bars_by[sym] = load(sym)
                except Exception:
                    bars_by[sym] = []
            got = resolve_idea(idea, bars_by[sym])
            if got is None or (got["status"] == idea["status"] and got.get("filled") == idea["filled"]):
                continue
            conn.execute("UPDATE trade_ideas SET status=?, filled=?, result_r=? WHERE id=?",
                         (got["status"], got.get("filled"), got.get("result_r"), idea["id"]))
            n += got["status"] not in ("filled",)
    return n


def track_record() -> dict:
    init_db()
    with closing(market._connect()) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT style, kind, edge, status, result_r FROM trade_ideas")]
    done = [r for r in rows if r["status"] in ("won", "lost", "closed")]
    out = {"suggested": len(rows), "open": sum(r["status"] in ("open", "filled") for r in rows),
           "unfilled": sum(r["status"] == "unfilled" for r in rows), "done": len(done), "groups": {}}
    for r in done:
        key = f"{r['style']} {r['kind']}" + (" with an edge" if r["edge"] else "")
        g = out["groups"].setdefault(key, {"n": 0, "won": 0, "r": 0.0})
        g["n"] += 1
        g["won"] += r["status"] == "won" or (r["result_r"] or 0) > 0
        g["r"] += r["result_r"] or 0
    if done:
        out["win_rate"] = sum(1 for r in done if (r["result_r"] or 0) > 0) / len(done)
        out["avg_r"] = sum(r["result_r"] or 0 for r in done) / len(done)
    return out


def describe_record() -> str:
    t = track_record()
    if not t["suggested"]:
        return "I haven't suggested any trades yet, so I have no record to judge them by."
    lines = [f"I've suggested {t['suggested']} entries: {t['done']} finished, {t['open']} still open, "
             f"{t['unfilled']} never filled."]
    if t["done"]:
        lines.append(f"Of the finished ones {100 * t['win_rate']:.0f}% made money, averaging "
                     f"{t['avg_r']:+.2f}R per trade.")
        for key, g in sorted(t["groups"].items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"  {key}: {g['n']} trades, {g['won']} winners, {g['r'] / g['n']:+.2f}R average.")
        if t["done"] < 20:
            lines.append("That's too few to call it skill yet; I'd want twenty or more.")
    return "\n".join(lines)


# ── what the user hears ─────────────────────────────────────────────────────

def _lvl(z: dict) -> str:
    return f"{z['px']:.2f} ({', '.join(z['why'][:2])}{'; strong' if z['strength'] >= 3 else ''})"


def _regime_and_spy() -> tuple[str, Optional[market.Series]]:
    snap = market.get_state("snapshot") or {}
    trend = (snap.get("regime") or {}).get("trend", "unknown")
    spy_rows = market.load_bars("1Day", [market.BENCHMARK]).get(market.BENCHMARK) or []
    return trend, (market.Series.build(market.BENCHMARK, spy_rows) if len(spy_rows) > 20 else None)


def current_price(symbol: str, s: market.Series, src=None) -> tuple[float, str]:
    if src is not None:
        try:
            px = src.quote(symbol)
            if px:
                return px, "live" if src.realtime else "delayed"
        except Exception:
            pass
    intraday = market.load_bars("15Min", [symbol]).get(symbol) or []
    if intraday:
        return intraday[-1]["c"], "15 minutes delayed"
    return s.c[-1], "the last close"


def _contracts(risk_dollars: float | None, per: float) -> str:
    if not risk_dollars or per <= 0:
        return ""
    n = int(risk_dollars // per)
    return (f" ({n} contract{'s' if n != 1 else ''} for your ${risk_dollars:.0f} risk)" if n
            else f" — one contract risks more than your ${risk_dollars:.0f}")


def suggest(symbol: str, direction: str = "", style: str = "swing", instrument: str = "both",
            risk_dollars: float | None = None, src=None, client=None, save: bool = True) -> str:
    style = style if style in STYLES else "swing"
    s = series_for(symbol, client)
    if s is None:
        return f"I couldn't get enough daily history for {symbol} to plan a trade."
    src = src if src is not None else (options.source() if options.enabled() else None)
    price, fresh = current_price(symbol, s, src)
    opt = None
    if src is not None and instrument in ("options", "both"):
        try:
            opt = options.measure(src, symbol, bars={symbol: []})
            opt["rv20"] = market.realised_vol(s.c, len(s) - 1, 20)
            opt["iv_premium"] = (opt["iv30"] - opt["rv20"]) if opt["iv30"] and opt["rv20"] else None
        except Exception as e:
            log.info("options for %s unavailable: %s", symbol, e)
    init_db()
    regime, spy = _regime_and_spy()
    setups = confirmed_setups(symbol)
    b = bias(s, price, regime, setups, spy)
    chosen = direction if direction in ("long", "short") else b["direction"]
    lines = [f"{symbol} at {price:.2f} ({fresh}); {b['trend']}, daily range about "
             f"{s.atr14[-1] or 0:.2f}."]
    lines.append("Bias " + (b["direction"] or "none") + f" (score {b['score']:+d}: "
                 + ("; ".join(b["why"]) or "nothing leaning either way") + ").")
    if chosen and chosen != b["direction"]:
        lines.append(f"You asked for the {chosen} side" + (", which is against that evidence."
                     if b["direction"] else "; the evidence doesn't lean either way."))
    lv = levels(s, price, opt)
    if lv["resistance"]:
        lines.append("Resistance: " + ", ".join(_lvl(z) for z in lv["resistance"][:3]) + ".")
    if lv["support"]:
        lines.append("Support: " + ", ".join(_lvl(z) for z in lv["support"][:3]) + ".")
    if not chosen:
        lines.append("No trade from me: the trend, the market and the patterns don't agree. "
                     "If you want a side anyway, ask for the long or the short plan.")
        return "\n".join(lines)
    p = plan(s, price, chosen, style, opt, risk_dollars)
    word = "Buy" if chosen == "long" else "Sell short"
    for e in p["entries"]:
        lines.append(f"{e['kind'].capitalize()}: {word.lower()} with {e['how']}; stop {e['stop']:.2f}; "
                     f"targets {e['targets'][0]:.2f} then {e['targets'][1]:.2f}; reward {e['rr']:.1f} to 1"
                     + (f"; {e['shares']} shares for your risk" if e.get("shares") else "")
                     + ("" if e["ok"] else " — skip it, the reward is too small for the risk") + ".")
    best = p["best"]
    if best["ok"]:
        exit_rule = ("Out by the close whatever happens." if style == "day" else
                     f"Take half at the first target and move the stop to the entry; otherwise "
                     f"close it after {p['hold']} sessions.")
        lines.append(f"My pick is the {best['kind']}. {exit_rule}")
    else:
        lines.append("None of these pays enough for its risk; I'd wait for a better level.")
    if instrument in ("options", "both") and opt and best["ok"]:
        try:
            oc = option_choice(src, symbol, p, opt)
        except Exception as e:
            oc = {"why": str(e)}
        if "why" in oc:
            lines.append(f"Options: nothing suitable ({oc['why']}).")
        else:
            lines.append(
                f"Options: the {oc['expiration']} {oc['strike']:g} {oc['type']} ({oc['dte']} days), "
                f"{oc['bid']:.2f} bid / {oc['ask']:.2f} ask, delta {oc['delta']:.2f}, losing "
                f"{abs(oc['theta']):.2f} a day to time; breakeven at expiry {oc['breakeven']:.2f}. "
                f"Estimated about {oc['at_entry']:.2f} when the entry fills, {oc['at_t1']:.2f} at the "
                f"first target and {oc['at_stop']:.2f} at the stop, so roughly "
                f"${oc['risk_per']:.0f} at risk per contract" + _contracts(risk_dollars, oc['risk_per'])
                + ". Those are estimates from the greeks, not quotes.")
            sp = oc.get("spread")
            if sp:
                lines.append(f"Options are rich here, so a debit spread is cheaper: buy the {oc['strike']:g}, "
                             f"sell the {sp['short_strike']:g}, about {sp['cost']:.2f} for up to "
                             f"{sp['max_gain']:.2f} more.")
    elif instrument in ("options", "both") and not opt:
        lines.append("Options: I couldn't read the chain just now.")
    if b["edge"]:
        lines.append("A confirmed pattern from my study stands behind this side.")
    else:
        lines.append("No confirmed pattern stands behind it: this is a levels-and-trend plan, not a "
                     "tested edge.")
    if save:
        try:
            record(symbol, (market.get_state("snapshot") or {}).get("session", {}).get("day")
                   or market.to_et(datetime.now(timezone.utc)).date().isoformat(), p, b["edge"])
        except Exception:
            log.warning("could not record the suggestion", exc_info=True)
    lines.append("It's a suggestion — you decide, and I don't place it.")
    return "\n".join(lines)


def scan(style: str = "swing", limit: int = 3) -> str:
    """The followed symbols with the clearest plan right now, stock levels only."""
    style = style if style in STYLES else "swing"
    init_db()
    regime, spy = _regime_and_spy()
    syms = [x for x in market.watchlist() + list(market.MARKET_SYMBOLS) + list(market.SECTORS)
            if x not in ("VIXY", "TLT", "GLD")]
    rows = market.load_bars("1Day", syms)
    found = []
    for sym in dict.fromkeys(syms):
        r = rows.get(sym) or []
        if len(r) < market.WARMUP:
            continue
        s = market.Series.build(sym, r)
        price = s.c[-1]
        setups = confirmed_setups(sym)
        b = bias(s, price, regime, setups, spy)
        if not b["direction"]:
            continue
        p = plan(s, price, b["direction"], style)
        if p["best"]["ok"]:
            found.append((b["edge"], p["best"]["rr"], abs(b["score"]), sym, b, p))
    if not found:
        return ("Nothing on my list has a clear side and a level worth trading right now; "
                "that's a fine answer.")
    found.sort(reverse=True)
    lines = [f"The clearest {style} plans on my list, from the last close (ask about any one for "
             f"the full plan and the option):"]
    for edge, rr, _, sym, b, p in found[:limit]:
        e = p["best"]
        lines.append(f"{sym} {b['direction']} ({b['trend']}{', confirmed pattern' if edge else ''}): "
                     f"{e['kind']} entry {e['entry']:.2f}, stop {e['stop']:.2f}, target "
                     f"{e['targets'][0]:.2f}, {rr:.1f} to 1.")
    lines.append("These are suggestions from fixed rules, not a promise; you decide.")
    return "\n".join(lines)
