"""Suggested trades: levels, entries, exits, the option, and the scored record."""
import asyncio
import math

import market
import server
import trade_plan
from tests.test_options import FakeSource


def rows(n=260, start=50.0, drift=0.2, wave=3.0):
    out = []
    for i in range(n):
        c = start + drift * i + wave * math.sin(i / 6)
        out.append({"t": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" if i < 12 * 28 else f"2027-x{i}",
                    "o": c - 0.3, "h": c + 1.0, "l": c - 1.0, "c": c, "v": 1e6})
    return out


def series(**kw):
    return market.Series.build("TEST", rows(**kw))


def test_levels_split_around_the_price():
    s = series()
    lv = trade_plan.levels(s, s.c[-1])
    assert lv["support"] and lv["resistance"]
    assert all(z["px"] < s.c[-1] for z in lv["support"])
    assert all(z["px"] > s.c[-1] for z in lv["resistance"])
    assert lv["support"] == sorted(lv["support"], key=lambda z: -z["px"])


def test_uptrend_leans_long_and_downtrend_short():
    up, down = series(drift=0.2), series(start=150, drift=-0.2)
    assert trade_plan.bias(up, up.c[-1], "uptrend", [])["direction"] == "long"
    assert trade_plan.bias(down, down.c[-1], "downtrend", [])["direction"] == "short"
    flat = series(drift=0.0, wave=0.0)
    assert trade_plan.bias(flat, flat.c[-1], "mixed", [])["direction"] == ""


def test_plan_has_stop_behind_and_targets_ahead():
    s = series()
    p = trade_plan.plan(s, s.c[-1], "long", "swing", risk_dollars=500)
    for e in p["entries"]:
        assert e["stop"] < e["entry"] < e["targets"][0] < e["targets"][1]
        assert e["ok"] == (e["rr"] >= trade_plan.MIN_RR)
        assert e["shares"] == int(500 // (e["entry"] - e["stop"])) or abs(
            e["shares"] - 500 / (e["entry"] - e["stop"])) <= 1
    short = trade_plan.plan(s, s.c[-1], "short", "day")
    for e in short["entries"]:
        assert e["stop"] > e["entry"] > e["targets"][0]


def test_option_choice_picks_delta_and_prices_the_plan():
    src = FakeSource(spot=100.0)
    import options
    from datetime import date
    opt = options.measure(src, "TEST", date(2026, 11, 4), bars={"TEST": []})
    p = {"style": "swing", "direction": "long", "hold": 10,
         "best": {"entry": 100.0, "stop": 97.0, "targets": [106.0, 109.0]}}
    oc = trade_plan.option_choice(src, "TEST", p, opt, date(2026, 11, 4))
    assert oc["expiration"] == "2026-12-04" and oc["type"] == "call" and oc["strike"] == 100
    assert oc["at_stop"] < oc["at_entry"] < oc["at_t1"] and oc["risk_per"] > 0


def test_resolve_won_lost_unfilled_and_closed():
    base = {"day": "2026-11-02", "direction": "long", "entry": 100.0, "stop": 98.0,
            "target": 104.0, "hold": 3, "kind": "pullback"}
    bar = lambda t, o, h, l, c: {"t": t, "o": o, "h": h, "l": l, "c": c}
    won = trade_plan.resolve_idea(base, [bar("2026-11-03", 101, 101, 99.5, 100.5),
                                         bar("2026-11-04", 101, 104.5, 100.5, 104)])
    assert won == {"status": "won", "filled": "2026-11-03", "result_r": 2.0}
    lost = trade_plan.resolve_idea(base, [bar("2026-11-03", 101, 105, 97, 99)])
    assert lost["status"] == "lost"                 # touched both: the stop counts
    never = trade_plan.resolve_idea(base, [bar(f"2026-11-0{d}", 102, 103, 101, 102) for d in (3, 4, 5)])
    assert never == {"status": "unfilled"}
    closed = trade_plan.resolve_idea(base, [bar(f"2026-11-0{d}", 100, 101, 99.5, 101) for d in (3, 4, 5)])
    assert closed == {"status": "closed", "filled": "2026-11-03", "result_r": 0.5}
    assert trade_plan.resolve_idea(base, []) is None


def test_record_score_and_describe():
    s = series()
    p = trade_plan.plan(s, s.c[-1], "long", "swing")
    p["entries"] = [dict(p["entries"][0], ok=True, kind="market", entry=100.0, stop=98.0,
                         targets=[104.0, 106.0])]
    trade_plan.record("TEST", "2026-11-02", p, edge=False)
    bars = [{"t": "2026-11-03", "o": 100, "h": 104.5, "l": 99, "c": 104}]
    assert trade_plan.score_ideas(load=lambda sym: bars) == 1
    said = trade_plan.describe_record()
    assert "I've suggested 1 entries: 1 finished" in said and "100% made money" in said
    assert "too few" in said


def test_record_starts_empty():
    assert trade_plan.describe_record().startswith("I haven't suggested any trades yet")


def test_tool_refuses_a_non_ticker_and_scans_without_one():
    said = asyncio.run(server.tool_trade_plan({"symbol": "rm -rf /"}))
    assert said == "That isn't a ticker I can plan for."
    said = asyncio.run(server.tool_trade_plan({}))
    assert "Nothing on my list" in said


def test_a_farther_entry_is_less_likely_to_fill_and_the_record_can_outrank_reward():
    near, far = {"kind": "pullback", "entry": 99.0}, {"kind": "pullback", "entry": 90.0}
    assert trade_plan.fill_chance({"kind": "market", "entry": 100.0}, 100.0, 10.0) == 1.0
    assert trade_plan.fill_chance(near, 100.0, 10.0) > trade_plan.fill_chance(far, 100.0, 10.0) >= 0.33
    s = series()
    p = trade_plan.plan(s, s.c[-1], "long", "swing")
    for e in p["entries"]:
        assert e["score"] == round(e["fill"] * e["rr"], 3) and e["exit"]["t1"] == e["targets"][0]
    kinds = [e["kind"] for e in p["entries"]]
    if len(kinds) > 1:
        learned = {k: (5.0 if k == kinds[-1] else -1.0) for k in kinds}
        q = trade_plan.plan(s, s.c[-1], "long", "swing", learned=learned)
        assert q["best"]["kind"] == kinds[-1] or not q["entries"][-1]["ok"]


def test_managed_exit_banks_half_then_trails_or_stalls():
    idea = {"day": "2026-11-02", "direction": "long", "entry": 100.0, "stop": 98.0, "target": 104.0,
            "target2": 108.0, "atr": 1.0, "hold": 6, "kind": "pullback", "style": "swing"}
    bar = lambda t, o, h, l, c: {"t": t, "o": o, "h": h, "l": l, "c": c}
    # half at 104 (+2R on half = +1R), the rest at 108 (+4R on half = +2R): +3R
    run = [bar("2026-11-03", 100, 101, 99.5, 100.5), bar("2026-11-04", 101, 104.5, 100.5, 104),
           bar("2026-11-05", 104, 108.5, 103.5, 108)]
    assert trade_plan.resolve_managed(idea, "2026-11-03", run) == 3.0
    # half at 104 (+1R), then the trail (best close 104 - 2 ATR = 102) takes the rest (+0.5R)
    back = run[:2] + [bar("2026-11-05", 103, 103.5, 101.5, 102)]
    assert trade_plan.resolve_managed(idea, "2026-11-03", back) == 1.5
    # never makes half an R by session 3 of 6: out at that close
    stall = [bar(f"2026-11-0{d}", 100, 100.6, 99.5, 100.4) for d in (3, 4, 5)]
    assert trade_plan.resolve_managed(idea, "2026-11-03", stall) == 0.2
    # straight to the stop: the whole position, -1R
    assert trade_plan.resolve_managed(idea, "2026-11-03", [bar("2026-11-03", 100, 100.5, 97, 98)]) == -1.0
    assert trade_plan.resolve_managed(dict(idea, target2=None), "2026-11-03", run) is None


def test_both_exits_are_scored_and_the_better_one_is_preferred_after_twenty():
    s = series()
    p = trade_plan.plan(s, s.c[-1], "long", "swing")
    bars = [{"t": "2026-11-03", "o": 100, "h": 101, "l": 99.5, "c": 100.5},
            {"t": "2026-11-04", "o": 101, "h": 104.5, "l": 100.5, "c": 104},
            {"t": "2026-11-05", "o": 104, "h": 108.5, "l": 103.5, "c": 108}]
    for i in range(trade_plan.LEARN_MIN):
        p["entries"] = [dict(p["entries"][0], ok=True, kind="market", entry=100.0, stop=98.0,
                             targets=[104.0, 108.0])]
        p["atr"] = 1.0
        trade_plan.record(f"T{i}", "2026-11-02", p, edge=False)
    trade_plan.score_ideas(load=lambda sym: bars)
    t = trade_plan.track_record()
    assert t["exits"]["n"] == trade_plan.LEARN_MIN and t["exits"]["managed"] > t["exits"]["fixed"]
    assert trade_plan.exit_preference() == "managed"
    assert "managed exit" in trade_plan.describe_record()


def test_the_verdict_never_says_full_size_without_an_edge():
    best = {"kind": "pullback", "rr": 3.0}
    strong = {"direction": "long", "edge": False, "score": 4}
    assert "half your normal size" in trade_plan._verdict(strong, "long", best)
    assert "I'd pass" in trade_plan._verdict(strong, "short", best)
    assert "either way" in trade_plan._verdict({"direction": "", "edge": False, "score": 0}, "long", best)
    assert "I'd take it" in trade_plan._verdict(dict(strong, edge=True), "long", best)
    assert "not a licensed adviser" in trade_plan.ADVICE_NOTE
