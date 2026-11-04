"""JARVIS studying the market: trends, movement, and patterns he keeps re-testing.

    collect   every few minutes while the market is open, and once after the
              close, the bars for the market universe (index and sector ETFs,
              bonds, gold, a volatility ETF) and the user's watchlist are
              pulled from Alpaca's market-data API into `market_bars`
    analyse   the trend and volatility regime, breadth, sector leadership,
              and for every symbol its trend, momentum, volatility, relative
              volume and how unusual today's move is. Unusual things become
              ALERTS, once per symbol, kind and day
    test      a fixed library of PATTERNS (an oversold pullback in an uptrend,
              a 20-day breakout, a high-volume flush, ...) is measured over
              five years of daily bars: what followed each one, net of costs,
              against what followed an ordinary day for the same symbol
    learn     after every close each pattern is re-tested, every pattern that
              fired that day is recorded as a forward SIGNAL, and signals are
              scored once their horizon has passed. A pattern's status follows
              the evidence, and only the evidence

How a pattern earns "confirmed", because that word is what the user hears:

  - The history is split: the older two thirds DISCOVER, the newest third is
    a HOLDOUT the discovery never saw.
  - A return is measured from the next session's open (the signal is only
    known at the close) to the close `horizon` sessions later, as excess over
    that symbol's own average return over the same horizon in the same
    period — a long pattern in a bull market must beat simply being long —
    minus a round-trip cost.
  - Events on the same day are averaged into one observation (fifteen stocks
    oversold on the same morning are one bet on the market, not fifteen), and
    observations closer together than the horizon are dropped, so the
    t-statistic is not inflated by overlapping windows.
  - Discovery has to clear t >= 2.5, not 2: the library tests a dozen ideas,
    and at 2.0 one of them would pass by luck alone.
  - The holdout then has to agree (positive, t >= 1.5). Anything else is
    "rejected", which is the expected result for most price patterns.
  - A confirmed pattern whose forward signals since tracking began lose money
    on balance is "retired". Forward signals are the only truly
    out-of-sample record, and they outrank the backtest.

Everything here is arithmetic. No model writes or judges a pattern: the
library is code, and every status is a threshold on counts. The brain reads
the result and says it in his own words.

JARVIS studies; he never trades. Nothing in this module can place an order —
the client below knows two read-only endpoints and nothing else.

Data is the free Alpaca plan's consolidated (SIP) feed, which is delayed
fifteen minutes, so every request ends sixteen minutes ago.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import httpx

from data_paths import db_path

log = logging.getLogger("jarvis.market")

# ── universe ────────────────────────────────────────────────────────────────

# What the regime is read from. Alpaca has no index data, so the VIX is
# watched through VIXY (short-term VIX futures), which is direction, not level.
MARKET_SYMBOLS = {
    "SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "DIA": "Dow 30",
    "TLT": "Long Treasuries", "GLD": "Gold", "VIXY": "VIX futures",
}
SECTORS = {
    "XLK": "Technology", "XLC": "Communication", "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples", "XLF": "Financials", "XLV": "Health Care",
    "XLI": "Industrials", "XLE": "Energy", "XLB": "Materials",
    "XLU": "Utilities", "XLRE": "Real Estate",
}
DEFAULT_WATCHLIST = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
                     "AVGO", "AMD", "JPM", "XOM", "LLY", "COST", "NFLX", "PLTR")
BENCHMARK = "SPY"
WATCHLIST_MAX = 60
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

# ── data window ─────────────────────────────────────────────────────────────

DATA_DELAY_MIN = 16             # the free SIP feed is 15 minutes behind
HISTORY_YEARS = 5
INTRADAY_KEEP_DAYS = 45         # calendar days of 15-minute bars kept
INTRADAY_LOOKBACK = 20          # sessions the time-of-day volume is averaged over
DEFAULT_INTERVAL_MIN = 15

# ── the test ────────────────────────────────────────────────────────────────

COST = 0.0010                   # round trip, liquid ETFs and mega caps
T_DISCOVER = 2.5
T_HOLDOUT = 1.5
MIN_DISCOVERY = 20
MIN_HOLDOUT = 10
MIN_LIVE_TO_RETIRE = 10
WARMUP = 200                    # bars before the 200-day average exists

TESTING, CONFIRMED, REJECTED, RETIRED = "testing", "confirmed", "rejected", "retired"

# ── alerts ──────────────────────────────────────────────────────────────────

ALERT_MOVE_Z = 2.0              # today's move in units of the 14-day ATR
ALERT_RVOL = 2.5                # volume against the same time of day
ALERT_KEEP_DAYS = 14


def enabled_by_env() -> bool:
    return os.getenv("JARVIS_MARKET", "1").strip().lower() not in ("0", "false", "no", "off")


def autostart() -> bool:
    return os.getenv("JARVIS_MARKET_AUTOSTART", "1").strip().lower() not in ("0", "false", "no")


def interval_minutes() -> int:
    try:
        value = int(os.getenv("JARVIS_MARKET_INTERVAL_MIN", str(DEFAULT_INTERVAL_MIN)))
    except ValueError:
        value = DEFAULT_INTERVAL_MIN
    return max(5, min(value, 120))


def keys() -> tuple[str, str]:
    """APCA_API_KEY_ID/SECRET, unless JARVIS_MARKET_KEYS_FROM names a fleet
    key prefix (e.g. AGS_ALPACA): then that bot's pair, read in place from
    the fleet's key file (accounts.keys_path) and never copied. Bars and the
    calendar are all this module asks for, whichever key it is."""
    prefix = os.getenv("JARVIS_MARKET_KEYS_FROM", "").strip()
    if prefix:
        import accounts
        fleet_keys = accounts.read_env(accounts.keys_path())
        return (fleet_keys.get(prefix + "_KEY", "").strip(),
                fleet_keys.get(prefix + "_SECRET", "").strip())
    return (os.getenv("APCA_API_KEY_ID", "").strip(),
            os.getenv("APCA_API_SECRET_KEY", "").strip())


def normalise_symbol(text: str) -> str:
    return str(text or "").strip().upper().lstrip("$")


# ── Eastern time, without tzdata ────────────────────────────────────────────
# Windows ships no IANA database and tzdata is not a dependency, so the one
# zone this module needs is computed from the US rule (since 2007): daylight
# time from 2am on the second Sunday of March to 2am on the first Sunday of
# November.

def _first_sunday(year: int, month: int, day: int) -> date:
    d = date(year, month, day)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def et_offset(utc: datetime) -> timedelta:
    y = utc.year
    start = datetime.combine(_first_sunday(y, 3, 8), datetime.min.time(),
                             timezone.utc) + timedelta(hours=7)
    end = datetime.combine(_first_sunday(y, 11, 1), datetime.min.time(),
                           timezone.utc) + timedelta(hours=6)
    return timedelta(hours=-4) if start <= utc < end else timedelta(hours=-5)


def to_et(utc: datetime) -> datetime:
    """Naive Eastern wall-clock time."""
    utc = utc.astimezone(timezone.utc)
    return (utc + et_offset(utc)).replace(tzinfo=None)


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# ── store ───────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS market_bars (
                symbol TEXT NOT NULL,
                tf     TEXT NOT NULL,          -- '1Day' (t = ET date) or '15Min' (t = UTC ISO)
                t      TEXT NOT NULL,
                o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
                v REAL NOT NULL,
                PRIMARY KEY (symbol, tf, t)
            );
            CREATE TABLE IF NOT EXISTS market_watchlist (
                symbol TEXT PRIMARY KEY,
                added  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_alerts (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                day    TEXT NOT NULL,
                ts     REAL NOT NULL,
                symbol TEXT NOT NULL,
                kind   TEXT NOT NULL,
                text   TEXT NOT NULL,
                UNIQUE (day, symbol, kind)
            );
            CREATE TABLE IF NOT EXISTS market_patterns (
                name     TEXT PRIMARY KEY,
                status   TEXT NOT NULL,
                reason   TEXT NOT NULL DEFAULT '',
                stats    TEXT NOT NULL DEFAULT '{}',
                tracked  TEXT NOT NULL,        -- first session it was tracked forward
                changed  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS market_signals (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern   TEXT NOT NULL,
                symbol    TEXT NOT NULL,
                day       TEXT NOT NULL,
                direction INTEGER NOT NULL,
                horizon   INTEGER NOT NULL,
                result    REAL,                -- net excess return, once resolved
                resolved  TEXT,
                UNIQUE (pattern, symbol, day)
            );
            CREATE TABLE IF NOT EXISTS market_briefs (
                day  TEXT PRIMARY KEY,
                ts   REAL NOT NULL,
                text TEXT NOT NULL
            );
        """)


def get_state(key: str, default=None):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT value FROM market_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO market_state (key, value) VALUES (?, ?)",
                     (key, json.dumps(value)))


def watchlist() -> list[str]:
    """The user's symbols. Seeded once, from JARVIS_MARKET_WATCHLIST or the
    default, and his to change from then on."""
    with closing(_connect()) as conn, conn:
        rows = conn.execute("SELECT symbol FROM market_watchlist ORDER BY added, symbol").fetchall()
        if rows or get_state("watchlist_seeded"):
            return [r["symbol"] for r in rows]
        seed = [normalise_symbol(s) for s in os.getenv("JARVIS_MARKET_WATCHLIST", "").split(",")]
        seed = [s for s in seed if SYMBOL_RE.fullmatch(s)] or list(DEFAULT_WATCHLIST)
        now = time.time()
        conn.executemany("INSERT OR IGNORE INTO market_watchlist (symbol, added) VALUES (?, ?)",
                         [(s, now + i * 1e-3) for i, s in enumerate(seed[:WATCHLIST_MAX])])
    set_state("watchlist_seeded", True)
    return seed[:WATCHLIST_MAX]


def change_watchlist(add: list[str], remove: list[str]) -> dict:
    current = watchlist()
    added, removed, refused = [], [], []
    with closing(_connect()) as conn, conn:
        for raw in add:
            s = normalise_symbol(raw)
            if not SYMBOL_RE.fullmatch(s):
                refused.append(raw)
            elif s in current or s in added:
                continue
            elif len(current) + len(added) >= WATCHLIST_MAX:
                refused.append(s)
            else:
                conn.execute("INSERT OR IGNORE INTO market_watchlist (symbol, added) VALUES (?, ?)",
                             (s, time.time()))
                added.append(s)
        for raw in remove:
            s = normalise_symbol(raw)
            if conn.execute("DELETE FROM market_watchlist WHERE symbol = ?", (s,)).rowcount:
                removed.append(s)
    return {"added": added, "removed": removed, "refused": refused, "watchlist": watchlist()}


def universe() -> list[str]:
    seen = dict.fromkeys(list(MARKET_SYMBOLS) + list(SECTORS) + watchlist())
    return list(seen)


def save_bars(tf: str, rows: list[tuple]) -> None:
    if not rows:
        return
    with closing(_connect()) as conn, conn:
        conn.executemany(
            "INSERT OR REPLACE INTO market_bars (symbol, tf, t, o, h, l, c, v) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(r[0], tf, *r[1:]) for r in rows])


def last_bar_time(tf: str) -> dict[str, str]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT symbol, MAX(t) AS t FROM market_bars WHERE tf = ? "
                            "GROUP BY symbol", (tf,)).fetchall()
    return {r["symbol"]: r["t"] for r in rows}


def load_bars(tf: str, symbols: list[str]) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list] = {s: [] for s in symbols}
    with closing(_connect()) as conn:
        for row in conn.execute(
                f"SELECT * FROM market_bars WHERE tf = ? AND symbol IN "
                f"({','.join('?' * len(symbols))}) ORDER BY symbol, t", (tf, *symbols)):
            out[row["symbol"]].append(row)
    return out


def prune(today_et: date) -> None:
    cutoff = datetime.combine(today_et - timedelta(days=INTRADAY_KEEP_DAYS),
                              datetime.min.time(), timezone.utc).isoformat()
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM market_bars WHERE tf = '15Min' AND t < ?", (cutoff,))
        conn.execute("DELETE FROM market_alerts WHERE day < ?",
                     ((today_et - timedelta(days=ALERT_KEEP_DAYS)).isoformat(),))


def record_alerts(day: str, alerts: list[dict]) -> list[dict]:
    """Insert what is new today; return only the new ones."""
    fresh = []
    with closing(_connect()) as conn, conn:
        for a in alerts:
            cur = conn.execute("INSERT OR IGNORE INTO market_alerts (day, ts, symbol, kind, text) "
                               "VALUES (?, ?, ?, ?, ?)",
                               (day, time.time(), a["symbol"], a["kind"], a["text"]))
            if cur.rowcount:
                fresh.append(a)
    return fresh


def recent_alerts(limit: int = 40) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT day, ts, symbol, kind, text FROM market_alerts "
                            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Alpaca, read-only ───────────────────────────────────────────────────────

class MarketDataError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class AlpacaData:
    """Bars and the trading calendar. There is deliberately no other method:
    this client cannot see an account or place an order."""

    DATA_URL = "https://data.alpaca.markets"
    TRADING_URL = "https://paper-api.alpaca.markets"

    def __init__(self, key_id: str, secret: str, client: httpx.Client | None = None):
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self._client = client or httpx.Client(timeout=30)
        self.data_url = os.getenv("APCA_DATA_BASE_URL", self.DATA_URL).rstrip("/")
        trading = os.getenv("APCA_API_BASE_URL", self.TRADING_URL).rstrip("/")
        self.trading_url = trading[:-3] if trading.endswith("/v2") else trading

    def _get(self, url: str, params: dict) -> dict | list:
        try:
            r = self._client.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as e:
            raise MarketDataError(f"could not reach Alpaca: {type(e).__name__}") from e
        if r.status_code in (401, 403):
            raise MarketDataError("Alpaca refused the API keys", r.status_code)
        if r.status_code == 429:
            raise MarketDataError("Alpaca rate limit reached", r.status_code)
        if r.status_code >= 400:
            raise MarketDataError(f"Alpaca answered {r.status_code}", r.status_code)
        return r.json()

    def bars(self, symbols: list[str], timeframe: str, start: datetime,
             end: datetime) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        params = {"symbols": ",".join(symbols), "timeframe": timeframe,
                  "start": start.isoformat(), "end": end.isoformat(), "feed": "sip",
                  "adjustment": "all", "limit": 10000}
        for _ in range(200):                     # pages; bounded, never a hang
            body = self._get(f"{self.data_url}/v2/stocks/bars", params)
            for sym, rows in (body.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            token = body.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
        return out

    def calendar(self, start: date, end: date) -> list[dict]:
        body = self._get(f"{self.trading_url}/v2/calendar",
                         {"start": start.isoformat(), "end": end.isoformat()})
        return body if isinstance(body, list) else []


# ── sessions ────────────────────────────────────────────────────────────────

@dataclass
class Session:
    day: str                    # ET date
    open: datetime              # naive ET
    close: datetime


def sessions_from_calendar(rows: list[dict]) -> list[Session]:
    out = []
    for r in rows:
        try:
            d = r["date"]
            out.append(Session(d, datetime.fromisoformat(f"{d}T{r['open']}"),
                               datetime.fromisoformat(f"{d}T{r['close']}")))
        except (KeyError, ValueError):
            continue
    return sorted(out, key=lambda s: s.day)


def market_phase(sessions: list[Session], now_et: datetime) -> tuple[str, Optional[Session], Optional[Session]]:
    """('open' | 'closed', the session now open or most recently finished,
    the next one to open)."""
    current, upcoming = None, None
    for s in sessions:
        if s.open <= now_et < s.close:
            return "open", s, None
        if s.close <= now_et:
            current = s
        elif upcoming is None and s.open > now_et:
            upcoming = s
    return "closed", current, upcoming


# ── indicators ──────────────────────────────────────────────────────────────

def sma(xs: list[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(xs)
    total = 0.0
    for i, x in enumerate(xs):
        total += x
        if i >= n:
            total -= xs[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi(closes: list[float], n: int) -> list[Optional[float]]:
    """Wilder's RSI."""
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n

    def value(g: float, l: float) -> float:
        return 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)

    out[n] = value(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = value(avg_g, avg_l)
    return out


def atr(h: list[float], l: list[float], c: list[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(c)
    if len(c) <= n:
        return out
    trs = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                           for i in range(1, len(c))]
    value = sum(trs[1:n + 1]) / n
    out[n] = value
    for i in range(n + 1, len(c)):
        value = (value * (n - 1) + trs[i]) / n
        out[i] = value
    return out


def relative_volume(v: list[float], n: int = 20) -> list[Optional[float]]:
    avg = sma(v, n)
    return [None if i == 0 or avg[i - 1] in (None, 0) else v[i] / avg[i - 1]
            for i in range(len(v))]


@dataclass
class Series:
    """One symbol's completed daily sessions, with what the patterns read."""
    symbol: str
    days: list[str]
    o: list[float]
    h: list[float]
    l: list[float]
    c: list[float]
    v: list[float]
    sma20: list = field(default_factory=list)
    sma50: list = field(default_factory=list)
    sma200: list = field(default_factory=list)
    rsi2: list = field(default_factory=list)
    rsi14: list = field(default_factory=list)
    atr14: list = field(default_factory=list)
    rvol: list = field(default_factory=list)

    @classmethod
    def build(cls, symbol: str, rows: list) -> "Series":
        s = cls(symbol, [r["t"] for r in rows], [r["o"] for r in rows], [r["h"] for r in rows],
                [r["l"] for r in rows], [r["c"] for r in rows], [r["v"] for r in rows])
        s.sma20, s.sma50, s.sma200 = sma(s.c, 20), sma(s.c, 50), sma(s.c, 200)
        s.rsi2, s.rsi14 = rsi(s.c, 2), rsi(s.c, 14)
        s.atr14 = atr(s.h, s.l, s.c, 14)
        s.rvol = relative_volume(s.v, 20)
        return s

    def __len__(self) -> int:
        return len(self.c)

    def ret(self, i: int, back: int) -> Optional[float]:
        if i - back < 0 or self.c[i - back] <= 0:
            return None
        return self.c[i] / self.c[i - back] - 1

    def forward(self, i: int, horizon: int) -> Optional[float]:
        """From the next session's open to the close `horizon` sessions on."""
        if i + horizon >= len(self.c) or self.o[i + 1] <= 0:
            return None
        return self.c[i + horizon] / self.o[i + 1] - 1


# ── patterns ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pattern:
    name: str
    label: str
    description: str
    direction: int              # +1: expect it to beat its average; -1: to lag it
    horizon: int                # sessions held
    fires: Callable[[Series, int], bool]


def _ok(*values) -> bool:
    return all(v is not None for v in values)


def _uptrend(s: Series, i: int) -> bool:
    return _ok(s.sma200[i]) and s.c[i] > s.sma200[i]


def _downtrend(s: Series, i: int) -> bool:
    return _ok(s.sma200[i]) and s.c[i] < s.sma200[i]


def _new_high(s: Series, i: int, n: int) -> bool:
    return i > n and s.c[i] > max(s.c[i - n:i]) and s.c[i - 1] <= max(s.c[i - n - 1:i - 1])


def _new_low(s: Series, i: int, n: int) -> bool:
    return i > n and s.c[i] < min(s.c[i - n:i]) and s.c[i - 1] >= min(s.c[i - n - 1:i - 1])


def _cross(fast: list, slow: list, i: int) -> int:
    if i < 1 or not _ok(fast[i], slow[i], fast[i - 1], slow[i - 1]):
        return 0
    if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
        return 1
    if fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
        return -1
    return 0


def _move(s: Series, i: int) -> Optional[float]:
    return s.ret(i, 1)


PATTERNS: tuple[Pattern, ...] = (
    Pattern("pullback_in_uptrend", "Oversold pullback in an uptrend",
            "2-day RSI under 10 while above the 200-day average; expect a bounce over 5 sessions",
            +1, 5, lambda s, i: _uptrend(s, i) and _ok(s.rsi2[i]) and s.rsi2[i] < 10),
    Pattern("rally_in_downtrend", "Overbought rally in a downtrend",
            "2-day RSI over 90 while below the 200-day average; expect it to fade over 5 sessions",
            -1, 5, lambda s, i: _downtrend(s, i) and _ok(s.rsi2[i]) and s.rsi2[i] > 90),
    Pattern("breakout_20d", "20-day breakout",
            "First close above the prior 20-day high; expect momentum over 10 sessions",
            +1, 10, lambda s, i: _new_high(s, i, 20)),
    Pattern("breakdown_20d", "20-day breakdown",
            "First close below the prior 20-day low; expect weakness over 10 sessions",
            -1, 10, lambda s, i: _new_low(s, i, 20)),
    Pattern("volume_surge_up", "High-volume up day",
            "Up 2.5% or more on twice the usual volume; expect continuation over 5 sessions",
            +1, 5, lambda s, i: _ok(s.rvol[i], _move(s, i)) and s.rvol[i] >= 2 and _move(s, i) >= 0.025),
    Pattern("volume_flush", "High-volume selloff",
            "Down 2.5% or more on twice the usual volume; expect a rebound over 5 sessions",
            +1, 5, lambda s, i: _ok(s.rvol[i], _move(s, i)) and s.rvol[i] >= 2 and _move(s, i) <= -0.025),
    Pattern("three_down_in_uptrend", "Three lower closes in an uptrend",
            "Three consecutive lower closes above the 200-day average; expect a bounce over 3 sessions",
            +1, 3, lambda s, i: i >= 3 and _uptrend(s, i)
            and s.c[i] < s.c[i - 1] < s.c[i - 2] < s.c[i - 3]),
    Pattern("golden_cross", "Golden cross",
            "50-day average crosses above the 200-day; expect strength over 20 sessions",
            +1, 20, lambda s, i: _cross(s.sma50, s.sma200, i) == 1),
    Pattern("death_cross", "Death cross",
            "50-day average crosses below the 200-day; expect weakness over 20 sessions",
            -1, 20, lambda s, i: _cross(s.sma50, s.sma200, i) == -1),
    Pattern("gap_up_fade", "Big gap up",
            "Opened 2% or more (and 1.5 ATRs) above the prior close; expect it to give back over 2 sessions",
            -1, 2, lambda s, i: i >= 1 and _ok(s.atr14[i - 1]) and s.c[i - 1] > 0
            and s.o[i] / s.c[i - 1] - 1 >= max(0.02, 1.5 * s.atr14[i - 1] / s.c[i - 1])),
    Pattern("gap_down_rebound", "Big gap down",
            "Opened 2% or more (and 1.5 ATRs) below the prior close; expect a rebound over 2 sessions",
            +1, 2, lambda s, i: i >= 1 and _ok(s.atr14[i - 1]) and s.c[i - 1] > 0
            and 1 - s.o[i] / s.c[i - 1] >= max(0.02, 1.5 * s.atr14[i - 1] / s.c[i - 1])),
    Pattern("low_vol_squeeze", "Volatility squeeze in an uptrend",
            "14-day ATR at its lowest in 60 sessions while above the 200-day; expect a move up over 10 sessions",
            +1, 10, lambda s, i: i >= 60 and _uptrend(s, i) and _ok(*s.atr14[i - 59:i + 1])
            and s.atr14[i] <= min(s.atr14[i - 59:i + 1])),
)
PATTERNS_BY_NAME = {p.name: p for p in PATTERNS}


@dataclass
class Stats:
    n: int = 0
    mean: float = 0.0
    t: float = 0.0
    hit: float = 0.0

    def as_dict(self) -> dict:
        return {"n": self.n, "mean": round(self.mean, 5), "t": round(self.t, 2),
                "hit": round(self.hit, 3)}


def summarise(values: list[float]) -> Stats:
    n = len(values)
    if n == 0:
        return Stats()
    mean = sum(values) / n
    sd = statistics.stdev(values) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return Stats(n, mean, t, sum(1 for v in values if v > 0) / n)


def _baselines(s: Series, horizon: int, split_day: str) -> tuple[Optional[float], Optional[float]]:
    """A symbol's average forward return over `horizon`, in each half — what
    an ordinary day was followed by."""
    early, late = [], []
    for i in range(WARMUP, len(s) - horizon):
        r = s.forward(i, horizon)
        if r is not None:
            (early if s.days[i] < split_day else late).append(r)
    return (sum(early) / len(early) if early else None,
            sum(late) / len(late) if late else None)


def _thin(observations: dict[str, float], calendar: dict[str, int], horizon: int) -> list[float]:
    """One value per signal day, days closer than `horizon` sessions dropped."""
    kept, last = [], None
    for day in sorted(observations):
        idx = calendar.get(day)
        if idx is None:
            continue
        if last is None or idx - last >= horizon:
            kept.append(observations[day])
            last = idx
    return kept


def split_point(series: dict[str, Series]) -> Optional[str]:
    days = sorted({d for s in series.values() for d in s.days[WARMUP:]})
    return days[len(days) * 2 // 3] if len(days) >= 60 else None


def evaluate(pattern: Pattern, series: dict[str, Series], split_day: str,
             calendar: dict[str, int]) -> dict:
    early: dict[str, list[float]] = {}
    late: dict[str, list[float]] = {}
    for s in series.values():
        if len(s) <= WARMUP + pattern.horizon:
            continue
        base_early, base_late = _baselines(s, pattern.horizon, split_day)
        for i in range(WARMUP, len(s) - pattern.horizon):
            if not pattern.fires(s, i):
                continue
            r = s.forward(i, pattern.horizon)
            in_early = s.days[i] < split_day
            base = base_early if in_early else base_late
            if r is None or base is None:
                continue
            value = pattern.direction * (r - base) - COST
            (early if in_early else late).setdefault(s.days[i], []).append(value)
    collapse = lambda d: {k: sum(v) / len(v) for k, v in d.items()}
    return {"discovery": summarise(_thin(collapse(early), calendar, pattern.horizon)),
            "holdout": summarise(_thin(collapse(late), calendar, pattern.horizon)),
            "events": sum(len(v) for v in early.values()) + sum(len(v) for v in late.values())}


def judge(discovery: Stats, holdout: Stats, live: Stats) -> tuple[str, str]:
    if discovery.n < MIN_DISCOVERY:
        return TESTING, f"only {discovery.n} independent events in the older data"
    if discovery.mean <= 0 or discovery.t < T_DISCOVER:
        return REJECTED, "no edge after costs in the older data"
    if holdout.n < MIN_HOLDOUT:
        return TESTING, "an edge in the older data; waiting for enough recent events"
    if holdout.mean <= 0 or holdout.t < T_HOLDOUT:
        return REJECTED, "the older edge did not hold up in recent data"
    if live.n >= MIN_LIVE_TO_RETIRE and live.mean <= 0:
        return RETIRED, f"losing on balance across {live.n} signals since tracking began"
    return CONFIRMED, "held up in data the discovery never saw"


# ── analysis ────────────────────────────────────────────────────────────────

def _pct(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(100 * x, 2)


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b in (None, 0) else a / b - 1


def realised_vol(closes: list[float], end: int, n: int = 20) -> Optional[float]:
    if end < n:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(end - n + 1, end + 1)
            if closes[i - 1] > 0 and closes[i] > 0]
    return statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 2 else None


_RTH_OPEN = datetime.strptime("09:30", "%H:%M").time()
_RTH_CLOSE = datetime.strptime("16:00", "%H:%M").time()


@dataclass
class Today:
    """The session in progress (or just finished), from 15-minute bars."""
    open: float
    high: float
    low: float
    last: float
    volume: float
    bars: int
    rvol: Optional[float]


def today_from_intraday(rows: list, session: Session) -> tuple[Optional[Today], dict[str, list]]:
    """(this session, earlier sessions' bar volumes by day), regular hours only."""
    by_day: dict[str, list] = {}
    for r in rows:
        et = to_et(parse_utc(r["t"]))
        if _RTH_OPEN <= et.time() < _RTH_CLOSE:
            by_day.setdefault(et.date().isoformat(), []).append(r)
    bars = [r for r in by_day.get(session.day, [])
            if to_et(parse_utc(r["t"])) < session.close]
    if not bars:
        return None, by_day
    k = len(bars)
    vol = sum(r["v"] for r in bars)
    earlier = [d for d in sorted(by_day) if d < session.day][-INTRADAY_LOOKBACK:]
    past = [sum(r["v"] for r in by_day[d][:k]) for d in earlier if len(by_day[d]) >= k]
    avg = sum(past) / len(past) if past else None
    return Today(bars[0]["o"], max(r["h"] for r in bars), min(r["l"] for r in bars),
                 bars[-1]["c"], vol, k, (vol / avg) if avg else None), by_day


def trend_label(s: Series, price: float) -> str:
    i = len(s) - 1
    s50, s200 = s.sma50[i], s.sma200[i]
    if s50 is None or s200 is None:
        return "young"
    if price > s50 > s200:
        return "uptrend"
    if price < s50 < s200:
        return "downtrend"
    if price > s200:
        return "pullback" if price < s50 else "recovering"
    return "rebounding" if price > s50 else "weak"


def symbol_view(s: Series, today: Optional[Today], spy_ret20: Optional[float],
                has_today: bool) -> dict:
    """What one symbol is doing. `s` holds completed sessions only: when
    `has_today` its last bar is this session's (finished) one, otherwise its
    last bar is the previous close and `today` is the session in progress."""
    i = len(s) - 1
    prev_i = i - 1 if has_today else i
    if has_today:
        price = s.c[i]                  # the official close beats the last 15-minute bar
    elif today is not None:
        price = today.last
    else:
        price = s.c[i]                  # nothing yet this session: no change to report
    prev_close = s.c[prev_i] if prev_i >= 0 else None
    change = _ratio(price, prev_close) if (has_today or today is not None) else None
    atr_now = s.atr14[prev_i] if prev_i >= 0 else None
    move_z = (change * prev_close / atr_now) if _ok(change, atr_now) and atr_now else None
    lo52 = min(s.l[-252:]) if s.l else None
    hi52 = max(s.h[-252:]) if s.h else None
    ret20 = _ratio(price, s.c[prev_i - 19]) if prev_i >= 19 else None
    view = {
        "symbol": s.symbol,
        "price": round(price, 2),
        "change": _pct(change),
        "ret_5d": _pct(_ratio(price, s.c[prev_i - 4]) if prev_i >= 4 else None),
        "ret_20d": _pct(ret20),
        "rs_20d": _pct(None if ret20 is None or spy_ret20 is None else ret20 - spy_ret20),
        "trend": trend_label(s, price),
        "vs_50d": _pct(_ratio(price, s.sma50[prev_i] if prev_i >= 0 else None)),
        "vs_200d": _pct(_ratio(price, s.sma200[prev_i] if prev_i >= 0 else None)),
        "rsi14": None if prev_i < 0 or s.rsi14[prev_i] is None else round(s.rsi14[prev_i], 1),
        "atr_pct": _pct(None if not _ok(atr_now) or not prev_close else atr_now / prev_close),
        "move_z": None if move_z is None else round(move_z, 2),
        "rvol": None if today is None or today.rvol is None else round(today.rvol, 2),
        "from_high": _pct(_ratio(price, hi52)),
        "from_low": _pct(_ratio(price, lo52)),
        "flags": [],
    }
    flags = view["flags"]
    if move_z is not None and abs(move_z) >= ALERT_MOVE_Z:
        flags.append("big_move")
    if view["rvol"] is not None and view["rvol"] >= ALERT_RVOL and today and today.bars >= 2:
        flags.append("heavy_volume")
    if hi52 and today and today.high >= hi52 and len(s) >= 252:
        flags.append("52w_high")
    if lo52 and today and today.low <= lo52 and len(s) >= 252:
        flags.append("52w_low")
    for key, avg in (("50d", s.sma50), ("200d", s.sma200)):
        level = avg[prev_i] if prev_i >= 0 else None
        if level is None or prev_close is None:
            continue
        if prev_close < level <= price:
            flags.append(f"crossed_above_{key}")
        elif prev_close > level >= price:
            flags.append(f"crossed_below_{key}")
    return view


FLAG_TEXT = {
    "big_move": lambda v: f"{v['symbol']} {v['change']:+.1f}% today, {abs(v['move_z']):.1f}× its usual daily range",
    "heavy_volume": lambda v: f"{v['symbol']} trading {v['rvol']:.1f}× its usual volume for this time of day",
    "52w_high": lambda v: f"{v['symbol']} at a 52-week high",
    "52w_low": lambda v: f"{v['symbol']} at a 52-week low",
    "crossed_above_50d": lambda v: f"{v['symbol']} crossed above its 50-day average",
    "crossed_below_50d": lambda v: f"{v['symbol']} crossed below its 50-day average",
    "crossed_above_200d": lambda v: f"{v['symbol']} crossed above its 200-day average",
    "crossed_below_200d": lambda v: f"{v['symbol']} crossed below its 200-day average",
}


def regime_view(series: dict[str, Series], views: dict[str, dict]) -> dict:
    spy = series.get(BENCHMARK)
    spy_view = views.get(BENCHMARK)
    if spy is None or spy_view is None or len(spy) < WARMUP:
        return {"trend": "unknown", "summary": "Not enough S&P 500 history yet."}
    i = len(spy) - 1
    vols = [realised_vol(spy.c, j) for j in range(max(21, i - 251), i + 1)]
    vols = [v for v in vols if v is not None]
    vol_now = vols[-1] if vols else None
    pctile = (100 * sum(1 for v in vols if v <= vol_now) / len(vols)) if vols else None
    vol_state = ("unknown" if pctile is None else "calm" if pctile < 30 else
                 "normal" if pctile < 70 else "elevated" if pctile < 90 else "stressed")
    stocks = [v for sym, v in views.items() if sym not in ("TLT", "GLD", "VIXY")]
    above50 = [v for v in stocks if v["vs_50d"] is not None]
    above200 = [v for v in stocks if v["vs_200d"] is not None]
    breadth50 = 100 * sum(1 for v in above50 if v["vs_50d"] > 0) / len(above50) if above50 else None
    breadth200 = 100 * sum(1 for v in above200 if v["vs_200d"] > 0) / len(above200) if above200 else None
    trend = {"uptrend": "uptrend", "downtrend": "downtrend"}.get(spy_view["trend"], "mixed")
    vixy = views.get("VIXY")
    return {
        "trend": trend,
        "spy_trend": spy_view["trend"],
        "spy_vs_50d": spy_view["vs_50d"],
        "spy_vs_200d": spy_view["vs_200d"],
        "breadth_50d": None if breadth50 is None else round(breadth50),
        "breadth_200d": None if breadth200 is None else round(breadth200),
        "vol_20d": _pct(vol_now),
        "vol_percentile": None if pctile is None else round(pctile),
        "vol_state": vol_state,
        "vixy_5d": vixy["ret_5d"] if vixy else None,
    }


def describe_regime(r: dict) -> str:
    if r.get("trend") == "unknown":
        return r.get("summary", "")
    parts = ["The market is mixed" if r["trend"] == "mixed" else
             f"The market is in {'an' if r['trend'] == 'uptrend' else 'a'} {r['trend']}"]
    if r.get("spy_vs_200d") is not None:
        parts[0] += f": the S&P 500 is {abs(r['spy_vs_200d']):.1f}% " \
                    f"{'above' if r['spy_vs_200d'] >= 0 else 'below'} its 200-day average"
    if r.get("breadth_50d") is not None:
        parts.append(f"{r['breadth_50d']}% of what I follow is above its 50-day")
    if r.get("vol_20d") is not None:
        parts.append(f"volatility is {r['vol_state']} ({r['vol_20d']:.0f}% annualised, "
                     f"{r['vol_percentile']}th percentile of the past year)")
    return "; ".join(parts) + "."


# ── the engine ──────────────────────────────────────────────────────────────

class MarketEngine:
    def __init__(self, client_factory: Callable[[], Optional[AlpacaData]] | None = None,
                 notify: Callable[[str, str], "asyncio.Future"] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self._client_factory = client_factory or self._default_client
        self._notify = notify
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._calendar: tuple[str, list[Session]] = ("", [])
        self.last_tick: Optional[float] = None
        self.last_error = ""
        self.next_run: Optional[float] = None
        self.phase = "unknown"

    @staticmethod
    def _default_client() -> Optional[AlpacaData]:
        key_id, secret = keys()
        return AlpacaData(key_id, secret) if key_id and secret else None

    @property
    def configured(self) -> bool:
        return all(keys())

    # lifecycle

    async def start(self) -> None:
        if self._task is None and enabled_by_env():
            await asyncio.to_thread(init_db)
            self._task = asyncio.create_task(self._loop(), name="market-study")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            delay = 30 * 60.0
            try:
                delay = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                log.warning("market cycle failed", exc_info=True)
            self.next_run = time.time() + delay
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # one cycle

    async def tick(self, force: bool = False) -> float:
        """Run whatever is due and return how long to sleep."""
        async with self._lock:
            try:
                result = await asyncio.to_thread(self._cycle, force)
            finally:
                # The bots' trades are local; learning from them must not
                # wait on Alpaca being reachable.
                await asyncio.to_thread(self._learn_from_fleet)
        self.last_tick = time.time()
        if result.get("notify") and self._notify is not None:
            try:
                await self._notify(result["notify"], "Market close")
                await asyncio.to_thread(set_state, "notified_day", result["day"])
            except Exception:
                log.warning("market notification failed", exc_info=True)
        return result["sleep"]

    @staticmethod
    def _learn_from_fleet() -> None:
        import fleet
        if not fleet.enabled():
            return
        try:
            fleet.sync()
            fleet.export_view()
        except Exception:
            log.warning("learning from the fleet failed", exc_info=True)

    def _sessions(self, client: AlpacaData, today: date) -> list[Session]:
        key = today.isoformat()
        if self._calendar[0] != key:
            rows = client.calendar(today - timedelta(days=14), today + timedelta(days=10))
            self._calendar = (key, sessions_from_calendar(rows))
        return self._calendar[1]

    def _cycle(self, force: bool) -> dict:
        init_db()
        client = self._client_factory()
        if client is None:
            self.phase = "unconfigured"
            self.last_error = ""
            return {"sleep": 30 * 60.0}
        now = self._clock()
        now_et = to_et(now)
        sessions = self._sessions(client, now_et.date())
        phase, session, upcoming = market_phase(sessions, now_et)
        self.phase = phase
        snapshot = get_state("snapshot")
        interval = interval_minutes() * 60.0
        if session is None:
            return {"sleep": 30 * 60.0}

        if phase == "open":
            self._collect(client, now)
            self._analyse(session, done=False)
            self.last_error = ""
            return {"sleep": interval}

        # Closed. The finished session's last bars arrive DATA_DELAY_MIN late.
        settled = now_et >= session.close + timedelta(minutes=DATA_DELAY_MIN + 1)
        due = snapshot is None or force or (
            settled and get_state("studied_day") != session.day)
        if not due:
            if not settled:
                wait = (session.close + timedelta(minutes=DATA_DELAY_MIN + 1) - now_et).total_seconds()
            elif upcoming is not None:
                wait = (upcoming.open - now_et).total_seconds() + 60
            else:
                wait = 30 * 60.0
            return {"sleep": max(60.0, min(wait, 60 * 60.0))}

        self._collect(client, now)
        snap = self._analyse(session, done=settled)
        out: dict = {"sleep": 60.0 if not settled else 30 * 60.0, "day": session.day}
        if settled:
            self._study(session.day)
            snap = self._analyse(session, done=True)
            brief = snap["brief"]
            with closing(_connect()) as conn, conn:
                conn.execute("INSERT OR REPLACE INTO market_briefs (day, ts, text) VALUES (?, ?, ?)",
                             (session.day, time.time(), brief))
            set_state("studied_day", session.day)
            if get_state("notified_day") != session.day and snapshot is not None:
                out["notify"] = snap["headline"]
        self.last_error = ""
        return out

    # collect

    def _collect(self, client: AlpacaData, now: datetime) -> None:
        symbols = universe()
        end = now - timedelta(minutes=DATA_DELAY_MIN)
        today_et = to_et(now).date()
        for tf, back in (("1Day", timedelta(days=365 * HISTORY_YEARS + 10)),
                         ("15Min", timedelta(days=INTRADAY_KEEP_DAYS))):
            last = last_bar_time(tf)
            fresh = [s for s in symbols if s not in last]
            stale = [s for s in symbols if s in last]
            groups = []
            if fresh:
                groups.append((fresh, now - back))
            if stale:
                oldest = min(last[s] for s in stale)
                start = (datetime.fromisoformat(oldest).replace(tzinfo=timezone.utc)
                         if tf == "1Day" else parse_utc(oldest)) - timedelta(days=4 if tf == "1Day" else 0)
                groups.append((stale, start))
            for group, start in groups:
                bars = self._fetch(client, group, tf, start, end)
                rows = []
                for sym, items in bars.items():
                    for b in items:
                        t = (to_et(parse_utc(b["t"])).date().isoformat() if tf == "1Day"
                             else parse_utc(b["t"]).isoformat())
                        rows.append((sym, t, b["o"], b["h"], b["l"], b["c"], b.get("v", 0)))
                save_bars(tf, rows)
        prune(today_et)

    @staticmethod
    def _fetch(client: AlpacaData, group: list[str], tf: str, start: datetime,
               end: datetime) -> dict[str, list[dict]]:
        """One request for the group; if Alpaca rejects it (a symbol it does
        not know fails the whole request), one per symbol, skipping the bad."""
        try:
            return client.bars(group, tf, start, end)
        except MarketDataError as e:
            if e.status != 400 or len(group) == 1:
                raise
        out = {}
        for sym in group:
            try:
                out.update(client.bars([sym], tf, start, end))
            except MarketDataError as e:
                if e.status != 400:
                    raise
                log.info("market data has no symbol %s", sym)
        return out

    # analyse

    def _series(self, session: Session, done: bool) -> dict[str, Series]:
        rows = load_bars("1Day", universe())
        out = {}
        for sym, items in rows.items():
            # While a session runs, its daily bar is partial: patterns and
            # averages read completed sessions only.
            kept = [r for r in items if done or r["t"] < session.day]
            if kept:
                out[sym] = Series.build(sym, kept)
        return out

    def _analyse(self, session: Session, done: bool) -> dict:
        series = self._series(session, done)
        intraday = load_bars("15Min", list(series))
        todays = {sym: today_from_intraday(intraday.get(sym, []), session)[0] for sym in series}
        has_today = {sym: s.days and s.days[-1] == session.day for sym, s in series.items()}
        spy = series.get(BENCHMARK)
        spy_today = todays.get(BENCHMARK)
        spy_ret20 = None
        if spy is not None and len(spy) > 20:
            base_i = len(spy) - (2 if has_today.get(BENCHMARK) else 1)
            price = spy_today.last if spy_today else spy.c[-1]
            spy_ret20 = _ratio(price, spy.c[base_i - 19])
        views = {}
        for sym, s in series.items():
            if len(s) < 2:
                continue
            views[sym] = symbol_view(s, todays.get(sym), spy_ret20, bool(has_today.get(sym)))
        regime = regime_view(series, views)
        sectors = sorted((dict(views[s], name=SECTORS[s]) for s in SECTORS if s in views),
                         key=lambda v: v["rs_20d"] if v["rs_20d"] is not None else -1e9, reverse=True)
        market = [dict(views[s], name=MARKET_SYMBOLS[s]) for s in MARKET_SYMBOLS if s in views]
        watch = [views[s] for s in watchlist() if s in views]
        alerts = [{"symbol": v["symbol"], "kind": f, "text": FLAG_TEXT[f](v)}
                  for v in list(views.values()) for f in v["flags"] if f in FLAG_TEXT]
        record_alerts(session.day, alerts)
        patterns = self._pattern_rows(series)
        setups = self._setups(series)
        snap = {
            "as_of": self._clock().isoformat(),
            "data_through": (self._clock() - timedelta(minutes=DATA_DELAY_MIN)).isoformat(),
            "session": {"day": session.day, "state": "done" if done else "open"},
            "regime": dict(regime, summary=describe_regime(regime)),
            "market": market,
            "sectors": sectors,
            "watchlist": watch,
            "missing": [s for s in watchlist() if s not in views],
            "patterns": patterns,
            "setups": setups,
        }
        snap["headline"], snap["brief"] = compose_brief(snap, recent_alerts(60), session.day)
        set_state("snapshot", snap)
        return snap

    def _setups(self, series: dict[str, Series]) -> list[dict]:
        """Patterns that fired on the latest completed session."""
        status = {r["name"]: r["status"] for r in self._pattern_rows(series)}
        spy = series.get(BENCHMARK)
        if spy is None:
            return []
        day = spy.days[-1]
        out = []
        for p in PATTERNS:
            hits = [s.symbol for s in series.values()
                    if len(s) > WARMUP and s.days[-1] == day and p.fires(s, len(s) - 1)]
            if hits:
                out.append({"pattern": p.name, "label": p.label, "status": status.get(p.name, TESTING),
                            "direction": p.direction, "horizon": p.horizon, "symbols": hits,
                            "day": day})
        return out

    # study

    def _study(self, day: str) -> None:
        """After the close: re-test every pattern, record today's signals,
        score the signals whose horizon has passed."""
        series = self._series(Session(day, datetime.max, datetime.max), done=True)
        spy = series.get(BENCHMARK)
        if spy is None or len(spy) < WARMUP + 60:
            return
        calendar = {d: i for i, d in enumerate(spy.days)}
        split_day = split_point(series)
        if split_day is None:
            return
        self._resolve_signals(series)
        live = self._live_stats()
        now = time.time()
        with closing(_connect()) as conn, conn:
            for p in PATTERNS:
                result = evaluate(p, series, split_day, calendar)
                status, reason = judge(result["discovery"], result["holdout"],
                                       live.get(p.name, Stats()))
                stats = {"discovery": result["discovery"].as_dict(),
                         "holdout": result["holdout"].as_dict(),
                         "live": live.get(p.name, Stats()).as_dict(),
                         "events": result["events"], "split": split_day}
                row = conn.execute("SELECT status, tracked, changed FROM market_patterns "
                                   "WHERE name = ?", (p.name,)).fetchone()
                conn.execute(
                    "INSERT OR REPLACE INTO market_patterns (name, status, reason, stats, tracked, changed) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (p.name, status, reason, json.dumps(stats), row["tracked"] if row else day,
                     row["changed"] if row and row["status"] == status else now))
                for s in series.values():
                    if len(s) > WARMUP and s.days[-1] == day and p.fires(s, len(s) - 1):
                        conn.execute("INSERT OR IGNORE INTO market_signals "
                                     "(pattern, symbol, day, direction, horizon) VALUES (?, ?, ?, ?, ?)",
                                     (p.name, s.symbol, day, p.direction, p.horizon))

    def _resolve_signals(self, series: dict[str, Series]) -> None:
        with closing(_connect()) as conn, conn:
            pending = conn.execute("SELECT * FROM market_signals WHERE result IS NULL").fetchall()
            for sig in pending:
                s = series.get(sig["symbol"])
                if s is None or sig["day"] not in s.days:
                    continue
                i = s.days.index(sig["day"])
                r = s.forward(i, sig["horizon"])
                if r is None:
                    continue
                base = [x for x in (s.forward(j, sig["horizon"])
                                    for j in range(max(WARMUP, len(s) - 252), len(s) - sig["horizon"]))
                        if x is not None]
                baseline = sum(base) / len(base) if base else 0.0
                conn.execute("UPDATE market_signals SET result = ?, resolved = ? WHERE id = ?",
                             (sig["direction"] * (r - baseline) - COST, s.days[i + sig["horizon"]],
                              sig["id"]))

    def _live_stats(self) -> dict[str, Stats]:
        with closing(_connect()) as conn:
            rows = conn.execute("SELECT pattern, day, AVG(result) AS r FROM market_signals "
                                "WHERE result IS NOT NULL GROUP BY pattern, day").fetchall()
        by: dict[str, list[float]] = {}
        for r in rows:
            by.setdefault(r["pattern"], []).append(r["r"])
        return {k: summarise(v) for k, v in by.items()}

    def _pattern_rows(self, series: dict[str, Series] | None = None) -> list[dict]:
        with closing(_connect()) as conn:
            rows = {r["name"]: r for r in conn.execute("SELECT * FROM market_patterns")}
            pending = dict(conn.execute("SELECT pattern, COUNT(*) FROM market_signals "
                                        "WHERE result IS NULL GROUP BY pattern").fetchall())
        out = []
        for p in PATTERNS:
            row = rows.get(p.name)
            out.append({
                "name": p.name, "label": p.label, "description": p.description,
                "direction": p.direction, "horizon": p.horizon,
                "status": row["status"] if row else TESTING,
                "reason": row["reason"] if row else "not tested yet — the first test runs after a close",
                "stats": json.loads(row["stats"]) if row else {},
                "tracked_since": row["tracked"] if row else None,
                "pending_signals": pending.get(p.name, 0),
            })
        order = {CONFIRMED: 0, TESTING: 1, RETIRED: 2, REJECTED: 3}
        return sorted(out, key=lambda r: order.get(r["status"], 9))

    # reading

    def status(self) -> dict:
        init_db()
        snap = get_state("snapshot") or {}
        return {
            "enabled": enabled_by_env(),
            "configured": self.configured,
            "running": self._task is not None and not self._task.done(),
            "phase": self.phase,
            "interval_min": interval_minutes(),
            "last_tick": self.last_tick,
            "next_run": self.next_run,
            "last_error": self.last_error,
            "studied_day": get_state("studied_day"),
            "watchlist": watchlist(),
            "alerts": recent_alerts(40),
            "snapshot": snap or None,
        }


def compose_brief(snap: dict, alerts: list[dict], day: str) -> tuple[str, str]:
    """(one line for a notification, the full brief). Written from the
    numbers, so every claim in it is one the dashboard shows."""
    regime = snap["regime"]
    lines = [f"{'Close' if snap['session']['state'] == 'done' else 'So far'} on {day} "
             f"(data {DATA_DELAY_MIN - 1} min delayed). {regime.get('summary', '')}"]
    spy = next((m for m in snap["market"] if m["symbol"] == BENCHMARK), None)
    head = []
    for m in snap["market"]:
        if m["symbol"] in ("SPY", "QQQ", "IWM") and m["change"] is not None:
            head.append(f"{m['symbol']} {m['change']:+.1f}%")
    if head:
        lines.append("Indexes: " + ", ".join(head) + ".")
    sectors = [s for s in snap["sectors"] if s["rs_20d"] is not None]
    if len(sectors) >= 4:
        lines.append("Leading over 20 days: " + ", ".join(f"{s['name']} ({s['rs_20d']:+.1f} vs S&P)"
                                                          for s in sectors[:2])
                     + "; lagging: " + ", ".join(f"{s['name']} ({s['rs_20d']:+.1f})"
                                                 for s in sectors[-2:]) + ".")
    movers = sorted((v for v in snap["watchlist"] if v["change"] is not None),
                    key=lambda v: abs(v["change"]), reverse=True)[:4]
    if movers:
        lines.append("Watchlist movers: " + ", ".join(
            f"{v['symbol']} {v['change']:+.1f}%" + (f" ({abs(v['move_z']):.1f}× usual range)"
                                                    if v["move_z"] and abs(v["move_z"]) >= ALERT_MOVE_Z else "")
            for v in movers) + ".")
    today_alerts = [a for a in alerts if a["day"] == day]
    if today_alerts:
        lines.append(f"{len(today_alerts)} alert{'s' if len(today_alerts) != 1 else ''} today, "
                     f"e.g. {today_alerts[0]['text']}.")
    counts: dict[str, int] = {}
    for p in snap["patterns"]:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    lines.append("Patterns: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ".")
    edge = [s for s in snap["setups"] if s["status"] == CONFIRMED]
    if edge:
        lines.append("Setups with a tested edge: " + "; ".join(
            f"{s['label']} in {', '.join(s['symbols'][:5])}" for s in edge) + ".")
    else:
        lines.append("No setup with a tested edge fired today.")
    headline = (f"{day}: S&P {spy['change']:+.1f}%, {regime.get('trend', 'mixed')}, "
                f"volatility {regime.get('vol_state', 'unknown')}" if spy and spy["change"] is not None
                else f"{day}: market study updated")
    if edge:
        headline += f"; {sum(len(s['symbols']) for s in edge)} tested setup(s)"
    if today_alerts:
        headline += f"; {len(today_alerts)} alert(s)"
    return headline + ".", "\n".join(lines)
