"""JARVIS running the paper fleet through the session.

Every TICK_S while the market is open, JARVIS reads the fleet's accounts
(accounts.py) and applies a few fixed rules. Each rule acts through
MegaEmpire's own audited command path (core.fleet_learning.submit_command,
the same one its `fleet_control` tool uses), and only with `pause` and
`resume`:

  day loss     a bot down DAY_LOSS_PCT or more of its equity today has its
               NEW entries paused for the rest of the day
  fleet loss   the fleet down FLEET_LOSS_PCT or more pauses new entries for
               every bot for the rest of the day
  lessons      a bot with a confirmed losing lesson (fleet_study.py) for the
               market JARVIS reads today is paused while that market lasts

A pause stops new entries only: MegaEmpire keeps every protective exit and
stop working, and nothing here places, changes, or cancels an order. Paper
only: the fleet's command path refuses real money, and there is no other
path here.

JARVIS undoes only his own pauses. At the next session (or when a lesson no
longer applies) he resumes a bot he paused, unless the bot's latest command
is no longer his, in which case the user (or the fleet) has decided since
and he leaves it alone. Budget caps are never used: MegaEmpire has no way to
clear one, so a cap would outlive the reason for it.

Env:
  JARVIS_AUTOPILOT                0 to switch it off (default on)
  JARVIS_AUTOPILOT_DAY_LOSS_PCT   default 2.0
  JARVIS_AUTOPILOT_FLEET_LOSS_PCT default 1.5
  JARVIS_AUTOPILOT_ROOT           MegaEmpire's checkout (default C:/MegaEmpire)
  JARVIS_FLEET_WATCH_PYTHON       its interpreter (shared with the fleet watch)
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx

import accounts
import data_paths
import fleet
import market

TICK_S = 300
REASON_PREFIX = "jarvis autopilot: "
SUBMIT = (
    "import json,sys\n"
    "sys.path.insert(0,sys.argv[1])\n"
    "from core.fleet_learning import submit_command\n"
    "print(json.dumps(submit_command(sys.argv[2],sys.argv[3],sys.argv[4])))\n"
)


def enabled() -> bool:
    return os.getenv("JARVIS_AUTOPILOT", "1").strip().lower() not in ("0", "false", "no", "off")


def _pct(name: str, default: float) -> float:
    try:
        return abs(float(os.getenv(name, str(default))))
    except ValueError:
        return default


def day_loss_pct() -> float:
    return _pct("JARVIS_AUTOPILOT_DAY_LOSS_PCT", 2.0)


def fleet_loss_pct() -> float:
    return _pct("JARVIS_AUTOPILOT_FLEET_LOSS_PCT", 1.5)


def root() -> Path:
    return Path(os.getenv("JARVIS_AUTOPILOT_ROOT", "C:/MegaEmpire"))


def state_path() -> Path:
    return data_paths.data_dir() / "autopilot.json"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── the world ───────────────────────────────────────────────────────────────

def market_clock(client: httpx.Client | None = None) -> dict:
    """Alpaca's clock, on the first fleet key that answers: {'is_open', 'day'}."""
    keys = accounts.read_env(accounts.keys_path())
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        for bot in accounts.roster():
            key, secret = keys.get(bot["prefix"] + "_KEY", ""), keys.get(bot["prefix"] + "_SECRET", "")
            if not (key and secret):
                continue
            try:
                r = client.get(accounts.ALPACA_URL + "/v2/clock",
                               headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
                r.raise_for_status()
                body = r.json()
            except (httpx.HTTPError, ValueError):
                continue
            return {"is_open": bool(body.get("is_open")),
                    "day": market.to_et(datetime.now(timezone.utc)).date().isoformat()}
    finally:
        if own:
            client.close()
    return {"is_open": False, "day": "", "unknown": True}


def last_commands() -> dict[str, dict]:
    """Each bot's latest pause/resume in MegaEmpire's command table (read-only)."""
    path = fleet.fleet_db()
    if not path.exists():
        return {}
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT id, action, bot, payload FROM commands "
                              "WHERE action IN ('pause','resume') ORDER BY id").fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for r in rows:
        try:
            reason = str(json.loads(r["payload"] or "{}").get("reason", ""))
        except ValueError:
            reason = ""
        out[str(r["bot"])] = {"id": r["id"], "action": r["action"], "reason": reason}
    return out


def submit(action: str, bot: str, reason: str) -> dict:
    """One audited command through MegaEmpire's own code. Argument list, no shell."""
    if action not in ("pause", "resume"):
        raise ValueError("the autopilot only pauses and resumes")
    if bot != "all" and not fleet.clean(bot):
        raise ValueError("not a bot name")
    python = Path(os.getenv("JARVIS_FLEET_WATCH_PYTHON", fleet.DEFAULT_WATCH_PYTHON))
    if not python.exists() or not (root() / "core" / "fleet_learning.py").exists():
        return {"ok": False, "reason": "MegaEmpire not found"}
    done = subprocess.run([str(python), "-B", "-c", SUBMIT, str(root()), action, bot,
                           REASON_PREFIX + reason],
                          capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        return {"ok": False, "reason": "submit failed"}
    try:
        return dict(json.loads(done.stdout.strip().splitlines()[-1]), ok=True)
    except (ValueError, IndexError):
        return {"ok": False, "reason": "unreadable answer"}


# ── the rules ───────────────────────────────────────────────────────────────

def decide(snap: dict, clock: dict, state: dict, commands: dict, lessons: list[dict],
           trend: str) -> list[dict]:
    """The actions to take now, as [{'action', 'bot', 'reason', 'rule'}]. Pure:
    every input is passed in, so the rules can be tested without a fleet."""
    day = clock.get("day") or ""
    mine = state.get("paused") or {}
    acts: list[dict] = []

    def still_mine(bot: str) -> bool:
        last = commands.get(bot)
        return bool(last) and last["action"] == "pause" and last["reason"].startswith(REASON_PREFIX)

    # Undo yesterday's pauses, and lesson pauses whose market has passed.
    live_lessons = {(l["bot"], l["trend"]) for l in lessons
                    if l["status"] == "confirmed" and l["sign"] < 0}
    for bot, why in mine.items():
        expired = why.get("day") != day if why.get("rule") != "lesson" else \
            (bot, trend) not in live_lessons
        if expired and still_mine(bot):
            acts.append({"action": "resume", "bot": bot, "rule": "undo",
                         "reason": f"{why.get('rule', 'pause')} pause from {why.get('day', '?')} is over"})

    if not clock.get("is_open"):
        return acts
    fleet_rows = [a for a in snap.get("accounts", []) if a["broker"] == "alpaca" and a["status"] == "ok"]
    already = {b for b, w in mine.items() if not any(a["bot"] == b and a["action"] == "resume" for a in acts)}

    total = sum(a.get("equity") or 0 for a in fleet_rows)
    day_pl = sum(a.get("day_pl") or 0 for a in fleet_rows)
    if total and day_pl / total * 100 <= -fleet_loss_pct() and "all" not in already:
        acts.append({"action": "pause", "bot": "all", "rule": "fleet_loss",
                     "reason": f"fleet down {abs(day_pl / total * 100):.2f}% today"})
        return acts
    for a in fleet_rows:
        bot, eq, pl = a["bot"], a.get("equity") or 0, a.get("day_pl") or 0
        if bot in already or not eq:
            continue
        if pl / eq * 100 <= -day_loss_pct():
            acts.append({"action": "pause", "bot": bot, "rule": "day_loss",
                         "reason": f"down {abs(pl / eq * 100):.2f}% today"})
        elif (bot, trend) in live_lessons:
            acts.append({"action": "pause", "bot": bot, "rule": "lesson",
                         "reason": f"confirmed losing lesson in a {trend} market"})
    return acts


# ── one tick ────────────────────────────────────────────────────────────────

_lock = threading.Lock()


def tick(snap: dict | None = None, clock: dict | None = None, lessons: list[dict] | None = None,
         trend: str | None = None, submitter=submit) -> list[str]:
    """Look, decide, act, remember. Returns a sentence per action taken."""
    if not enabled():
        return []
    with _lock:
        clock = clock if clock is not None else market_clock()
        if clock.get("unknown"):
            return []
        if lessons is None:
            import fleet_study
            lessons = fleet_study.lessons()
        if trend is None:
            trend = str(((market.get_state("snapshot") or {}).get("regime") or {}).get("trend") or "unknown")
        snap = snap if snap is not None else (accounts.snapshot() if clock.get("is_open") else {})
        state = load_state()
        acts = decide(snap, clock, state, last_commands(), lessons, trend)
        said, log = [], state.setdefault("log", [])
        for act in acts:
            got = submitter(act["action"], act["bot"], act["reason"])
            who = "every bot" if act["bot"] == "all" else act["bot"].capitalize()
            if not got.get("ok"):
                said.append(f"I tried to {act['action']} {who} ({act['reason']}) but MegaEmpire "
                            f"didn't take it: {got.get('reason', 'unknown')}.")
                continue
            if act["action"] == "pause":
                state.setdefault("paused", {})[act["bot"]] = {
                    "day": clock["day"], "rule": act["rule"], "reason": act["reason"]}
                said.append(f"Paused new entries for {who}: {act['reason']}. Exits and stops keep working.")
            else:
                state.setdefault("paused", {}).pop(act["bot"], None)
                said.append(f"Resumed {who}: {act['reason']}.")
            log.append({"t": time.time(), "day": clock["day"], **act})
        # A pause someone else has since overridden is no longer ours to undo.
        commands = last_commands()
        for bot in list(state.get("paused", {})):
            last = commands.get(bot)
            if last and not (last["action"] == "pause" and last["reason"].startswith(REASON_PREFIX)):
                state["paused"].pop(bot)
        state["log"] = log[-200:]
        state["last_tick"] = time.time()
        state["market_open"] = bool(clock.get("is_open"))
        save_state(state)
        return said


def describe() -> str:
    if not enabled():
        return "The autopilot is switched off (JARVIS_AUTOPILOT=0)."
    state = load_state()
    lines = [f"Autopilot rules: pause a bot's new entries at a {day_loss_pct():g}% day loss, "
             f"every bot at a {fleet_loss_pct():g}% fleet loss, and a bot whose confirmed "
             "losing lesson matches today's market. Exits and stops are never touched."]
    if not state.get("last_tick"):
        lines.append("It hasn't run yet.")
        return "\n".join(lines)
    age = (time.time() - state["last_tick"]) / 60
    lines.append(f"Last check {age:.0f} minutes ago; market {'open' if state.get('market_open') else 'closed'}.")
    paused = state.get("paused") or {}
    if paused:
        lines.append("Paused by me: " + "; ".join(
            f"{'every bot' if b == 'all' else b} ({w.get('reason', '')})" for b, w in paused.items()) + ".")
    else:
        lines.append("I have nothing paused.")
    today = [e for e in state.get("log", []) if time.time() - e.get("t", 0) < 86400]
    for e in today[-6:]:
        who = "every bot" if e["bot"] == "all" else e["bot"]
        lines.append(f"  {e['action']} {who}: {e['reason']}")
    return "\n".join(lines)
