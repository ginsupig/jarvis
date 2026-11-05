"""Auditing the bots: weaknesses from trades, the functions behind them, read-only."""
import asyncio
import json
import os
import sqlite3
from pathlib import Path

import bot_audit
import server

DAY = 1793800000.0          # a weekday mid-session, UTC epoch


def trade(bot, i, symbol="", opened=DAY, hold=3600, net=-1.0, gross=None, cost=0.1, bps=-10.0,
          direction="long"):
    return {"bot": bot, "id": f"{bot}-{i}", "symbol": symbol or f"S{i}", "direction": direction,
            "opened": opened + i * 7200, "closed": opened + i * 7200 + hold,
            "gross": net + cost if gross is None else gross, "cost": cost, "net": net,
            "return_bps": bps}


def fleet_db(rows, fills=()):
    path = Path(os.environ["JARVIS_FLEET_DB"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE outcomes (bot, id, strategy, attribution, symbol, direction, opened, "
                  "closed, regime, environment, session, gross, cost, net, basis, return_bps)")
        c.execute("CREATE TABLE fills (bot, id, ts, symbol, side, qty, price, order_id, strategy, "
                  "attribution, context)")
        for r in rows:
            c.execute("INSERT INTO outcomes (bot, id, symbol, direction, opened, closed, gross, cost, "
                      "net, return_bps) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (r["bot"], r["id"], r["symbol"], r["direction"], r["opened"], r["closed"],
                       r["gross"], r["cost"], r["net"], r["return_bps"]))
        for f in fills:
            c.execute("INSERT INTO fills (bot, ts, symbol, side, qty, price) VALUES (?,?,?,?,?,?)", f)


def kinds(found, bot):
    return {w["kind"] for w in found if w["bot"] == bot}


def test_churn_costs_and_a_tail_loss_are_found():
    rows = [trade("quick", i, hold=30) for i in range(3)]
    rows += [trade("costly", i, net=-0.5, gross=0.5, cost=1.0, bps=-5) for i in range(12)]
    rows += [trade("tail", i, net=-1, bps=-20) for i in range(6)] + [trade("tail", 9, net=-50, bps=-400)]
    found = bot_audit.weaknesses(rows, [])
    assert "churn" in kinds(found, "quick")
    assert "costs" in kinds(found, "costly")
    assert "tail_loss" in kinds(found, "tail")
    assert "tail_loss" not in kinds(found, "costly")


def test_the_same_symbol_three_times_a_day_is_re_entry():
    rows = [trade("busy", i, symbol="AAA", hold=600) for i in range(3)]
    for r in rows:                     # all on one ET day
        r["opened"], r["closed"] = DAY + 60 * len(r["id"]), DAY + 60 * len(r["id"]) + 600
    assert "re_entry" in kinds(bot_audit.weaknesses(rows, []), "busy")


def test_a_loss_run_past_where_a_stop_belongs_is_seen_in_the_bars():
    o = trade("loose", 0, symbol="AAA", opened=DAY, hold=3 * 3600, net=-30, bps=-300)
    fill = lambda ts, side, px: {"bot": "loose", "ts": ts, "symbol": "AAA", "side": side, "qty": 10, "price": px}
    fills = [fill(o["opened"], "buy", 100.0), fill(o["closed"], "sell", 97.0)]
    from datetime import datetime, timezone, timedelta
    import market
    t0 = market.to_et(datetime.fromtimestamp(o["opened"], timezone.utc)).replace(tzinfo=None)
    bars = [{"t": (t0 + timedelta(minutes=15 * k)).isoformat(), "h": 100.5, "l": 100 - k, "c": 100 - k}
            for k in range(1, 6)]
    found = bot_audit.weaknesses([o], fills, bars_for=lambda sym: (bars, 2.0))
    assert "past_stop" in kinds(found, "loose")


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


BOT_MAIN = '''
from broker import place
import strategy.rules as rules

def main():
    place("AAA")
'''
BOT_BROKER = '''
import requests
API_KEY = "ABCDEFGHIJKLMNOPQRSTUVWX"

def place(sym):
    try:
        requests.post("https://x/v2/orders", json={"stop_loss": 1})
    except Exception:
        pass

class Client:
    def protect(self, sym, stop_price):
        return self.submit_order(sym, stop_price=stop_price)
'''
DEAD = '''
def protect_old(stop_price):
    return stop_price  # never imported: not on the live path
'''


def make_bot(tmp_path):
    root = Path(os.environ["JARVIS_FLEET_SOURCES"]) / "tail"
    write(root, "main.py", BOT_MAIN)
    write(root, "broker.py", BOT_BROKER)
    write(root, "strategy/__init__.py", "")
    write(root, "strategy/rules.py", "def signal(x):\n    return x > 1\n")
    write(root, "scripts/old_fix.py", DEAD)
    Path(os.environ["JARVIS_FLEET_RUNTIMES"]).write_text(
        json.dumps({"tail": {"preflight": "main.py"}}), encoding="utf-8")
    return root


def test_code_pointers_follow_the_live_imports_and_checks_find_defects(tmp_path):
    root = make_bot(tmp_path)
    live = bot_audit.live_files(root, bot_audit.entry_file("tail", root))
    names = {p.name for p in live}
    assert {"main.py", "broker.py", "rules.py", "__init__.py"} <= names and "old_fix.py" not in names
    code = bot_audit.scan_code(root, ["tail_loss"], live)
    where = [f["where"] for f in code["functions"]["tail_loss"]]
    assert any(w.startswith("broker.py:") for w in where)
    assert not any("old_fix" in w for w in where)
    checks = {c["check"] for c in code["checks"]}
    assert checks == {"swallowed", "no_timeout", "secret"}


def test_describe_names_functions_never_code_or_the_key(tmp_path):
    make_bot(tmp_path)
    rows = [trade("tail", i, net=-1, bps=-20) for i in range(6)] + [trade("tail", 9, net=-50, bps=-400)]
    fleet_db(rows)
    said = bot_audit.describe("tail")
    assert "tail_loss" in said and "Where to look:" in said and "place() at broker.py:" in said
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in said and "requests.post" not in said
    assert "API_KEY at broker.py:" in said
    summary = bot_audit.describe()
    assert summary.splitlines()[1] == "tail: tail_loss"


def test_watch_announces_a_weakness_once(tmp_path):
    fleet_db([trade("quick", i, hold=30) for i in range(3)])
    first = bot_audit.watch("2026-11-04", bars_for=None)
    assert first == ["I see a weakness in quick: 3 round trips closed within two minutes of "
                     "opening. Ask me to audit its code."]
    assert bot_audit.watch("2026-11-05", bars_for=None) == []


def test_the_audit_never_writes_into_a_bot():
    text = Path(bot_audit.__file__).read_text(encoding="utf-8")
    for needle in ("write_text", "write_bytes", '"w"', "'w'", "unlink", "rmtree", "subprocess",
                   "exec(", "eval(", "import_module"):
        assert needle not in text, needle


def test_tool_refuses_a_non_name_and_handles_an_empty_fleet():
    assert asyncio.run(server.tool_bot_audit({"bot": "../etc"})) == "That isn't a bot name I know."
    said = asyncio.run(server.tool_bot_audit({}))
    assert said.startswith("The fleet's completed trades show no weakness")
