"""What the user's paper-trading bots have taught JARVIS.

MegaEmpire's fleet brain records every completed paper round trip in its own
SQLite database. This module opens that database read-only, copies each new
trade into JARVIS's own (`fleet_trades`), and labels it with the market regime
JARVIS's market study measured on the day the trade opened. From those rows it
keeps a running verdict per bot, per strategy, and per strategy in each kind
of market -- so over time he can say which strategies hold up, and in what
conditions, from the trades themselves.

It never writes to the fleet, never trades, and never reaches the network.
Everything it says back is a number it computed, a regime word market.py
chose, or a bot/strategy name held to NAME_RE.

It also works the other way. JARVIS publishes his own read of the market
(`export_view`) for MegaEmpire's overwatch, which allocates the fleet's
capital by conditions, and reads back what the overwatch decided
(`overwatch`) so he can say where the capital is and why.

Env:
  JARVIS_FLEET             0 to switch it off (default on)
  JARVIS_FLEET_DB          the fleet brain's database
                           (default C:/MegaEmpire/state/fleet_brain/learning.sqlite3)
  JARVIS_FLEET_ALLOCATION  the overwatch's current decision
                           (default C:/MegaEmpire/state/fleet_brain/allocation.json)
  JARVIS_ENV_EXPORT        where JARVIS writes his market read
                           (default <data dir>/market_environment.json)
  JARVIS_FLEET_WATCH       0 to stop seeing to the fleet (default on)
  JARVIS_FLEET_WATCHDOG    MegaEmpire's watchdog script, and
  JARVIS_FLEET_WATCH_PYTHON  the interpreter that runs it
  JARVIS_FLEET_WATCH_REPORT  its report (default C:/MegaEmpire/state/fleet_watch/report.json)
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import market

DEFAULT_DB = "C:/MegaEmpire/state/fleet_brain/learning.sqlite3"
DEFAULT_ALLOCATION = "C:/MegaEmpire/state/fleet_brain/allocation.json"
TAG_RE = re.compile(r"[a-z]+:[a-z_]{1,24}")
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
UNTAGGED = ("unknown", "unattributed_legacy", "mixed")

# Same bar as the fleet's own promotion rule and its scorecard.
MIN_TRADES = 30
MIN_DAYS = 7


def enabled() -> bool:
    return os.getenv("JARVIS_FLEET", "1").strip().lower() not in ("0", "false", "no", "off")


def fleet_db() -> Path:
    return Path(os.getenv("JARVIS_FLEET_DB", DEFAULT_DB))


def export_path() -> Path:
    raw = os.getenv("JARVIS_ENV_EXPORT")
    if raw:
        return Path(raw)
    import data_paths
    return data_paths.data_dir() / "market_environment.json"


def export_view() -> bool:
    """Write the market study's latest regime for the overwatch. It is
    stamped with when the study ran, so a stale read is ignored there."""
    snap = market.get_state("snapshot") or {}
    regime = snap.get("regime") or {}
    if not regime or not snap.get("as_of"):
        return False
    view = {"updated_epoch": market.parse_utc(snap["as_of"]).timestamp(),
            "schema_version": 2,
            "data_through": snap.get("data_through"),
            "feed_role": "delayed_research",
            "execution_fresh": False,
            "day": (snap.get("session") or {}).get("day"),
            **{k: regime.get(k) for k in ("trend", "vol_state", "breadth_50d", "breadth_200d",
                                           "spy_vs_50d", "spy_vs_200d", "vol_percentile")}}
    path = export_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(view, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return True


def overwatch() -> dict:
    """The overwatch's current decision: how it reads the market and where
    the capital is. Names held to NAME_RE, tags to TAG_RE."""
    path = Path(os.getenv("JARVIS_FLEET_ALLOCATION", DEFAULT_ALLOCATION))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    age = time.time() - float(data.get("updated_epoch", 0))
    tags = [t for t in ((data.get("overwatch") or {}).get("tags") or []) if TAG_RE.fullmatch(str(t))]
    bots = []
    for name, row in (data.get("bots") or {}).items():
        if clean(name) and isinstance(row, dict):
            bots.append({"bot": clean(name), "budget_pct": float(row.get("budget_pct") or 0),
                         "budget": float(row.get("budget_dollars") or 0),
                         "score": float(((row.get("overwatch") or {}).get("evidence") or {})
                                        .get("score_bps") or 0)})
    bots.sort(key=lambda b: -b["budget"])
    return {"age_s": age, "tags": tags, "bots": bots,
            "regime": data.get("regime") or {}, "command_statuses": data.get("command_statuses") or {},
            "fleet_budget": float((data.get("overwatch") or {}).get("fleet_budget_dollars") or 0)}


def clean(name) -> str:
    """A bot or strategy name safe to say back, or '' if it isn't one."""
    text = str(name or "").strip().lower().replace("-", "_")
    return text if NAME_RE.fullmatch(text) else ""


def init_db() -> None:
    market.init_db()
    with closing(market._connect()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fleet_trades (
                bot        TEXT NOT NULL,
                id         TEXT NOT NULL,
                strategy   TEXT NOT NULL,       -- '' when the fleet never knew
                symbol     TEXT NOT NULL,
                direction  TEXT NOT NULL,
                day        TEXT NOT NULL,       -- ET date it opened
                closed     REAL NOT NULL,
                net        REAL NOT NULL,
                return_bps REAL NOT NULL,
                trend      TEXT NOT NULL,       -- market.py's regime that day
                vol_state  TEXT NOT NULL,
                PRIMARY KEY (bot, id)
            );
            CREATE TABLE IF NOT EXISTS fleet_regime_days (
                day       TEXT PRIMARY KEY,
                trend     TEXT NOT NULL,
                vol_state TEXT NOT NULL
            );
        """)


def remember_regime() -> None:
    """Keep the regime of each finished session, so trades can be labelled
    with the market they were made in."""
    snap = market.get_state("snapshot") or {}
    session = snap.get("session") or {}
    regime = snap.get("regime") or {}
    if session.get("state") == "done" and session.get("day"):
        with closing(market._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO fleet_regime_days VALUES (?, ?, ?)",
                         (session["day"], str(regime.get("trend") or "unknown"),
                          str(regime.get("vol_state") or "unknown")))


def _et_day(ts: float) -> str:
    return market.to_et(datetime.fromtimestamp(ts, timezone.utc)).date().isoformat()


def sync() -> dict:
    """Copy the fleet's completed trades in. Rows are replaced, not just
    added: the fleet rebuilds its episodes from fills and may re-tag a trade."""
    init_db()
    remember_regime()
    path = fleet_db()
    if not path.exists():
        return {"ok": False, "reason": f"no fleet database at {path}"}
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)) as src:
        src.row_factory = sqlite3.Row
        columns = {r[1] for r in src.execute("PRAGMA table_info(outcomes)")}
        context_columns = ", regime, environment" if {"regime", "environment"} <= columns else ""
        rows = src.execute("SELECT bot, id, strategy, attribution, symbol, direction, "
                           "opened, closed, net, return_bps" + context_columns + " FROM outcomes").fetchall()
    with closing(market._connect()) as conn, conn:
        regimes = {r["day"]: (r["trend"], r["vol_state"])
                   for r in conn.execute("SELECT * FROM fleet_regime_days")}
        conn.execute("DELETE FROM fleet_trades")
        for r in rows:
            bot = clean(r["bot"])
            if not bot:
                continue
            day = _et_day(r["opened"])
            strategy = "" if r["attribution"] in UNTAGGED else clean(r["strategy"])
            # Completed-session labels include information unavailable at entry.
            # Prefer the broker episode's immutable entry context. For older
            # schemas, use only a strictly earlier completed session.
            prior_days = [d for d in regimes if 0 < (datetime.fromisoformat(day)-datetime.fromisoformat(d)).days <= 4]
            trend, vol = regimes[max(prior_days)] if prior_days else ("unknown", "unknown")
            if context_columns:
                trend = r["regime"] or "unknown"
                vol = r["environment"] or "unknown"
            conn.execute(
                "INSERT INTO fleet_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bot, str(r["id"]), strategy, market.normalise_symbol(r["symbol"] or ""),
                 "short" if r["direction"] == "short" else "long", day, float(r["closed"]),
                 float(r["net"]), float(r["return_bps"]), trend, vol))
    if market.get_state("fleet_label_version") != 2:
        with closing(market._connect()) as conn, conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_lessons'").fetchone()
            if exists:
                conn.execute("UPDATE fleet_lessons SET status='retired' WHERE status!='retired'")
        market.set_state("fleet_label_version", 2)
    market.set_state("fleet_synced", time.time())
    return {"ok": True, "trades": len(rows)}


def grade(rows: list) -> dict:
    """Day-clustered verdict on a set of trades: each day is one observation,
    because trades on the same day share the same market."""
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(float(r["return_bps"]))
    means = [statistics.mean(v) for v in by_day.values()]
    n, days = len(rows), len(means)
    mean_bps = statistics.mean(float(r["return_bps"]) for r in rows) if rows else 0.0
    se = statistics.stdev(means) / math.sqrt(days) if days > 1 else None
    day_mean = statistics.mean(means) if means else 0.0
    lower = day_mean - 1.96 * se if se is not None else None
    upper = day_mean + 1.96 * se if se is not None else None
    if n < MIN_TRADES or days < MIN_DAYS:
        verdict = "collecting"
    elif upper is not None and upper < 0:
        verdict = "losing"
    elif lower is not None and lower > 0:
        verdict = "winning"
    else:
        verdict = "unproven"
    return {"trades": n, "days": days, "net": round(sum(float(r["net"]) for r in rows), 2),
            "wins": sum(float(r["net"]) > 0 for r in rows),
            "mean_bps": round(mean_bps, 1), "verdict": verdict}


def review(bot: str = "") -> dict:
    """Per bot, per strategy, and per strategy by market trend."""
    init_db()
    with closing(market._connect()) as conn:
        rows = conn.execute("SELECT * FROM fleet_trades" + (" WHERE bot = ?" if bot else ""),
                            (bot,) if bot else ()).fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault(("bot", r["bot"]), []).append(r)
        if r["strategy"]:
            groups.setdefault(("strategy", r["bot"], r["strategy"]), []).append(r)
            groups.setdefault(("regime", r["bot"], r["strategy"], r["trend"]), []).append(r)
    out = {"bots": [], "strategies": [], "regimes": [],
           "untagged": sum(not r["strategy"] for r in rows), "total": len(rows)}
    for key, members in groups.items():
        entry = dict(grade(members), bot=key[1])
        if key[0] == "bot":
            out["bots"].append(entry)
        elif key[0] == "strategy":
            out["strategies"].append(dict(entry, strategy=key[2]))
        else:
            out["regimes"].append(dict(entry, strategy=key[2], trend=key[3]))
    for k in ("bots", "strategies", "regimes"):
        out[k].sort(key=lambda e: (-e["trades"], e["bot"]))
    return out


def describe(bot: str = "") -> str:
    """Spoken summary. Only numbers, regime words and NAME_RE names."""
    if not enabled():
        return "Learning from the bots is switched off (JARVIS_FLEET=0)."
    r = review(clean(bot))
    ow = overwatch()
    head = []
    if ow.get("bots") and ow["age_s"] < 600:
        reading = ", ".join(t.replace(":", " ").replace("_", " ") for t in ow["tags"][:4])
        top = [b for b in ow["bots"] if not bot or b["bot"] == clean(bot)][:5]
        head.append(f"Overwatch reads the market as {reading}; fleet budget ${ow['fleet_budget']:,.0f}. "
                    "Capital now: " + ", ".join(f"{b['bot']} {b['budget_pct']:.0f}%" for b in top) + ".")
    elif ow.get("bots"):
        head.append("The overwatch's allocation is stale; the fleet brain may be down.")
    if not r["total"]:
        return "\n".join(head + ["No completed bot trades yet" + (f" for {clean(bot)}" if bot else "")
                                 + ". I'll learn from them as they close."])
    lines = head + [f"{r['total']} completed paper trades so far."]
    for b in r["bots"]:
        lines.append(f"{b['bot']}: {b['trades']} trades over {b['days']} days, net "
                     f"${b['net']:+,.2f}, {b['wins']} winners, {b['mean_bps']:+.1f} bps a trade "
                     f"— {b['verdict']}.")
    for s in r["strategies"][:8]:
        lines.append(f"  {s['bot']} / {s['strategy']}: {s['trades']} trades, "
                     f"{s['mean_bps']:+.1f} bps, {s['verdict']}.")
    split = [g for g in r["regimes"] if g["trend"] != "unknown" and g["trades"] >= 5]
    for g in split[:6]:
        lines.append(f"  {g['strategy']} in a {g['trend']} market: {g['trades']} trades, "
                     f"{g['mean_bps']:+.1f} bps.")
    if r["untagged"]:
        lines.append(f"{r['untagged']} trades have no strategy tag, so they count for the bot only.")
    lines.append(f"A verdict needs {MIN_TRADES} trades over {MIN_DAYS} days; "
                 "until then everything is 'collecting'.")
    return "\n".join(lines)


# ── fleet watch ─────────────────────────────────────────────────────────────
# MegaEmpire's watchdog (scripts/fleet_watchdog.py) checks the supervisor, the
# bots, the overwatch, the accounts' keys and stops, and repairs what it
# safely can: restarting what has died or hung, never touching an order.
# JARVIS runs it every WATCH_EVERY_S, tells the user what it fixed and what
# needs him, and reads its report back on request. The report is codes; every
# sentence here is ours, with names held to NAME_RE and symbols to SYMBOL_RE.

DEFAULT_WATCHDOG = "C:/MegaEmpire/scripts/fleet_watchdog.py"
DEFAULT_WATCH_PYTHON = "C:/MegaEmpire/.audit-venv/Scripts/python.exe"
DEFAULT_WATCH_REPORT = "C:/MegaEmpire/state/fleet_watch/report.json"
WATCH_EVERY_S = 180
CAUSE_RE = re.compile(r"[a-z0-9_.]{1,24}")
TASK_NAMES = {"megaempiretradingresearch": "the trading research task",
              "megaempirefleetdashboard": "the fleet dashboard",
              "revbot": "revbot", "continuumpaper": "the Continuum bot"}
# code -> (what is wrong, what fixing it was). {bot} {symbol} {cause} filled in.
WATCH_SAY = {
    "supervisor_down": ("the fleet supervisor had stopped", "restarted it"),
    "supervisor_hung": ("the fleet supervisor stopped responding", "restarted it"),
    "supervisor_old_code": ("the fleet supervisor was running old code", "restarted it on the current code"),
    "preflight_failed": ("{bot} failed its startup check", "re-ran the check and restarted the fleet"),
    "market_clock_failed": ("the supervisor can't read Alpaca's market clock", ""),
    "bot_crashing": ("{bot} keeps crashing", ""),
    "bot_silent": ("{bot} went silent for {cause}", "restarted it"),
    "brain_stalled": ("the overwatch stopped publishing its allocation", "restarted the fleet supervisor"),
    "brain_errors": ("the fleet brain logged {cause} errors in the last quarter hour", ""),
    "keys_rejected": ("Alpaca is rejecting {bot}'s paper keys", ""),
    "account_blocked": ("{bot}'s paper account is blocked from trading", ""),
    "account_unreachable": ("I couldn't reach {bot}'s paper account", ""),
    "position_unprotected": ("{bot} holds {symbol} with no broker stop", ""),
    "task_stopped": ("{bot} had stopped", "started it again"),
    "task_failed": ("{bot} failed its last run, most likely on its keys", ""),
    "jarvis_down": ("I was down", "restarted myself"),
    "jarvis_old_code": ("I'm running older code than what's on disk", "restarted myself"),
    "disk_low": ("the disk is down to {cause} free", ""),
}
CAUSE_SAY = {"smart_app_control": "Windows Smart App Control is blocking its libraries; "
                                  "the supervisor retries every five minutes",
             "keys": "Alpaca is rejecting its keys", "crash": "it is raising an error",
             "network": "the network is failing"}


def watch_enabled() -> bool:
    return enabled() and os.getenv("JARVIS_FLEET_WATCH", "1").strip().lower() not in ("0", "false", "no", "off")


def _watch_report_path() -> Path:
    return Path(os.getenv("JARVIS_FLEET_WATCH_REPORT", DEFAULT_WATCH_REPORT))


def run_watch(fix: bool = True) -> dict:
    """Run the watchdog once (repairing, unless fix=False) and return its
    report. A run already in progress elsewhere just leaves the last report."""
    script = Path(os.getenv("JARVIS_FLEET_WATCHDOG", DEFAULT_WATCHDOG))
    python = Path(os.getenv("JARVIS_FLEET_WATCH_PYTHON", DEFAULT_WATCH_PYTHON))
    if not (script.exists() and python.exists()):
        return {}
    import subprocess
    subprocess.run([str(python), "-B", str(script), *(["--fix"] if fix else []), "--by", "jarvis", "--quiet"],
                   cwd=script.parent.parent, capture_output=True, timeout=1500,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return watch_report()


def _held(row: dict) -> dict:
    """One report row, cut down to what JARVIS may say."""
    bot = clean(row.get("bot"))
    symbol = str(row.get("symbol") or "")
    cause = str(row.get("cause") or "")
    return {"code": row.get("code"), "bot": TASK_NAMES.get(bot, bot),
            "symbol": symbol if market.SYMBOL_RE.match(symbol) else "",
            "cause": cause if CAUSE_RE.fullmatch(cause) else "",
            **{k: bool(row.get(k)) for k in ("fixed", "needs_you", "fix_held")},
            "severity": row.get("severity") if row.get("severity") in ("info", "warn", "critical") else "warn",
            "planned": bool(row.get("fix"))}


def _held_action(a: dict) -> dict:
    return dict(_held({"code": a.get("for"), "bot": a.get("bot")}), ok=bool(a.get("ok")),
                epoch=float(a.get("epoch") or 0))


def watch_report() -> dict:
    """The watchdog's last report: codes, held names, and flags."""
    try:
        data = json.loads(_watch_report_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {"age_s": time.time() - float(data.get("updated_epoch") or 0),
            "ok": bool(data.get("ok")), "market_open": bool(data.get("market_open")),
            "issues": [_held(i) for i in data.get("issues") or [] if i.get("code") in WATCH_SAY],
            "actions": [_held_action(a) for a in data.get("actions") or [] if a.get("for") in WATCH_SAY],
            "recent_actions": [_held_action(a) for a in data.get("recent_actions") or []
                               if a.get("for") in WATCH_SAY]}


def _sentence(i: dict) -> str:
    problem, repair = WATCH_SAY[i["code"]]
    cause = i["cause"].replace("min", " minutes").replace("GB", " gigabytes") if i["cause"] else "a while"
    text = problem.format(bot=i["bot"] or "a bot", symbol=i["symbol"] or "a position", cause=cause)
    if i["code"] == "bot_crashing" and i["cause"] in CAUSE_SAY:
        text += f": {CAUSE_SAY[i['cause']]}"
    if i["fixed"] and repair:
        text += f"; I {repair}"
    elif i["fix_held"]:
        text += "; I've already made that repair as often as I'm allowed today, so it needs you"
    elif i["needs_you"]:
        text += " — that needs you"
    elif i["planned"]:
        text += "; I'll repair it as soon as that is safe"
    return text[0].upper() + text[1:] + "."


def health() -> str:
    """Spoken state of the fleet, from the watchdog's last report."""
    if not watch_enabled():
        return "The fleet watch is switched off (JARVIS_FLEET_WATCH=0)."
    r = watch_report()
    if not r:
        return "The fleet watch hasn't reported yet."
    lines = []
    if r["age_s"] > 3 * WATCH_EVERY_S:
        lines.append(f"The fleet watch last reported {r['age_s'] / 60:.0f} minutes ago, "
                     "so this may be out of date.")
    shown = [i for i in r["issues"] if i["severity"] != "info" or i["planned"]]
    if not shown:
        lines.append("All clear: the supervisor, the overwatch, every bot and every account check out.")
    lines += [_sentence(i) for i in shown]
    day = [a for a in r["recent_actions"] if time.time() - a["epoch"] < 86400]
    if day:
        done = []
        for a in day[-5:]:
            when = market.to_et(datetime.fromtimestamp(a["epoch"], timezone.utc))
            what = WATCH_SAY[a["code"]][0].format(bot=a["bot"] or "a bot", symbol="a position", cause="a while")
            done.append(f"at {when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'} ET "
                        f"{what}, so I {WATCH_SAY[a['code']][1] or 'made a repair'}"
                        + ("" if a["ok"] else ", but it didn't take"))
        lines.append("In the last day: " + "; ".join(done) + ".")
    return "\n".join(lines)


def watch_news(previous: dict, current: dict) -> list[str]:
    """What to tell the user after a watch run: each repair just made, and
    each problem that newly needs him. What he was already told stays quiet."""
    out = [_sentence(dict(a, fixed=a["ok"], needs_you=not a["ok"], fix_held=False, planned=False))
           for a in current.get("actions") or []]
    told = {(i["code"], i["bot"], i["symbol"]) for i in previous.get("issues") or [] if i["needs_you"]}
    out += [_sentence(i) for i in current.get("issues") or []
            if i["needs_you"] and (i["code"], i["bot"], i["symbol"]) not in told]
    return list(dict.fromkeys(out))
