"""MegaEmpire: the trading bots JARVIS studies, and never touches.

MegaEmpire (its brain calls itself MegaMind) supervises a roster of trading
bots — ORB, VWAP rejection, mean reversion, alpha signals — each appending
every closed trade to its own `outcomes.jsonl`, and serves its own state on
a small HTTP dashboard (default `http://127.0.0.1:8765`). JARVIS reads both:

- `JARVIS_EMPIRE_URL` — MegaMind's dashboard. `/api/empire` is the live
  picture (bots, regime, today's R, cooldowns), `/api/performance` its own
  aggregate stats.
- `JARVIS_EMPIRE_OUTCOMES` — the bots' `outcomes.jsonl` files, which is where
  the per-trade detail lives and so where the studying happens. Comma
  separated; each entry a file or a directory (searched for
  `outcomes.jsonl`), optionally named: `fable=C:/fable/data`.

READ-ONLY, by construction. Nothing in this module can place an order,
change a risk setting or answer one of MegaMind's `/approve` prompts: it
issues GETs and opens files for reading, and MegaMind exposes no write route
on that dashboard anyway. Trading stays MegaMind's job, under its own
governor and risk engine; JARVIS's job is to notice what the record says.

The statistics here are deliberately plain — expectancy in R, win rate,
profit factor — and every "finding" is a comparison a person can check by
hand: a strategy's expectancy under one condition against the same
strategy's expectancy overall, with the sample size beside it. No model is
involved; learning.py decides what to do with them.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

TIMEOUT_SEC = 5.0
MIN_SAMPLES = 10          # trades under one condition before it can be a finding
MIN_EDGE_R = 0.25         # how far from the strategy's own baseline, in R
MIN_T = 1.5               # and how far in standard errors
OUTCOMES_NAME = "outcomes.jsonl"
_READ_CHUNK = 4 * 2 ** 20

try:                                                    # US equity session time
    from zoneinfo import ZoneInfo
    _MARKET_TZ = ZoneInfo("America/New_York")
except Exception:                                      # pragma: no cover - no tzdata
    _MARKET_TZ = timezone(timedelta(hours=-5))


class EmpireError(Exception):
    """MegaMind could not be reached or said something unreadable."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class EmpireConfig:
    url: str = ""
    sources: list[tuple[str, Path]] = field(default_factory=list)  # (bot or "", path)

    @classmethod
    def from_env(cls) -> "EmpireConfig":
        url = (os.getenv("JARVIS_EMPIRE_URL") or "").strip().rstrip("/")
        sources = []
        for raw in (os.getenv("JARVIS_EMPIRE_OUTCOMES") or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            name, sep, rest = raw.partition("=")
            # "C:/fable/data" has no "=", and a Windows drive's ":" is not one.
            if sep and name and "/" not in name and "\\" not in name:
                sources.append((name.strip(), Path(rest.strip()).expanduser()))
            else:
                sources.append(("", Path(raw).expanduser()))
        return cls(url=url, sources=sources)

    @property
    def enabled(self) -> bool:
        return bool(self.url or self.sources)

    def outcome_files(self) -> list[tuple[str, Path]]:
        """Every `outcomes.jsonl` the configuration names, de-duplicated."""
        found, seen = [], set()
        for name, path in self.sources:
            candidates = [path] if path.is_file() else (
                sorted(path.rglob(OUTCOMES_NAME)) if path.is_dir() else [])
            for candidate in candidates:
                key = str(candidate.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append((name, candidate))
        return found


# ---------------------------------------------------------------------------
# The live picture, from MegaMind's own dashboard
# ---------------------------------------------------------------------------

async def fetch(config: EmpireConfig, path: str,
                transport: httpx.AsyncBaseTransport | None = None) -> dict:
    if not config.url:
        raise EmpireError("JARVIS_EMPIRE_URL is not set")
    try:
        async with httpx.AsyncClient(base_url=config.url, timeout=TIMEOUT_SEC,
                                     transport=transport, trust_env=False) as client:
            response = await client.get(path)
    except httpx.HTTPError as e:
        raise EmpireError(f"MegaMind at {config.url} is not answering "
                          f"({type(e).__name__})") from e
    if response.status_code != 200:
        raise EmpireError(f"MegaMind said HTTP {response.status_code} for {path}")
    try:
        body = response.json()
    except ValueError as e:
        raise EmpireError(f"MegaMind's {path} was not JSON") from e
    if not isinstance(body, dict):
        raise EmpireError(f"MegaMind's {path} was not an object")
    return body


# ---------------------------------------------------------------------------
# Trades, read incrementally from the bots' own logs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Trade:
    key: str            # stable identity, so a re-read never double counts
    ts: float
    bot: str
    strategy: str
    regime: str
    symbol: str
    pnl_R: float
    window: str


def _parse_ts(raw) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw) if raw < 1e11 else raw / 1000.0
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MARKET_TZ)   # MegaMind's bots log local session time
    return dt.timestamp()


def session_window(ts: float) -> str:
    """The part of the US session a trade closed in, in New York time."""
    t = datetime.fromtimestamp(ts, _MARKET_TZ)
    minutes = t.hour * 60 + t.minute
    if minutes < 10 * 60 + 30:
        return "open (9:30-10:30)"
    if minutes < 12 * 60:
        return "morning (10:30-12)"
    if minutes < 14 * 60:
        return "midday (12-2)"
    return "afternoon (2-close)"


def _label(value, fallback: str) -> str:
    text = " ".join(str(value or "").split())[:40]
    return text or fallback


def parse_trade(line: str, bot_hint: str, source: str, offset: int) -> Trade | None:
    """One `outcomes.jsonl` line, or None for anything that is not a closed
    trade with a usable R. Lenient on shape: the roster's bots do not all
    write the same optional fields."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    pnl = entry.get("pnl_R")
    if isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not math.isfinite(pnl):
        return None
    if abs(pnl) > 100:                 # MegaMind's own schema rejects these too
        return None
    if str(entry.get("status", "")).lower() == "cancelled":
        return None
    ts = _parse_ts(entry.get("timestamp"))
    if ts is None:
        return None
    bot = _label(entry.get("bot_name") or entry.get("bot") or bot_hint, "unknown")
    trade_id = entry.get("trade_id")
    key = f"{bot}:{trade_id}" if trade_id else f"{source}@{offset}"
    return Trade(key=key, ts=ts, bot=bot,
                 strategy=_label(entry.get("strategy"), "unknown"),
                 regime=_label(entry.get("regime"), "unknown"),
                 symbol=_label(entry.get("symbol"), "?"),
                 pnl_R=round(float(pnl), 4), window=session_window(ts))


def read_new(path: Path, bot_hint: str, offset: int, size_seen: int
             ) -> tuple[list[Trade], int, int]:
    """Trades appended since `offset`. Returns (trades, new offset, size).

    A file that is now SHORTER than last time was rotated or rewritten, so
    it is read from the start; trade keys keep that from double counting.
    A last line with no newline yet is left for next time — a bot may be
    halfway through writing it."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset, size_seen
    if size < size_seen or offset > size:
        offset = 0
    if size == offset:
        return [], offset, size
    hint = bot_hint or path.parent.name
    trades: list[Trade] = []
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(_READ_CHUNK)
    end = data.rfind(b"\n")
    if end == -1:
        return [], offset, size
    position = offset
    for raw in data[:end + 1].splitlines(keepends=True):
        line = raw.decode("utf-8", "replace").strip()
        if line:
            trade = parse_trade(line, hint, str(path), position)
            if trade is not None:
                trades.append(trade)
        position += len(raw)
    return trades, offset + end + 1, size


# ---------------------------------------------------------------------------
# Statistics and findings
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    total: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    sq: float = 0.0

    def add(self, r: float) -> None:
        self.n += 1
        self.total += r
        self.sq += r * r
        if r > 0:
            self.wins += 1
            self.gross_win += r
        else:
            self.gross_loss += -r

    @property
    def expectancy(self) -> float:
        return self.total / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def profit_factor(self) -> float | None:
        return self.gross_win / self.gross_loss if self.gross_loss else None

    @property
    def stdev(self) -> float:
        if self.n < 2:
            return 0.0
        var = (self.sq - self.n * self.expectancy ** 2) / (self.n - 1)
        return math.sqrt(max(var, 0.0))

    def to_dict(self) -> dict:
        pf = self.profit_factor
        return {"trades": self.n, "win_rate": round(self.win_rate, 3),
                "expectancy_R": round(self.expectancy, 3),
                "total_R": round(self.total, 2),
                "profit_factor": round(pf, 2) if pf is not None else None}


def tabulate(trades) -> dict:
    """By strategy, and by strategy under each regime and session window."""
    overall: dict[str, Stats] = defaultdict(Stats)
    by: dict[tuple[str, str, str], Stats] = defaultdict(Stats)
    for t in trades:
        overall[t.strategy].add(t.pnl_R)
        by[(t.strategy, "regime", t.regime)].add(t.pnl_R)
        by[(t.strategy, "window", t.window)].add(t.pnl_R)
    return {"overall": dict(overall), "by": dict(by)}


@dataclass(frozen=True)
class Finding:
    strategy: str
    dimension: str          # "regime" | "window"
    value: str
    n: int
    expectancy: float
    baseline: float
    baseline_n: int
    win_rate: float

    @property
    def key(self) -> str:
        return f"trading|{self.strategy}|{self.dimension}|{self.value}"

    @property
    def better(self) -> bool:
        return self.expectancy > self.baseline

    def title(self) -> str:
        where = (f"in a {self.value} regime" if self.dimension == "regime"
                 else f"in the {self.value} window")
        verb = "does better" if self.better else "does worse"
        return f"{self.strategy} {verb} {where}"

    def body(self) -> str:
        return (f"{self.expectancy:+.2f}R per trade over {self.n} trades "
                f"({self.win_rate:.0%} won), against {self.baseline:+.2f}R across "
                f"all {self.baseline_n} of its trades.")


def findings(table: dict, min_samples: int = MIN_SAMPLES,
             min_edge: float = MIN_EDGE_R, min_t: float = MIN_T) -> list[Finding]:
    """Conditions under which a strategy is clearly not its usual self."""
    out = []
    for (strategy, dimension, value), stats in table["by"].items():
        base = table["overall"].get(strategy)
        if base is None or stats.n < min_samples or base.n <= stats.n:
            continue
        diff = stats.expectancy - base.expectancy
        if abs(diff) < min_edge:
            continue
        se = stats.stdev / math.sqrt(stats.n) if stats.n else 0.0
        if se > 0 and abs(diff) / se < min_t:
            continue
        out.append(Finding(strategy, dimension, value, stats.n,
                           round(stats.expectancy, 3), round(base.expectancy, 3),
                           base.n, round(stats.win_rate, 3)))
    out.sort(key=lambda f: -abs(f.expectancy - f.baseline) * math.sqrt(f.n))
    return out
