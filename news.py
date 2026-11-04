"""Market news for JARVIS: headlines that move prices or shift the regime.

Sources, each used only when it can be:
  alpaca    Alpaca's news API (Benzinga's wire), on the market study's key
            (market.keys); per-symbol or the whole tape
  fed       the Federal Reserve's press releases (RSS, no key): FOMC
            statements, minutes, rate and balance-sheet decisions
  finnhub   Finnhub general/company news, when FINNHUB_API_KEY is set
  fmp       Financial Modeling Prep latest news, when FMP_API_KEY is set

Headlines are written by strangers, so they are never JARVIS's words: the
server wraps everything below the first line as untrusted text and the
`market_news` tool taints the turn. The first line is ours: counts, and how
many items touch the macro regime (MACRO_RE), computed here.

Read-only GETs, cached for CACHE_S; keys go in headers or query strings and
are never logged or said.

Env:
  JARVIS_NEWS        0 to switch it off (default on)
  FINNHUB_API_KEY, FMP_API_KEY   optional extra sources
"""
from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

import market

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
FINNHUB_URL = "https://finnhub.io/api/v1"
FMP_URL = "https://financialmodelingprep.com/stable"
CACHE_S = 300
MAX_ITEMS = 25
FED_KEEP = 5          # the Fed's newest releases always make the list
HEADLINE_MAX = 220

# Words that mark a headline as about the macro regime rather than one company.
MACRO_RE = re.compile(
    r"\b(fomc|federal reserve|the fed|fed's|powell|rate (cut|hike)s?|interest rates?|"
    r"inflation|cpi|pce|ppi|payrolls?|jobs report|unemployment|gdp|recession|"
    r"treasur(y|ies)|yields?|tariffs?|opec|oil prices?|shutdown|debt ceiling|"
    r"balance sheet|quantitative|stimulus|vix|volatility|sell-?off|rally)\b",
    re.IGNORECASE)


def enabled() -> bool:
    return os.getenv("JARVIS_NEWS", "1").strip().lower() not in ("0", "false", "no", "off")


def sources() -> dict[str, bool]:
    key, secret = market.keys()
    return {"alpaca": bool(key and secret), "fed": True,
            "finnhub": bool(os.getenv("FINNHUB_API_KEY", "").strip()),
            "fmp": bool(os.getenv("FMP_API_KEY", "").strip())}


def _clean(text) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = " ".join(text.split())
    return text[:HEADLINE_MAX].rstrip() + ("…" if len(text) > HEADLINE_MAX else "")


def _when(value) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
                  parsedate_to_datetime,
                  lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)):
        try:
            dt = parse(text)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, TypeError):
            continue
    return 0.0


def _item(source: str, when, headline, symbols=()) -> dict | None:
    headline = _clean(headline)
    if not headline:
        return None
    syms = sorted({s.upper() for s in symbols or () if market.SYMBOL_RE.match(str(s).upper())})
    return {"source": source, "time": _when(when), "headline": headline, "symbols": syms[:8],
            "macro": bool(MACRO_RE.search(headline))}


def from_alpaca(client: httpx.Client, symbol: str = "") -> list[dict]:
    key, secret = market.keys()
    if not (key and secret):
        return []
    params = {"limit": 30, "sort": "desc"}
    if symbol:
        params["symbols"] = symbol
    r = client.get(ALPACA_NEWS_URL, params=params,
                   headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    r.raise_for_status()
    return [i for n in (r.json().get("news") or [])
            if (i := _item("alpaca", n.get("created_at"), n.get("headline"), n.get("symbols")))]


def from_fed(client: httpx.Client, symbol: str = "") -> list[dict]:
    if symbol:
        return []
    r = client.get(FED_RSS_URL, headers={"User-Agent": "JARVIS news reader"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for node in root.iter("item"):
        it = _item("fed", node.findtext("pubDate"), node.findtext("title"))
        if it:
            it["macro"] = True
            out.append(it)
    return out[:15]


def from_finnhub(client: httpx.Client, symbol: str = "") -> list[dict]:
    token = os.getenv("FINNHUB_API_KEY", "").strip()
    if not token:
        return []
    if symbol:
        today = datetime.now(timezone.utc).date()
        r = client.get(f"{FINNHUB_URL}/company-news", params={
            "symbol": symbol, "from": today.fromordinal(today.toordinal() - 7).isoformat(),
            "to": today.isoformat(), "token": token})
    else:
        r = client.get(f"{FINNHUB_URL}/news", params={"category": "general", "token": token})
    r.raise_for_status()
    return [i for n in (r.json() or [])[:30]
            if (i := _item("finnhub", n.get("datetime"), n.get("headline"),
                           [n.get("related")] if n.get("related") else ()))]


def from_fmp(client: httpx.Client, symbol: str = "") -> list[dict]:
    token = os.getenv("FMP_API_KEY", "").strip()
    if not token:
        return []
    if symbol:
        r = client.get(f"{FMP_URL}/news/stock", params={"symbols": symbol, "limit": 30, "apikey": token})
    else:
        r = client.get(f"{FMP_URL}/news/general-latest", params={"page": 0, "limit": 30, "apikey": token})
    r.raise_for_status()
    body = r.json()
    return [i for n in (body if isinstance(body, list) else [])
            if (i := _item("fmp", n.get("publishedDate"), n.get("title"),
                           [n.get("symbol")] if n.get("symbol") else ()))]


READERS = {"alpaca": from_alpaca, "fed": from_fed, "finnhub": from_finnhub, "fmp": from_fmp}

_cache: dict[str, dict] = {}
_lock = threading.Lock()


def fetch(symbol: str = "", force: bool = False, client: httpx.Client | None = None) -> dict:
    """Newest first, de-duplicated by headline, with each source's outcome."""
    symbol = market.normalise_symbol(symbol)
    if symbol and not market.SYMBOL_RE.match(symbol):
        symbol = ""
    with _lock:
        hit = _cache.get(symbol)
        if hit and not force and time.time() - hit["updated"] < CACHE_S:
            return hit
        own = client is None
        client = client or httpx.Client(timeout=15, follow_redirects=True)
        items, status = [], {}
        try:
            for name, reader in READERS.items():
                if symbol and name == "fed":
                    continue
                if not sources()[name]:
                    status[name] = "no_key"
                    continue
                try:
                    got = reader(client, symbol)
                    items.extend(got)
                    status[name] = "ok"
                except (httpx.HTTPError, ValueError, ET.ParseError) as e:
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    status[name] = "refused" if code in (401, 403) else "failed"
        finally:
            if own:
                client.close()
        seen, unique = set(), []
        for it in sorted(items, key=lambda i: i["time"], reverse=True):
            k = it["headline"].lower()
            if k not in seen:
                seen.add(k)
                unique.append(it)
        fed = [i for i in unique if i["source"] == "fed"][:FED_KEEP]
        rest = [i for i in unique if i["source"] != "fed"][:MAX_ITEMS - len(fed)]
        result = {"updated": time.time(), "symbol": symbol, "status": status,
                  "items": sorted(fed + rest, key=lambda i: i["time"], reverse=True)}
        _cache[symbol] = result
        return result


def _age(t: float) -> str:
    if not t:
        return "?"
    s = max(0, time.time() - t)
    return f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 172800 else f"{int(s // 86400)}d"


def describe(symbol: str = "") -> tuple[str, str]:
    """(our summary line, the headlines as foreign text)."""
    if not enabled():
        return "Market news is switched off (JARVIS_NEWS=0).", ""
    got = fetch(symbol)
    items = got["items"]
    working = [n for n, s in got["status"].items() if s == "ok"]
    broken = [n for n, s in got["status"].items() if s in ("refused", "failed")]
    subject = f"news on {got['symbol']}" if got["symbol"] else "market news"
    head = (f"{len(items)} items of {subject} from {', '.join(working) or 'no source'}; "
            f"{sum(i['macro'] for i in items)} touch the macro regime.")
    if broken:
        head += " Couldn't read: " + ", ".join(broken) + "."
    lines = [f"[{i['source']} {_age(i['time'])} ago{' · macro' if i['macro'] else ''}"
             f"{' · ' + ','.join(i['symbols']) if i['symbols'] else ''}] {i['headline']}"
             for i in items]
    return head, "\n".join(lines)
