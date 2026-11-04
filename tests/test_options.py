"""The options study: what chains price in, and the forward record of it."""
from contextlib import closing
from datetime import date

import brain
import jarvis_mcp
import market
import options
import server

TODAY = date(2026, 11, 4)


def contract(kind, strike, exp, mid, delta, iv, oi=500, vol=100, spread=0.10):
    return {"symbol": f"X{exp}{kind[0]}{strike}", "type": kind, "strike": strike, "expiration": exp,
            "bid": mid - spread / 2, "ask": mid + spread / 2, "mid": mid, "volume": vol, "oi": oi,
            "delta": delta, "gamma": 0.02, "theta": -0.05, "vega": 0.1, "iv": iv}


class FakeSource:
    name, realtime = "Fake", True

    def __init__(self, spot=100.0):
        self.spot = spot
        self.exps = ["2026-11-05", "2026-11-11", "2026-12-04", "2027-01-02"]

    def quote(self, symbol):
        return self.spot

    def expirations(self, symbol):
        return self.exps

    def chain(self, symbol, exp):
        iv = {"2026-11-05": 0.30, "2026-11-11": 0.25, "2026-12-04": 0.20, "2027-01-02": 0.21}[exp]
        out = []
        for k, cd, pd in ((90, 0.85, -0.15), (95, 0.75, -0.25), (100, 0.52, -0.48),
                          (105, 0.25, -0.75), (110, 0.12, -0.88)):
            out.append(contract("call", k, exp, max(0.2, 100 - k + 3), cd, iv,
                                oi=3000 if k == 110 else 400))
            out.append(contract("put", k, exp, max(0.2, k - 100 + 3), pd, iv + (0.05 if k == 95 else 0),
                                oi=5000 if k == 90 else 400))
        return out


def test_pick_expirations():
    picks = options.pick_expirations(FakeSource().exps, TODAY)
    assert picks == {"front": "2026-11-05", "weekly": "2026-11-11", "m30": "2026-12-04",
                     "m60": "2027-01-02"}


def test_measure_reads_term_move_skew_walls():
    m = options.measure(FakeSource(), "SPY", TODAY, bars={"SPY": []})
    assert m["term"]["m30"]["iv"] == 0.20 and m["term"]["weekly"]["straddle"] == 6.0
    assert abs(m["skew25"] - 0.05) < 1e-9          # 25-delta put (95) 0.25 against call (105) 0.20
    assert m["walls"]["calls"][0]["strike"] == 110 and m["walls"]["puts"][0]["strike"] == 90
    assert m["max_pain"] is not None and m["iv_premium"] is None


def test_record_then_score_the_expected_move():
    options.init_db()
    m = options.measure(FakeSource(), "SPY", TODAY, bars={"SPY": []})
    options.save(m)
    bars = {"SPY": [{"t": "2026-11-11", "c": 104.0}]}
    assert options.score_moves(bars) == 1
    rec = options.track_record("SPY", bars={"SPY": []})
    assert rec["moves"] == 1 and rec["inside"] == 1.0


def test_describe_without_a_source():
    assert "no options data" in options.describe("SPY", src=None).lower() or \
        options.source() is None


def test_describe_says_it_is_new():
    said = options.describe("SPY", src=FakeSource())
    assert said.startswith("SPY at 100.0") and "Expected move to 2026-11-11" in said
    assert "only begun recording" in said


def test_tools_are_wired():
    for name in ("options_study", "trade_plan", "trade_record"):
        assert name in server.TOOL_HANDLERS and name in server.TAINT_EXEMPT_TOOLS
        assert "mcp__jarvis__" + name in brain.ALLOWED_TOOLS
        assert name in {t["name"] for t in jarvis_mcp.TOOL_SPECS}
        assert name not in server.ACTING_TOOLS
    with closing(market._connect()) as conn:
        conn.execute("SELECT 1")
