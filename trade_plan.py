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
              risk. Under 1.5 to 1 the plan says to skip it. The best entry
              is the one most likely to pay: its reward, weighted by how
              likely a resting order that far away is to fill, and, once
              twenty of that kind have finished, by what that kind has
              actually returned
    exits     a managed exit beside the plain one: half at the first target,
              the stop to the entry, the rest trailed by ATR to the second
              target, and out early if it hasn't moved half an R by halfway
              through the holding period. Both are scored on every
              suggestion, and once twenty have finished the plan leads with
              the one that has done better
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

Nothing here can place an order. JARVIS advises — the user has accepted that
JARVIS is not a licensed adviser, and the risk — and the user decides.
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
    # holding sessions, stop in ATRs beyond the level, option delta, option days,
    # how far away an entry may rest, and the trail for the managed exit's runner
    "day":   {"hold": 1, "stop_atr": 0.5, "delta": 0.50, "dte": (1, 7), "near_atr": 1.0,
              "trail_atr": 1.0},
    "swing": {"hold": 10, "stop_atr": 1.0, "delta": 0.60, "dte": (21, 60), "near_atr": 2.0,
              "trail_atr": 2.0},
}
FILL_SESSIONS = 3               # an unfilled entry is dropped after this many sessions
MAX_SPREAD = 0.10               # option spread as a fraction of its mid
MIN_OI = 100
LEARN_MIN = 20                  # finished suggestions before the record steers the plan
STALE_R = 0.5                   # the managed exit leaves at halfway if it hasn't made this


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


def fill_chance(e: dict, price: float, near: float) -> float:
    """How likely a resting entry is to fill: certain at the market, falling
    to a third at the farthest distance the style lets an entry rest."""
    if e["kind"] == "market" or near <= 0:
        return 1.0
    return max(0.33, 1.0 - 0.67 * min(1.0, abs(e["entry"] - price) / near))


def learned_r(style: str) -> dict[str, float]:
    """Average R by entry kind, for kinds with LEARN_MIN finished suggestions."""
    try:
        groups = track_record()["groups"]
    except Exception:
        return {}
    return {key.split(" ")[1]: g["r"] / g["n"] for key, g in groups.items()
            if key.startswith(style + " ") and "edge" not in key and g["n"] >= LEARN_MIN}


def exit_plan(e: dict, side: int, a: float, style: str, hold: int) -> dict:
    cfg = STYLES[style]
    risk = abs(e["entry"] - e["stop"])
    return {"stop": e["stop"], "t1": e["targets"][0], "t2": e["targets"][1],
            "trail": round(cfg["trail_atr"] * a, 2),
            "stale_by": max(1, hold // 2), "stale_at": round(e["entry"] + side * STALE_R * risk, 2),
            "hold": hold}


def plan(s: market.Series, price: float, direction: str, style: str = "swing",
         opt: dict | None = None, risk_dollars: float | None = None,
         learned: dict | None = None) -> dict:
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
                        "how": f"a limit at {lvl['px']:.2f} ({', '.join(lvl['why'][:2])})"})
    if ahead and abs(ahead[0]["px"] - price) <= near:
        lvl = ahead[0]
        entry = lvl["px"] + side * 0.1 * a
        entries.append({"kind": "breakout", "entry": entry,
                        "stop": lvl["px"] - side * cfg["stop_atr"] * a,
                        "how": f"a stop order through {lvl['px']:.2f} ({', '.join(lvl['why'][:2])})"})
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
        e["fill"] = round(fill_chance(e, price, near), 2)
        e["learned_r"] = (learned or {}).get(e["kind"])
        pays = e["learned_r"] if e["learned_r"] is not None else e["rr"]
        e["score"] = round(e["fill"] * pays, 3)
        e["exit"] = exit_plan(e, side, a, style, cfg["hold"])
    best = max(entries, key=lambda e: (e["ok"], e["score"], e["rr"]))
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
        have = {r["name"] for r in conn.execute("PRAGMA table_info(trade_ideas)")}
        # Added with the managed exit; rows from before it have no second
        # target, so only the plain exit is scored for them.
        for col in ("target2 REAL", "atr REAL", "result_managed REAL"):
            if col.split()[0] not in have:
                conn.execute(f"ALTER TABLE trade_ideas ADD COLUMN {col}")


def record(symbol: str, day: str, p: dict, edge: bool) -> None:
    init_db()
    with closing(market._connect()) as conn, conn:
        for e in p["entries"]:
            if not e["ok"]:
                continue
            conn.execute("INSERT OR IGNORE INTO trade_ideas (ts, day, symbol, direction, style, kind, "
                         "entry, stop, target, hold, edge, target2, atr) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (time.time(), day, symbol, p["direction"], p["style"], e["kind"],
                          e["entry"], e["stop"], e["targets"][0], p["hold"], int(edge),
                          e["targets"][1], p["atr"]))


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


def resolve_managed(idea: dict, filled: str, bars: list) -> Optional[float]:
    """The managed exit on the same fill: half off at the first target with
    the stop moved to the entry, the rest trailed by ATR from the best close
    to the second target, out at halfway if it hasn't made STALE_R, and at
    the holding period's end. A bar that touches a stop and a target counts
    as the stop. None while the bars don't decide it yet."""
    if not idea.get("target2") or not idea.get("atr"):
        return None
    cfg = STYLES.get(idea["style"], STYLES["swing"])
    side = 1 if idea["direction"] == "long" else -1
    after = [b for b in bars if b["t"] >= filled]
    if not after:
        return None
    entry = after[0]["o"] if idea["kind"] == "market" else idea["entry"]
    risk = abs(entry - idea["stop"])
    if not risk:
        return 0.0
    r = lambda px: side * (px - entry) / risk
    stop, banked, half, best = idea["stop"], 0.0, False, entry
    for i, b in enumerate(after):
        worst_px, best_px = (b["l"], b["h"]) if side == 1 else (b["h"], b["l"])
        if side * (worst_px - stop) <= 0:
            return round(banked + (0.5 if half else 1.0) * r(stop), 2)
        if not half and side * (best_px - idea["target"]) >= 0:
            banked, half, stop = 0.5 * r(idea["target"]), True, entry
        if half and side * (best_px - idea["target2"]) >= 0:
            return round(banked + 0.5 * r(idea["target2"]), 2)
        best = max(best, b["c"]) if side == 1 else min(best, b["c"])
        if half:
            trail = best - side * cfg["trail_atr"] * idea["atr"]
            stop = max(stop, trail) if side == 1 else min(stop, trail)
        held = i + 1
        if not half and held == max(1, idea["hold"] // 2) and idea["hold"] > 1 \
                and r(b["c"]) < STALE_R:
            return round(r(b["c"]), 2)
        if held >= idea["hold"]:
            return round(banked + (0.5 if half else 1.0) * r(b["c"]), 2)
    return None


def score_ideas(load=None) -> int:
    """After a close: settle every open suggestion the bars now decide, and
    the managed exit of each filled one."""
    init_db()
    with closing(market._connect()) as conn:
        open_ideas = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_ideas WHERE status IN ('open', 'filled') OR (status IN "
            "('won', 'lost', 'closed') AND result_managed IS NULL AND target2 IS NOT NULL)")]
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
            managed = (resolve_managed(idea, got["filled"], bars_by[sym])
                       if got and got.get("filled") else None)
            if managed is not None:
                conn.execute("UPDATE trade_ideas SET result_managed=? WHERE id=?",
                             (managed, idea["id"]))
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
            "SELECT style, kind, edge, status, result_r, result_managed FROM trade_ideas")]
    done = [r for r in rows if r["status"] in ("won", "lost", "closed")]
    both = [r for r in done if r["result_managed"] is not None]
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
    if both:
        out["exits"] = {"n": len(both),
                        "fixed": sum(r["result_r"] or 0 for r in both) / len(both),
                        "managed": sum(r["result_managed"] for r in both) / len(both)}
    return out


def exit_preference() -> str:
    """'managed' or 'fixed' once LEARN_MIN suggestions have both scores; '' before."""
    try:
        ex = track_record().get("exits")
    except Exception:
        return ""
    if not ex or ex["n"] < LEARN_MIN:
        return ""
    return "managed" if ex["managed"] > ex["fixed"] else "fixed"


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
        ex = t.get("exits")
        if ex:
            lines.append(f"Exits, on the {ex['n']} scored both ways: taking it all at the first "
                         f"target averaged {ex['fixed']:+.2f}R; the managed exit (half there, the "
                         f"rest trailed) averaged {ex['managed']:+.2f}R.")
        if t["done"] < LEARN_MIN:
            lines.append("That's too few to call it skill yet; I'd want twenty or more.")
    return "\n".join(lines)


# ── what the user hears ─────────────────────────────────────────────────────

def _lvl(z: dict) -> str:
    return f"{z['px']:.2f} ({z['why'][0]}{', strong' if z['strength'] >= 3 else ''})"


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
        lines.append("Resistance: " + ", ".join(_lvl(z) for z in lv["resistance"][:2]) + ".")
    if lv["support"]:
        lines.append("Support: " + ", ".join(_lvl(z) for z in lv["support"][:2]) + ".")
    if not chosen:
        lines.append("No trade from me: the trend, the market and the patterns don't agree. "
                     "If you want a side anyway, ask for the long or the short plan.")
        return "\n".join(lines)
    p = plan(s, price, chosen, style, opt, risk_dollars, learned_r(style))
    word = "Buy" if chosen == "long" else "Sell short"
    best = p["best"]
    if best["ok"]:
        why = (f"my finished {best['kind']}s have averaged {best['learned_r']:+.2f}R"
               if best["learned_r"] is not None else "most reward once the fill odds count")
        lines.append(f"Best entry, the {best['kind']} ({why}): {word.lower()} with {best['how']}; "
                     f"stop {best['stop']:.2f}; targets {best['targets'][0]:.2f} then "
                     f"{best['targets'][1]:.2f}; reward {best['rr']:.1f} to 1"
                     + (f", about {100 * best['fill']:.0f}% likely to fill" if best["kind"] != "market" else "")
                     + (f"; {best['shares']} shares for your risk" if best.get("shares") else "") + ".")
        lines.append(_exit_text(best, style, exit_preference()))
    for e in p["entries"]:
        if e is best and e["ok"]:
            continue
        lines.append(f"{'Other' if e['ok'] else 'Skip the'} {e['kind']}: {e['entry']:.2f}, stop "
                     f"{e['stop']:.2f}, {e['rr']:.1f} to 1"
                     + (f", {100 * e['fill']:.0f}% to fill" if e["kind"] != "market" else "") + ".")
    if not best["ok"]:
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
                f"{oc['bid']:.2f}/{oc['ask']:.2f}, delta {oc['delta']:.2f}, losing "
                f"{abs(oc['theta']):.2f} a day; breakeven {oc['breakeven']:.2f}. By the greeks "
                f"~{oc['at_entry']:.2f} at the entry, {oc['at_t1']:.2f} at the first target, "
                f"{oc['at_stop']:.2f} at the stop: ${oc['risk_per']:.0f} risk per contract"
                + _contracts(risk_dollars, oc['risk_per']) + ".")
            sp = oc.get("spread")
            if sp:
                lines.append(f"Options are rich: the {oc['strike']:g}/{sp['short_strike']:g} {oc['type']} "
                             f"spread costs about {sp['cost']:.2f} to make up to {sp['max_gain']:.2f}.")
            lines.append(_option_exit_text(oc, best))
    elif instrument in ("options", "both") and not opt:
        lines.append("Options: I couldn't read the chain just now.")
    if b["edge"]:
        lines.append("A confirmed pattern from my study stands behind this side.")
    elif not best["ok"]:
        lines.append("No confirmed pattern stands behind it.")
    if best["ok"]:
        lines.append(_verdict(b, chosen, best))
    if save:
        try:
            record(symbol, (market.get_state("snapshot") or {}).get("session", {}).get("day")
                   or market.to_et(datetime.now(timezone.utc)).date().isoformat(), p, b["edge"])
        except Exception:
            log.warning("could not record the suggestion", exc_info=True)
    lines.append(ADVICE_NOTE)
    return "\n".join(lines)


ADVICE_NOTE = "My advice, not a licensed adviser's: your call, your risk, your order."


def _exit_text(e: dict, style: str, prefer: str) -> str:
    x = e["exit"]
    managed = (f"Exit: stop {x['stop']:.2f}; half off at {x['t1']:.2f}, stop to "
               f"{e['entry']:.2f}, trail the rest {x['trail']:.2f} behind the best close to "
               f"{x['t2']:.2f}.")
    if style == "day":
        managed += " Out by the close regardless."
    else:
        managed += (f" Not past {x['stale_at']:.2f} in {x['stale_by']} sessions, close it; out "
                    f"after {x['hold']} regardless.")
    if prefer == "fixed":
        return (f"Exit: stop {x['stop']:.2f}, all of it off at {x['t1']:.2f} — my record says "
                f"taking the whole first target has paid better than trailing; out after "
                f"{x['hold']} session{'s' if x['hold'] != 1 else ''} either way.")
    if prefer == "managed":
        managed += " My record says this has paid better than taking it all at once."
    return managed


def _option_exit_text(oc: dict, e: dict) -> str:
    """Exit the option on the stock's levels, with the two ways an option
    loses that the stock doesn't: time and volatility."""
    late = 21 if oc["dte"] >= 30 else max(1, oc["dte"] // 3)
    text = (f"Option exits: follow the stock's stop and targets; out early if it loses half its "
            f"premium; never into its last {late} day{'s' if late != 1 else ''}.")
    if oc.get("spread"):
        text += " Take the spread at 60% of its maximum."
    return text


def _verdict(b: dict, chosen: str, best: dict) -> str:
    if b["direction"] != chosen:
        lean = "leans the other way" if b["direction"] else "doesn't lean either way"
        return (f"My call: I'd pass — the evidence {lean}. If you take it anyway, keep it small "
                f"and keep the stop.")
    if b["edge"]:
        return (f"My call: I'd take it, at the {best['kind']} entry — the trend and a confirmed "
                f"pattern agree.")
    if best["rr"] >= 2.5 and abs(b["score"]) >= 3:
        return (f"My call: take the {best['kind']} at half your normal size — trend and reward "
                f"are good, but it isn't a tested edge yet.")
    return (f"My call: marginal; if you take the {best['kind']}, use a quarter of your normal "
            f"size — it isn't a tested edge.")


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
