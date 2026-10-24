"""market.py: the arithmetic, the verdicts, and one full cycle against a fake
Alpaca. Nothing here reaches the network."""

import asyncio
import random
from datetime import date, datetime, timedelta, timezone

import pytest

import market


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JARVIS_MARKET_WATCHLIST", raising=False)
    market.init_db()
    return tmp_path


# ── time ────────────────────────────────────────────────────────────────────

def test_eastern_time_follows_the_us_daylight_rule():
    utc = timezone.utc
    assert market.et_offset(datetime(2026, 3, 8, 6, 59, tzinfo=utc)) == timedelta(hours=-5)
    assert market.et_offset(datetime(2026, 3, 8, 7, 0, tzinfo=utc)) == timedelta(hours=-4)
    assert market.et_offset(datetime(2026, 11, 1, 5, 59, tzinfo=utc)) == timedelta(hours=-4)
    assert market.et_offset(datetime(2026, 11, 1, 6, 0, tzinfo=utc)) == timedelta(hours=-5)
    assert market.to_et(datetime(2026, 10, 23, 13, 30, tzinfo=utc)) == datetime(2026, 10, 23, 9, 30)
    assert market.to_et(datetime(2026, 12, 1, 14, 30, tzinfo=utc)) == datetime(2026, 12, 1, 9, 30)


def test_market_phase_reads_the_calendar():
    sessions = market.sessions_from_calendar([
        {"date": "2026-10-22", "open": "09:30", "close": "16:00"},
        {"date": "2026-10-23", "open": "09:30", "close": "13:00"},     # an early close
        {"date": "2026-10-26", "open": "09:30", "close": "16:00"},
    ])
    phase, current, upcoming = market.market_phase(sessions, datetime(2026, 10, 23, 12, 0))
    assert (phase, current.day) == ("open", "2026-10-23")
    phase, current, upcoming = market.market_phase(sessions, datetime(2026, 10, 23, 14, 0))
    assert (phase, current.day, upcoming.day) == ("closed", "2026-10-23", "2026-10-26")


# ── indicators ──────────────────────────────────────────────────────────────

def test_sma_rsi_and_atr():
    xs = [float(x) for x in range(1, 11)]
    assert market.sma(xs, 3)[:3] == [None, None, 2.0]
    assert market.sma(xs, 3)[-1] == 9.0
    assert market.rsi(xs, 2)[-1] == 100.0                      # never a down day
    assert market.rsi(list(reversed(xs)), 2)[-1] == 0.0
    flat = [10.0] * 20
    assert market.atr([11.0] * 20, [9.0] * 20, flat, 14)[-1] == pytest.approx(2.0)


def test_relative_volume_compares_with_the_days_before():
    v = [100.0] * 20 + [300.0]
    assert market.relative_volume(v, 20)[-1] == pytest.approx(3.0)


# ── the test and the verdict ────────────────────────────────────────────────

def _walk(symbol: str, days: list[str], seed: int, drift=0.0003, vol=0.01, plant=None):
    rng = random.Random(seed)
    rows, price = [], 100.0
    boost = 0
    for i, d in enumerate(days):
        o = price
        r = rng.gauss(drift, vol) + (0.006 if boost > 0 else 0.0)
        boost -= 1
        c = max(1.0, o * (1 + r))
        rows.append({"t": d, "o": o, "h": max(o, c) * 1.002, "l": min(o, c) * 0.998,
                     "c": c, "v": 1e6})
        price = c
        if plant is not None and plant(i):
            boost = 5
    return market.Series.build(symbol, rows)


def _days(n: int) -> list[str]:
    d, out = date(2019, 1, 1), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_a_planted_edge_is_confirmed_and_noise_is_not():
    days = _days(1300)
    every = lambda i: i % 12 == 0
    planted = {f"S{k}": _walk(f"S{k}", days, k, plant=every) for k in range(4)}
    noise = {f"S{k}": _walk(f"S{k}", days, 100 + k) for k in range(4)}
    pattern = market.Pattern("every_12", "x", "x", +1, 5, lambda s, i: every(i))
    calendar = {d: i for i, d in enumerate(days)}

    result = market.evaluate(pattern, planted, market.split_point(planted), calendar)
    verdict, _ = market.judge(result["discovery"], result["holdout"], market.Stats())
    assert verdict == market.CONFIRMED
    assert result["discovery"].mean > 0.01            # 3% planted, less the baseline and cost

    result = market.evaluate(pattern, noise, market.split_point(noise), calendar)
    verdict, _ = market.judge(result["discovery"], result["holdout"], market.Stats())
    assert verdict in (market.REJECTED, market.TESTING)


def test_same_day_events_count_once_and_overlaps_are_dropped():
    calendar = {d: i for i, d in enumerate(_days(30))}
    days = sorted(calendar)
    obs = {days[0]: 1.0, days[2]: 1.0, days[5]: 1.0, days[6]: 1.0}
    assert market._thin(obs, calendar, 5) == [1.0, 1.0]       # days 0 and 5


def test_judge_thresholds():
    s = market.Stats
    assert market.judge(s(10, 0.01, 5, 0.6), s(), s())[0] == market.TESTING
    assert market.judge(s(50, 0.01, 2.0, 0.6), s(30, 0.01, 3, 0.6), s())[0] == market.REJECTED
    assert market.judge(s(50, 0.01, 3.0, 0.6), s(30, -0.01, 3, 0.4), s())[0] == market.REJECTED
    assert market.judge(s(50, 0.01, 3.0, 0.6), s(30, 0.01, 2, 0.6), s())[0] == market.CONFIRMED
    retired = market.judge(s(50, 0.01, 3.0, 0.6), s(30, 0.01, 2, 0.6), s(12, -0.002, -1, 0.4))
    assert retired[0] == market.RETIRED


def test_every_pattern_runs_on_real_shaped_data():
    s = _walk("X", _days(400), 7, vol=0.02)
    for p in market.PATTERNS:
        for i in range(len(s)):
            p.fires(s, i)                       # never raises on early or missing values


# ── the watchlist ───────────────────────────────────────────────────────────

def test_watchlist_is_seeded_once_then_the_users(data_dir, monkeypatch):
    monkeypatch.setenv("JARVIS_MARKET_WATCHLIST", "nvda, $amd, not a ticker")
    assert market.watchlist() == ["NVDA", "AMD"]
    result = market.change_watchlist(["tsla", "NVDA", "bad!"], ["amd"])
    assert result["added"] == ["TSLA"] and result["removed"] == ["AMD"]
    assert result["refused"] == ["bad!"]
    market.change_watchlist([], ["NVDA", "TSLA"])
    assert market.watchlist() == []                         # empty stays empty


# ── one full cycle ──────────────────────────────────────────────────────────

class FakeAlpaca:
    """Weekday sessions, 9:30-16:00 ET in October (EDT), bars from a seeded walk."""

    def __init__(self):
        self.requests = []

    def calendar(self, start, end):
        out, d = [], start
        while d <= end:
            if d.weekday() < 5:
                out.append({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
            d += timedelta(days=1)
        return out

    def bars(self, symbols, tf, start, end):
        self.requests.append((tuple(symbols), tf))
        out = {}
        for sym in symbols:
            rng = random.Random(sym)
            price, rows = 100.0, []
            d = date(2021, 9, 1)
            while d <= end.date():
                if d.weekday() < 5:
                    r = rng.gauss(0.0004, 0.012)
                    o, c = price, price * (1 + r)
                    if tf == "1Day":
                        t = datetime(d.year, d.month, d.day, 4, tzinfo=timezone.utc)
                        if start <= t <= end:
                            rows.append({"t": t.isoformat().replace("+00:00", "Z"), "o": o,
                                         "h": max(o, c) * 1.004, "l": min(o, c) * 0.996,
                                         "c": c, "v": rng.uniform(5e5, 2e6)})
                    else:
                        for k in range(26):
                            t = datetime(d.year, d.month, d.day, 13, 30, tzinfo=timezone.utc) \
                                + timedelta(minutes=15 * k)
                            if start <= t <= end:
                                p = o + (c - o) * (k + 1) / 26
                                rows.append({"t": t.isoformat().replace("+00:00", "Z"), "o": p,
                                             "h": p * 1.001, "l": p * 0.999, "c": p, "v": 4e4})
                    price = c
                d += timedelta(days=1)
            if rows:
                out[sym] = rows
        return out


def test_a_cycle_after_the_close_studies_and_notifies_once(data_dir, monkeypatch):
    monkeypatch.setenv("JARVIS_MARKET_WATCHLIST", "AAPL,NVDA")
    fake = FakeAlpaca()
    now = {"t": datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)}     # 17:00 ET Thursday
    sent = []

    async def notify(line, subtitle):
        sent.append((line, subtitle))

    engine = market.MarketEngine(client_factory=lambda: fake, notify=notify,
                                 clock=lambda: now["t"])

    async def go():
        await engine.tick()
        first = engine.status()
        assert first["studied_day"] == "2026-10-22"
        assert sent == []                         # the first pass is a backfill, not news
        snap = first["snapshot"]
        assert snap["session"] == {"day": "2026-10-22", "state": "done"}
        assert [v["symbol"] for v in snap["watchlist"]] == ["AAPL", "NVDA"]
        assert len(snap["sectors"]) == len(market.SECTORS)
        assert {p["status"] for p in snap["patterns"]} <= {
            market.TESTING, market.CONFIRMED, market.REJECTED, market.RETIRED}
        assert all(p["reason"] for p in snap["patterns"])
        assert snap["regime"]["trend"] in ("uptrend", "downtrend", "mixed")
        assert snap["brief"] and snap["headline"]

        await engine.tick()                       # same evening: nothing new to do
        assert sent == []

        now["t"] = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)   # 11:00 ET Friday
        sleep = await engine.tick()
        assert sleep == market.interval_minutes() * 60
        assert engine.status()["snapshot"]["session"] == {"day": "2026-10-23", "state": "open"}

        now["t"] = datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc)  # 16:30 ET Friday
        await engine.tick()
        assert len(sent) == 1 and sent[0][1] == "Market close"
        assert sent[0][0].startswith("2026-10-23")
        await engine.tick(force=True)
        assert len(sent) == 1                     # once per day, however often it runs

    asyncio.run(go())


def test_signals_are_recorded_and_scored_once_their_horizon_passes(data_dir):
    fake = FakeAlpaca()
    engine = market.MarketEngine(client_factory=lambda: fake,
                                 clock=lambda: datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc))
    asyncio.run(engine.tick())
    series = engine._series(market.Session("2026-10-22", datetime.max, datetime.max), True)
    with market.closing(market._connect()) as conn, conn:
        conn.execute("INSERT INTO market_signals (pattern, symbol, day, direction, horizon) "
                     "VALUES ('breakout_20d', 'SPY', ?, 1, 2)", (series["SPY"].days[-5],))
    engine._resolve_signals(series)
    with market.closing(market._connect()) as conn:
        row = conn.execute("SELECT result, resolved FROM market_signals "
                           "WHERE pattern = 'breakout_20d' AND symbol = 'SPY' "
                           "AND day = ?", (series["SPY"].days[-5],)).fetchone()
    assert row["result"] is not None and row["resolved"] == series["SPY"].days[-3]


def test_no_keys_means_no_requests(data_dir, monkeypatch):
    engine = market.MarketEngine(client_factory=lambda: None)
    assert asyncio.run(engine.tick()) == 30 * 60.0
    assert engine.status()["phase"] == "unconfigured"


def test_the_client_cannot_trade():
    public = {n for n in dir(market.AlpacaData) if not n.startswith("_")}
    assert public == {"DATA_URL", "TRADING_URL", "bars", "calendar"}


# ── the voice tool ──────────────────────────────────────────────────────────

def test_the_brief_tool_says_what_is_missing(data_dir):
    import server
    said = asyncio.run(server.tool_market_brief({}))
    assert "APCA_API_KEY_ID" in said


def test_the_brief_tool_reads_a_study(data_dir, monkeypatch):
    import server
    fake = FakeAlpaca()
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    monkeypatch.setenv("JARVIS_MARKET_WATCHLIST", "AAPL")
    engine = market.MarketEngine(client_factory=lambda: fake,
                                 clock=lambda: datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(server, "market_engine", engine)
    asyncio.run(engine.tick())
    whole = asyncio.run(server.tool_market_brief({}))
    assert "Patterns:" in whole
    one = asyncio.run(server.tool_market_brief({"symbol": "aapl"}))
    assert one.startswith("AAPL at ")
    assert "don't follow" in asyncio.run(server.tool_market_brief({"symbol": "ZZZZ"}))
    said = asyncio.run(server.tool_market_watchlist({"add": ["msft"], "remove": ["AAPL"]}))
    assert "Added MSFT" in said and "dropped AAPL" in said
