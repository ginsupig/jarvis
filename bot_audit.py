"""JARVIS auditing the trading bots' code when their trades show a weakness.

    evidence  from the fleet's completed trades and fills (MegaEmpire's
              learning database, opened read-only), per bot:
                churn      round trips closed within two minutes of opening
                re_entry   the same symbol entered three or more times a day
                costs      commissions and slippage eating most of the gross
                tail_loss  a loss several times the bot's usual loss
                past_stop  a trade that ran more than 1.5 ATR against the
                           entry before it was closed (stored bars only)
                gave_back  a trade a full ATR in profit that closed at a loss
                losing     the fleet's own verdict on the bot: losing
    code      for each weakness, the functions in the running copy of that
              bot (MegaEmpire runtime/sources/<bot>) that decide it — stop
              placement for a tail loss, re-entry guards for churn, sizing
              and frequency for costs — found by parsing the code (ast),
              never by running it. Paths and function names only: no code
              text leaves this module
    checks    three defects that are wrong anywhere on an order path: an
              exception swallowed in a function that places orders, an HTTP
              call there with no timeout, and a key written into the code
              (its location only, never its value)
    watch     after each close the evidence is re-run; a weakness not seen
              before is recorded (`bot_audits`) and announced once

Nothing here writes to a bot's files, runs its code or touches an order.
Reading the functions and proposing a fix is the brain's job, with
`read_file`; changing them is the user's call.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import statistics
import time
import warnings
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import fleet
import market

DEFAULT_SOURCES = "C:/MegaEmpire/runtime/sources"
DEFAULT_RUNTIMES = "C:/MegaEmpire/config/paper_runtimes.json"
SKIP_DIRS = {".venv", "venv", "env", "site-packages", "node_modules", "__pycache__", ".git",
             "tests", "test", "data", "logs", "backtest", "backtests", "archive", "notebooks",
             "build", "dist"}
MAX_FILE = 600_000
CHURN_S = 120
RE_ENTRY_N = 3
MIN_TRADES = 10                 # before costs or tails are judged
TAIL_X = 4.0                    # a loss this many times the median loss
PAST_STOP_ATR = 1.5
GAVE_BACK_ATR = 1.0

ORDER_RE = re.compile(r"submit_order|place_order|create_order|/v2/orders|replace_order")
HTTP_RE = re.compile(r"(requests|httpx)\.(get|post|put|patch|delete|request)")
SECRET_RE = re.compile(r"(?i)api_?key|secret|token|password")
KEYLIKE_RE = re.compile(r"[A-Za-z0-9/+_-]{20,}")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,80}")
PATH_RE = re.compile(r"[A-Za-z0-9_./-]{1,200}")

# What decides each weakness, by the words its code is written in.
TOPICS = {
    "tail_loss": r"stop_?loss|stop_?price|protect|bracket|oco|max_?loss|risk_?per",
    "past_stop": r"stop_?loss|stop_?price|protect|bracket|oco|reprotect|time_in_force",
    "gave_back": r"take_?profit|target|trail|break_?even|scale_?out|partial",
    "churn":     r"cooldown|re_?entry|min_?hold|already|duplicate|last_?(trade|entry|order)",
    "re_entry":  r"cooldown|re_?entry|max_?trades|already|duplicate|last_?(trade|entry|order)",
    "costs":     r"qty|position_?size|notional|max_?trades|interval|slippage|commission|min_?edge",
    "losing":    r"signal|score|threshold|confidence|entry_?(rule|condition)|should_?(enter|buy)",
}
WHAT = {
    "churn": "{n} round trips closed within two minutes of opening",
    "re_entry": "{n} day(s) with the same symbol entered three or more times",
    "costs": "costs of ${cost:.0f} against ${gross:+.0f} gross over {trades} trades",
    "tail_loss": "a loss of {worst:.0f} bps, {x:.0f} times its usual {median:.0f} bps loss",
    "past_stop": "{n} trades that ran more than 1.5 ATR against the entry before closing",
    "gave_back": "{n} trades a full ATR in profit that closed at a loss",
    "losing": "the fleet grades it losing over {trades} trades and {days} days",
}
FIX_HINT = {
    "churn": "look for a missing cooldown or a position check that lets an exit re-trigger an entry",
    "re_entry": "look for a missing per-symbol daily limit or cooldown after a stop-out",
    "costs": "trade less often, size up only with edge, or use limit orders",
    "tail_loss": "check the stop is placed at the broker, survives overnight, and is honoured",
    "past_stop": "check the stop is placed at the broker, survives overnight, and is honoured",
    "gave_back": "check for a take-profit or trail that locks in part of a winner",
    "losing": "the entry rule itself is the suspect; its signal and threshold are where to look",
}


def sources_root() -> Path:
    return Path(os.getenv("JARVIS_FLEET_SOURCES", DEFAULT_SOURCES))


def bot_dirs() -> dict[str, Path]:
    root = sources_root()
    try:
        return {fleet.clean(d.name): d for d in sorted(root.iterdir())
                if d.is_dir() and fleet.clean(d.name) == d.name}
    except OSError:
        return {}


# ── evidence ────────────────────────────────────────────────────────────────

def load_fleet() -> tuple[list[dict], list[dict]]:
    path = fleet.fleet_db()
    if not path.exists():
        return [], []
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)) as src:
        src.row_factory = sqlite3.Row
        outs = [dict(r) for r in src.execute(
            "SELECT bot, id, symbol, direction, opened, closed, gross, cost, net, return_bps "
            "FROM outcomes")]
        fills = [dict(r) for r in src.execute("SELECT bot, ts, symbol, side, qty, price FROM fills")]
    return outs, fills


def _et_day(ts: float) -> str:
    return market.to_et(datetime.fromtimestamp(ts, timezone.utc)).date().isoformat()


def _prices(o: dict, fills: list[dict]) -> tuple[float, float] | None:
    """Average entry and exit price of one round trip, from its fills."""
    opening = "buy" if o["direction"] != "short" else "sell"
    fs = [f for f in fills if f["bot"] == o["bot"] and f["symbol"] == o["symbol"]
          and o["opened"] - 5 <= f["ts"] <= o["closed"] + 5 and f["qty"]]
    ins = [f for f in fs if str(f["side"]).lower().startswith(opening)]
    outs = [f for f in fs if not str(f["side"]).lower().startswith(opening)]
    if not ins or not outs:
        return None
    avg = lambda xs: sum(f["price"] * f["qty"] for f in xs) / sum(f["qty"] for f in xs)
    return avg(ins), avg(outs)


def _excursions(o: dict, entry: float, bars: list, atr: float) -> tuple[float, float]:
    """Best and worst move in ATRs while the trade was open, from 15-minute bars."""
    t0 = market.to_et(datetime.fromtimestamp(o["opened"], timezone.utc)).replace(tzinfo=None)
    t1 = market.to_et(datetime.fromtimestamp(o["closed"], timezone.utc)).replace(tzinfo=None)
    side = -1 if o["direction"] == "short" else 1
    best = worst = 0.0
    for b in bars:
        try:
            t = datetime.fromisoformat(str(b["t"])[:19])
        except ValueError:
            continue
        if t0 <= t <= t1:
            hi, lo = (b["h"], b["l"]) if side == 1 else (b["l"], b["h"])
            best = max(best, side * (hi - entry) / atr)
            worst = min(worst, side * (lo - entry) / atr)
    return best, worst


def weaknesses(outs: list[dict], fills: list[dict], bars_for=None) -> list[dict]:
    """Every weakness the trades show, per bot. `bars_for(symbol)` returns
    (15-minute bars, daily ATR) or None; without it the excursion checks skip."""
    by_bot: dict[str, list[dict]] = {}
    for o in outs:
        bot = fleet.clean(o["bot"])
        if bot:
            by_bot.setdefault(bot, []).append(o)
    found = []
    for bot, rows in sorted(by_bot.items()):
        add = lambda kind, **kw: found.append({"bot": bot, "kind": kind, "detail": WHAT[kind].format(**kw),
                                               "facts": kw})
        quick = [o for o in rows if o["closed"] - o["opened"] <= CHURN_S]
        if len(quick) >= 2:
            add("churn", n=len(quick))
        per_day: dict[tuple, int] = {}
        for o in rows:
            k = (o["symbol"], _et_day(o["opened"]))
            per_day[k] = per_day.get(k, 0) + 1
        busy = sum(1 for v in per_day.values() if v >= RE_ENTRY_N)
        if busy:
            add("re_entry", n=busy)
        gross = sum(float(o["gross"] or 0) for o in rows)
        cost = sum(float(o["cost"] or 0) for o in rows)
        net = sum(float(o["net"] or 0) for o in rows)
        if len(rows) >= MIN_TRADES and net < 0 and cost >= 0.5 * abs(gross):
            add("costs", cost=cost, gross=gross, trades=len(rows))
        losses = sorted(float(o["return_bps"]) for o in rows if float(o["net"] or 0) < 0)
        if len(losses) >= 5:
            median = statistics.median(losses)
            if median < 0 and losses[0] <= TAIL_X * median and losses[0] <= -100:
                add("tail_loss", worst=losses[0], median=median, x=losses[0] / median)
        verdict = fleet.grade([{"day": _et_day(o["opened"]), "return_bps": o["return_bps"],
                                "net": o["net"]} for o in rows])
        if verdict["verdict"] == "losing":
            add("losing", trades=verdict["trades"], days=verdict["days"])
        if bars_for is None:
            continue
        past = gave = 0
        for o in rows:
            px = _prices(o, fills)
            got = bars_for(market.normalise_symbol(o["symbol"] or "")) if px else None
            if not got or not got[1]:
                continue
            best, worst = _excursions(o, px[0], got[0], got[1])
            past += worst <= -PAST_STOP_ATR
            gave += best >= GAVE_BACK_ATR and float(o["net"] or 0) < 0
        if past:
            add("past_stop", n=past)
        if gave:
            add("gave_back", n=gave)
    return found


def stored_bars(symbol: str):
    """The study's own 15-minute bars and daily ATR, when it follows the symbol."""
    if not market.SYMBOL_RE.fullmatch(symbol):
        return None
    intraday = market.load_bars("15Min", [symbol]).get(symbol) or []
    daily = market.load_bars("1Day", [symbol]).get(symbol) or []
    if not intraday or len(daily) < 20:
        return None
    s = market.Series.build(symbol, daily)
    return intraday, s.atr14[-1]


# ── code ────────────────────────────────────────────────────────────────────

def runtimes_path() -> Path:
    return Path(os.getenv("JARVIS_FLEET_RUNTIMES", DEFAULT_RUNTIMES))


def entry_file(bot: str, root: Path) -> Path | None:
    """The file the fleet launches the bot from (its runtime's `preflight`)."""
    try:
        cfg = json.loads(runtimes_path().read_text(encoding="utf-8")).get(bot) or {}
    except (OSError, ValueError):
        return None
    rel = str(cfg.get("preflight") or "")
    path = (root / rel).resolve() if rel.endswith(".py") else None
    try:
        return path if path and path.is_file() and path.is_relative_to(root.resolve()) else None
    except OSError:
        return None


def _module_file(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.is_file():
            return p
    return None


def live_files(root: Path, entry: Path) -> set[Path]:
    """The entry file and every file under the bot's folder it imports,
    directly or through other files there: the code that actually runs."""
    root = root.resolve()
    seen, todo = set(), [entry.resolve()]
    while todo and len(seen) < 2000:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        tree = _parse(path)
        if tree is None:
            continue
        pkg = path.parent.relative_to(root).parts
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    up = pkg[:len(pkg) - node.level + 1] if node.level <= len(pkg) + 1 else ()
                    base = ".".join(up + ((node.module,) if node.module else ()))
                else:
                    base = node.module or ""
                names = [base] + [f"{base}.{a.name}" if base else a.name for a in node.names]
            for name in names:
                parts = name.split(".")
                # the file itself, then each parent package's __init__
                for i in range(len(parts), 0, -1):
                    p = _module_file(root, ".".join(parts[:i]))
                    if p and p.resolve() not in seen:
                        todo.append(p.resolve())
    return seen


def _parse(path: Path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None


def _py_files(root: Path):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(".py") and not fn.startswith("test_") and not fn.endswith("_test.py"):
                p = Path(dp) / fn
                try:
                    if p.stat().st_size <= MAX_FILE:
                        yield p
                except OSError:
                    continue


def _functions(path: Path):
    """(qualified name, first line, source text) for each function in a file."""
    tree = _parse(path)
    if tree is None:
        return
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                end = getattr(child, "end_lineno", child.lineno)
                yield name, child, "\n".join(lines[child.lineno - 1:end])
                yield from walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
    yield from walk(tree, "")


def _say_path(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return rel if PATH_RE.fullmatch(rel) else ""


def _call_name(node: ast.Call) -> str:
    parts, f = [], node.func
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def scan_code(root: Path, kinds: list[str], only: set[Path] | None = None) -> dict:
    """The functions behind each weakness kind, and the order-path checks,
    in the files `only` names (the code that runs) when it is given."""
    topics = {k: re.compile(TOPICS[k], re.I) for k in kinds if k in TOPICS}
    hits: dict[str, list[tuple[int, str, int]]] = {k: [] for k in topics}
    checks: list[dict] = []
    for path in _py_files(root):
        if only and path.resolve() not in only:
            continue
        rel = _say_path(path, root)
        if not rel:
            continue
        for name, node, src in _functions(path):
            if not IDENT_RE.fullmatch(name):
                continue
            where = f"{rel}:{node.lineno}"
            for k, rx in topics.items():
                n = len(rx.findall(src)) + 3 * bool(rx.search(name))
                if n:
                    hits[k].append((n, where, name))
            if not ORDER_RE.search(src):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler) and all(
                        isinstance(s, (ast.Pass, ast.Continue)) for s in sub.body):
                    caught = sub.type is None or (isinstance(sub.type, ast.Name)
                                                  and sub.type.id in ("Exception", "BaseException"))
                    if caught:
                        checks.append({"check": "swallowed", "where": f"{rel}:{sub.lineno}", "function": name})
                if isinstance(sub, ast.Call) and HTTP_RE.fullmatch(_call_name(sub)) \
                        and not any(kw.arg == "timeout" for kw in sub.keywords):
                    checks.append({"check": "no_timeout", "where": f"{rel}:{sub.lineno}", "function": name})
        tree = _parse(path)
        for sub in ast.walk(tree) if tree is not None else ():
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Constant) \
                    and isinstance(sub.value.value, str) and KEYLIKE_RE.fullmatch(sub.value.value):
                for t in sub.targets:
                    if isinstance(t, ast.Name) and SECRET_RE.search(t.id):
                        checks.append({"check": "secret", "where": f"{rel}:{sub.lineno}", "function": t.id})
    functions = {k: [{"where": w, "function": f} for _, w, f in sorted(v, key=lambda x: -x[0])[:5]]
                 for k, v in hits.items()}
    return {"functions": functions, "checks": checks}


# ── the audit ───────────────────────────────────────────────────────────────

def audit(bot: str = "", bars_for=stored_bars) -> dict:
    outs, fills = load_fleet()
    if bot:
        outs = [o for o in outs if fleet.clean(o["bot"]) == bot]
    found = weaknesses(outs, fills, bars_for)
    dirs = bot_dirs()
    bots = sorted({w["bot"] for w in found} | ({bot} if bot else set()))
    out = {}
    for b in bots:
        ws = [w for w in found if w["bot"] == b]
        code = None
        if b in dirs:
            entry = entry_file(b, dirs[b])
            only = live_files(dirs[b], entry) if entry else None
            code = dict(scan_code(dirs[b], [w["kind"] for w in ws], only), live_only=bool(only))
        out[b] = {"weaknesses": ws, "code": code, "trades": sum(fleet.clean(o["bot"]) == b for o in outs)}
    return out


CHECK_WORDS = {
    "swallowed": "an exception swallowed silently in an order function",
    "no_timeout": "an HTTP call with no timeout in an order function (it can hang the bot)",
    "secret": "what looks like a key written into the code (rotate it and move it to .env)",
}


def describe(bot: str = "") -> str:
    bot = fleet.clean(bot)
    try:
        got = audit(bot)
    except Exception as e:
        return f"I couldn't audit the bots just now ({type(e).__name__})."
    if not got:
        return ("The fleet's completed trades show no weakness I check for right now"
                + (f" in {bot}." if bot else "."))
    if not bot:
        # The whole fleet in one breath; the detail is one bot at a time.
        lines = [f"{b}: " + ", ".join(w["kind"] for w in r["weaknesses"]) for b, r in got.items()
                 if r["weaknesses"]]
        return ("Weaknesses in the fleet's completed trades —\n" + "\n".join(lines)
                + "\nAsk me to audit one bot for the evidence and the functions to look at.")
    r = got[bot]
    if not r["weaknesses"]:
        return f"{bot}: no weakness in its {r['trades']} completed trades."
    lines = [f"{bot}, from its {r['trades']} completed trades:"]
    for w in r["weaknesses"]:
        lines.append(f"- {w['kind']}: {w['detail']}; {FIX_HINT[w['kind']]}.")
    if r["code"] is None:
        lines.append(f"I can't find {bot}'s running code under {sources_root().as_posix()}.")
        return "\n".join(lines)
    # The functions most weaknesses point at come first.
    tally: dict[tuple, int] = {}
    for w in r["weaknesses"]:
        for f in r["code"]["functions"].get(w["kind"], [])[:3]:
            key = (f["function"], f["where"])
            tally[key] = tally.get(key, 0) + 1
    if tally:
        top = sorted(tally, key=lambda k: -tally[k])[:5]
        lines.append("Where to look: " + "; ".join(f"{fn}() at {where}" for fn, where in top) + ".")
    by: dict[str, list[dict]] = {}
    for c in r["code"]["checks"]:
        by.setdefault(c["check"], []).append(c)
    for kind, cs in by.items():
        lines.append(f"Also {len(cs)} × {CHECK_WORDS[kind]}: "
                     + "; ".join(f"{c['function']} at {c['where']}" for c in cs[:2]) + ".")
    lines.append(f"Paths are under {sources_root().as_posix()}/{bot}, the copy the fleet runs"
                 + ("" if r["code"].get("live_only") else ", all of it (I couldn't trace its imports)")
                 + ". Read the functions before saying what's wrong.")
    return "\n".join(lines)


# ── watch ───────────────────────────────────────────────────────────────────

def init_db() -> None:
    market.init_db()
    with closing(market._connect()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS bot_audits (
                            bot    TEXT NOT NULL,
                            kind   TEXT NOT NULL,
                            first  TEXT NOT NULL,      -- ET day it was first seen
                            last   TEXT NOT NULL,
                            detail TEXT NOT NULL,
                            PRIMARY KEY (bot, kind))""")


def watch(day: str, bars_for=stored_bars) -> list[str]:
    """After a close: record every weakness and return a line for each new one."""
    init_db()
    outs, fills = load_fleet()
    found = weaknesses(outs, fills, bars_for)
    new = []
    with closing(market._connect()) as conn, conn:
        seen = {(r["bot"], r["kind"]) for r in conn.execute("SELECT bot, kind FROM bot_audits")}
        for w in found:
            if (w["bot"], w["kind"]) not in seen:
                new.append(f"I see a weakness in {w['bot']}: {w['detail']}. Ask me to audit its code.")
                conn.execute("INSERT INTO bot_audits VALUES (?, ?, ?, ?, ?)",
                             (w["bot"], w["kind"], day, day, w["detail"]))
            else:
                conn.execute("UPDATE bot_audits SET last=?, detail=? WHERE bot=? AND kind=?",
                             (day, w["detail"], w["bot"], w["kind"]))
    market.set_state("bot_audit_watched", {"day": day, "at": time.time(), "found": len(found)})
    return new
