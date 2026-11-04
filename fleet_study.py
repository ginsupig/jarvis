"""After the close: what the fleet did today, and what it is teaching.

Once per finished session JARVIS studies the day:

  review    every bot's day (account change, trades closed, winners and
            losers), the day's best and worst trades, the market he read,
            and what his autopilot did; kept per day in fleet_reviews
  lessons   claims of the form "BOT wins (or loses) in a TREND market",
            found in the fleet's completed trades and then TESTED FORWARD:

    propose   a (bot, trend) group whose trades so far, clustered by day,
              have |t| >= PROPOSE_T over at least PROPOSE_TRADES trades and
              PROPOSE_DAYS days. A guess, not yet a lesson.
    test      only trades opened AFTER the proposal count from then on.
    confirm   CONFIRM_TRADES forward trades over CONFIRM_DAYS days with a
              mean of the same sign and forward t >= CONFIRM_T.
    retire    forward mean of the opposite sign once there are enough
              forward trades; a confirmed lesson is retired the same way the
              moment later trades stop agreeing with it.

A confirmed losing lesson is what autopilot.py acts on (it pauses that bot's
new entries while that market lasts). Nothing is learned from open profit,
from the same trades twice, or from a model's opinion: every decision here
is a count or a t-statistic.

Env:
  JARVIS_FLEET_STUDY   0 to switch it off (default on)
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from contextlib import closing
from datetime import datetime, timezone

import accounts
import fleet
import market

PROPOSE_T, PROPOSE_TRADES, PROPOSE_DAYS = 2.5, 15, 5
CONFIRM_T, CONFIRM_TRADES, CONFIRM_DAYS = 1.0, 10, 3


def enabled() -> bool:
    return os.getenv("JARVIS_FLEET_STUDY", "1").strip().lower() not in ("0", "false", "no", "off")


def init_db() -> None:
    fleet.init_db()
    with closing(market._connect()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fleet_lessons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bot         TEXT NOT NULL,
                trend       TEXT NOT NULL,
                sign        INTEGER NOT NULL,      -- +1 wins, -1 loses
                created_day TEXT NOT NULL,
                evidence    TEXT NOT NULL,         -- the proposal's stats
                forward     TEXT NOT NULL DEFAULT '{}',
                status      TEXT NOT NULL,         -- testing|confirmed|retired
                updated     REAL NOT NULL,
                UNIQUE (bot, trend, created_day)
            );
            CREATE TABLE IF NOT EXISTS fleet_reviews (
                day    TEXT PRIMARY KEY,
                review TEXT NOT NULL
            );
        """)


def stats(rows: list) -> dict:
    """Day-clustered: each day one observation, as fleet.grade does."""
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(float(r["return_bps"]))
    means = [statistics.mean(v) for v in by_day.values()]
    n, days = len(rows), len(means)
    mean = statistics.mean(means) if means else 0.0
    sd = statistics.stdev(means) if days > 1 else 0.0
    t = mean / (sd / math.sqrt(days)) if days > 1 and sd > 0 else 0.0
    return {"trades": n, "days": days, "mean_bps": round(mean, 1), "t": round(t, 2)}


def _trades() -> list:
    with closing(market._connect()) as conn:
        return conn.execute("SELECT * FROM fleet_trades WHERE trend != 'unknown'").fetchall()


def update_lessons(today: str) -> list[str]:
    """Propose, test, confirm, retire. Returns a sentence per change."""
    init_db()
    rows = _trades()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["bot"], r["trend"]), []).append(r)
    changes = []
    with closing(market._connect()) as conn, conn:
        existing = conn.execute("SELECT * FROM fleet_lessons").fetchall()
        open_keys = {(l["bot"], l["trend"]) for l in existing if l["status"] != "retired"}
        for l in existing:
            if l["status"] == "retired":
                continue
            fwd_rows = [r for r in groups.get((l["bot"], l["trend"]), []) if r["day"] > l["created_day"]]
            fwd = stats(fwd_rows)
            status = l["status"]
            enough = fwd["trades"] >= CONFIRM_TRADES and fwd["days"] >= CONFIRM_DAYS
            if enough and fwd["mean_bps"] * l["sign"] < 0:
                status = "retired"
            elif enough and status == "testing" and fwd["t"] * l["sign"] >= CONFIRM_T:
                status = "confirmed"
            if status != l["status"]:
                verb = "wins" if l["sign"] > 0 else "loses"
                changes.append(
                    f"{'Confirmed' if status == 'confirmed' else 'Retired'}: {l['bot']} {verb} "
                    f"in a {l['trend']} market ({fwd['trades']} later trades, "
                    f"{fwd['mean_bps']:+.1f} bps, t {fwd['t']}).")
            conn.execute("UPDATE fleet_lessons SET forward = ?, status = ?, updated = ? WHERE id = ?",
                         (json.dumps(fwd), status, time.time(), l["id"]))
        for (bot, trend), members in groups.items():
            if (bot, trend) in open_keys:
                continue
            past = [r for r in members if r["day"] <= today]
            s = stats(past)
            if s["trades"] < PROPOSE_TRADES or s["days"] < PROPOSE_DAYS or abs(s["t"]) < PROPOSE_T:
                continue
            sign = 1 if s["t"] > 0 else -1
            conn.execute("INSERT OR IGNORE INTO fleet_lessons (bot, trend, sign, created_day, evidence, "
                         "status, updated) VALUES (?, ?, ?, ?, ?, 'testing', ?)",
                         (bot, trend, sign, today, json.dumps(s), time.time()))
            changes.append(f"New idea to test: {bot} {'wins' if sign > 0 else 'loses'} in a {trend} "
                           f"market ({s['trades']} trades over {s['days']} days, "
                           f"{s['mean_bps']:+.1f} bps, t {s['t']}).")
    return changes


def lessons() -> list[dict]:
    init_db()
    with closing(market._connect()) as conn:
        out = []
        for l in conn.execute("SELECT * FROM fleet_lessons ORDER BY id"):
            out.append({"id": l["id"], "bot": l["bot"], "trend": l["trend"], "sign": l["sign"],
                        "created_day": l["created_day"], "status": l["status"],
                        "evidence": json.loads(l["evidence"]), "forward": json.loads(l["forward"])})
        return out


def _closed_on(r, day: str) -> bool:
    return market.to_et(datetime.fromtimestamp(float(r["closed"]), timezone.utc)).date().isoformat() == day


def review_day(day: str, snap: dict | None = None, autopilot_log: list | None = None) -> dict:
    """The day's numbers, stored and returned."""
    init_db()
    with closing(market._connect()) as conn:
        all_rows = conn.execute("SELECT * FROM fleet_trades").fetchall()
    closed = [r for r in all_rows if _closed_on(r, day)]
    snap = snap if snap is not None else accounts.snapshot()
    regime = (market.get_state("snapshot") or {}).get("regime") or {}
    bots = {}
    for a in snap.get("accounts", []):
        if a["broker"] != "alpaca" or a["status"] != "ok":
            continue
        mine = [r for r in closed if r["bot"] == a["bot"]]
        bots[a["bot"]] = {"day_pl": a.get("day_pl"), "equity": a.get("equity"),
                          "open": len(a["positions"]), "closed": len(mine),
                          "realized": round(sum(float(r["net"]) for r in mine), 2),
                          "wins": sum(float(r["net"]) > 0 for r in mine)}
    ranked = sorted(closed, key=lambda r: float(r["net"]))

    def trade(r):
        return {"bot": r["bot"], "symbol": r["symbol"], "strategy": r["strategy"],
                "direction": r["direction"], "net": round(float(r["net"]), 2),
                "return_bps": round(float(r["return_bps"]), 1)}
    review = {"day": day, "trend": str(regime.get("trend") or "unknown"),
              "vol_state": str(regime.get("vol_state") or "unknown"),
              "bots": bots, "best": [trade(r) for r in ranked[::-1][:3] if float(r["net"]) > 0],
              "worst": [trade(r) for r in ranked[:3] if float(r["net"]) < 0],
              "autopilot": [e for e in (autopilot_log or []) if e.get("day") == day]}
    with closing(market._connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO fleet_reviews VALUES (?, ?)", (day, json.dumps(review)))
    return review


def regime_on(day: str, bars: dict | None = None) -> dict:
    """The regime market.py would have read at that day's close, rebuilt from
    the daily bars it already stores (each series cut at `day`)."""
    bars = bars if bars is not None else market.load_bars("1Day", market.universe())
    series = {}
    for sym, rows in bars.items():
        kept = [r for r in rows if r["t"] <= day]
        if kept:
            series[sym] = market.Series.build(sym, kept)
    spy = series.get(market.BENCHMARK)
    if spy is None or not spy.days or spy.days[-1] != day:
        return {"trend": "unknown", "vol_state": "unknown"}
    spy_ret20 = market._ratio(spy.c[-1], spy.c[-21]) if len(spy) > 21 else None
    views = {sym: market.symbol_view(s_, None, spy_ret20, s_.days[-1] == day)
             for sym, s_ in series.items() if len(s_) >= 2}
    return market.regime_view(series, views)


def backfill_regimes() -> int:
    """Label trade days the market study missed (it was down 10-24 to 11-01)."""
    fleet.init_db()
    with closing(market._connect()) as conn:
        have = {r["day"] for r in conn.execute("SELECT day FROM fleet_regime_days")}
        wanted = sorted({r["day"] for r in conn.execute("SELECT DISTINCT day FROM fleet_trades")} - have)
    if not wanted:
        return 0
    bars = market.load_bars("1Day", market.universe())
    added = 0
    with closing(market._connect()) as conn, conn:
        for day in wanted:
            r = regime_on(day, bars)
            if r.get("trend", "unknown") == "unknown":
                continue
            conn.execute("INSERT OR IGNORE INTO fleet_regime_days VALUES (?, ?, ?)",
                         (day, str(r["trend"]), str(r.get("vol_state") or "unknown")))
            added += 1
    return added


def study(day: str, snap: dict | None = None, autopilot_log: list | None = None) -> str:
    """The whole after-close pass. Returns what to post."""
    fleet.sync()
    if backfill_regimes():
        fleet.sync()
    review = review_day(day, snap, autopilot_log)
    changes = update_lessons(day)
    market.set_state("fleet_studied", day)
    return describe_review(review, changes)


def _bot_line(name: str, b: dict) -> str:
    pl = b["day_pl"]
    pct = f" ({pl / b['equity'] * 100:+.2f}%)" if pl is not None and b["equity"] else ""
    pl_s = "?" if pl is None else f"${pl:+,.0f}"
    return f"{name} {pl_s}{pct}"


def describe_review(review: dict, changes: list[str] | None = None) -> str:
    bots = review["bots"]
    ordered = sorted(bots.items(), key=lambda kv: kv[1]["day_pl"] or 0, reverse=True)
    total = sum(b["day_pl"] or 0 for b in bots.values())
    lines = [f"Fleet study for {review['day']} ({review['trend']} market, {review['vol_state']} "
             f"volatility): ${total:+,.0f} across {len(bots)} bots, "
             f"{sum(b['closed'] for b in bots.values())} trades closed."]
    if ordered:
        lines.append("Best: " + ", ".join(_bot_line(n, b) for n, b in ordered[:3]) + ".")
        lines.append("Worst: " + ", ".join(_bot_line(n, b) for n, b in ordered[::-1][:3]) + ".")
    for label, key in (("Best trades", "best"), ("Worst trades", "worst")):
        if review[key]:
            lines.append(f"{label}: " + "; ".join(
                f"{t['bot']} {t['symbol']} {t['direction']} ${t['net']:+,.2f} ({t['return_bps']:+.0f} bps)"
                for t in review[key]) + ".")
    if review["autopilot"]:
        lines.append("Autopilot today: " + "; ".join(
            f"{e['action']} {e['bot']} ({e['reason']})" for e in review["autopilot"]) + ".")
    lines.extend(changes or [])
    return "\n".join(lines)


def describe(day: str = "") -> str:
    """The latest (or a given day's) review plus the lesson book."""
    if not enabled():
        return "The fleet study is switched off (JARVIS_FLEET_STUDY=0)."
    init_db()
    with closing(market._connect()) as conn:
        row = conn.execute("SELECT review FROM fleet_reviews WHERE day = ?", (day,)).fetchone() if day \
            else conn.execute("SELECT review FROM fleet_reviews ORDER BY day DESC LIMIT 1").fetchone()
    lines = [describe_review(json.loads(row["review"])) if row else
             "I haven't studied a finished session yet; I do after each close."]
    book = lessons()
    live = [l for l in book if l["status"] != "retired"]
    if live:
        lines.append("Lessons:")
        for l in live:
            fwd = l["forward"]
            lines.append(f"  {l['status']}: {l['bot']} {'wins' if l['sign'] > 0 else 'loses'} in a "
                         f"{l['trend']} market — first seen {l['created_day']} "
                         f"(t {l['evidence']['t']}), since then {fwd.get('trades', 0)} trades, "
                         f"{fwd.get('mean_bps', 0):+.1f} bps.")
    retired = len(book) - len(live)
    lines.append(f"{retired} retired. A lesson needs {CONFIRM_TRADES} later trades over "
                 f"{CONFIRM_DAYS} days that agree with it before I act on it."
                 if book else "No lessons yet: a bot needs "
                 f"{PROPOSE_TRADES} completed trades over {PROPOSE_DAYS} days in one kind of market first.")
    return "\n".join(lines)
