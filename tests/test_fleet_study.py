"""The after-close study: a day review and lessons tested forward."""
import random
from contextlib import closing
from datetime import datetime, timedelta, timezone

import fleet
import fleet_study
import market


def put_trades(bot, trend, days, per_day, mean_bps, start="2026-09-01", seed=1):
    rng = random.Random(seed)
    fleet_study.init_db()
    d0 = datetime.fromisoformat(start)
    with closing(market._connect()) as conn, conn:
        for i in range(days):
            day = (d0 + timedelta(days=i)).date().isoformat()
            for j in range(per_day):
                bps = mean_bps + rng.uniform(-15, 15)
                closed = datetime.fromisoformat(day + "T19:00:00").replace(tzinfo=timezone.utc).timestamp()
                conn.execute("INSERT INTO fleet_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (bot, f"{bot}-{trend}-{start}-{i}-{j}", "", "SPY", "long", day, closed,
                              bps, bps, trend, "normal"))


def test_no_lesson_without_enough_evidence():
    put_trades("aegis", "down", days=3, per_day=3, mean_bps=-40)
    assert fleet_study.update_lessons("2026-09-10") == []


def test_propose_then_confirm_forward():
    put_trades("aegis", "down", days=8, per_day=3, mean_bps=-40)
    said = fleet_study.update_lessons("2026-09-08")
    assert said and said[0].startswith("New idea to test: aegis loses in a down market")
    assert fleet_study.lessons()[0]["status"] == "testing"
    put_trades("aegis", "down", days=5, per_day=3, mean_bps=-30, start="2026-09-20", seed=2)
    said = fleet_study.update_lessons("2026-09-25")
    assert said[0].startswith("Confirmed: aegis loses in a down market (15 later trades")
    assert fleet_study.lessons()[0]["status"] == "confirmed"


def test_forward_disagreement_retires_it():
    put_trades("ebt", "up", days=8, per_day=3, mean_bps=50)
    fleet_study.update_lessons("2026-09-08")
    put_trades("ebt", "up", days=5, per_day=3, mean_bps=-30, start="2026-09-20", seed=3)
    said = fleet_study.update_lessons("2026-09-25")
    assert said[0].startswith("Retired: ebt wins in a up market")
    assert fleet_study.lessons()[0]["status"] == "retired"


def test_day_review_ranks_bots_and_trades():
    put_trades("aegis", "down", days=1, per_day=2, mean_bps=-40, start="2026-11-02")
    snap = {"accounts": [
        {"broker": "alpaca", "status": "ok", "bot": "aegis", "equity": 100_000, "day_pl": -500, "positions": []},
        {"broker": "alpaca", "status": "ok", "bot": "ebt", "equity": 100_000, "day_pl": 900, "positions": [{}]},
        {"broker": "webull", "status": "ok", "bot": "", "equity": 20_000, "day_pl": -2000, "positions": []},
    ]}
    review = fleet_study.review_day("2026-11-02", snap, [{"day": "2026-11-02", "action": "pause",
                                                          "bot": "aegis", "reason": "down 2% today"}])
    assert set(review["bots"]) == {"aegis", "ebt"}
    assert review["bots"]["aegis"]["closed"] == 2 and len(review["worst"]) == 2
    said = fleet_study.describe_review(review)
    assert said.startswith("Fleet study for 2026-11-02")
    assert "$+400 across 2 bots, 2 trades closed." in said
    assert "Best: ebt $+900 (+0.90%), aegis $-500 (-0.50%)." in said
    assert "Autopilot today: pause aegis (down 2% today)." in said
    assert "Fleet study for 2026-11-02" in fleet_study.describe()


def test_describe_before_any_study():
    said = fleet_study.describe()
    assert "haven't studied a finished session yet" in said and "No lessons yet" in said


def test_backfill_labels_a_missing_day_and_skips_one_without_bars(monkeypatch):
    put_trades("aegis", "unknown", days=2, per_day=1, mean_bps=5, start="2026-10-27")
    seen = []

    def fake_regime(day, bars=None):
        seen.append(day)
        return {"trend": "uptrend", "vol_state": "calm"} if day == "2026-10-27" else {"trend": "unknown"}
    monkeypatch.setattr(fleet_study, "regime_on", fake_regime)
    monkeypatch.setattr(market, "load_bars", lambda tf, syms: {})
    assert fleet_study.backfill_regimes() == 1
    assert seen == ["2026-10-27", "2026-10-28"]
    with closing(market._connect()) as conn:
        rows = [tuple(r) for r in conn.execute("SELECT * FROM fleet_regime_days")]
    assert rows == [("2026-10-27", "uptrend", "calm")]


def test_regime_on_needs_that_days_benchmark_bar():
    assert fleet_study.regime_on("2026-11-02", bars={}) == {"trend": "unknown", "vol_state": "unknown"}
