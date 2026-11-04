"""The user's brokerage accounts, read-only: the paper fleet's Alpaca
accounts and their own Webull and Tradier accounts.

For each account JARVIS can say the balance, the day's change, every open
position with its profit, and whether it has a broker stop. That is all this
module can do: it only ever sends GET requests (Alpaca, Tradier) or calls the
SDK's account queries (Webull). There is no order code here, and no way to add one
through a tool.

The fleet's keys stay where MegaEmpire keeps them (the proxy's .env). They are
read at fetch time, used in request headers, and never copied, logged, or
returned. Which accounts to read comes from MegaEmpire's roster: every bot
holding a paper slot, under its key prefix.

Everything said back is a number computed here, a sentence this module wrote,
a bot name held to fleet.NAME_RE, an account number held to ACCOUNT_RE, or a
symbol held to the patterns below; anything else is "an instrument".

Env:
  JARVIS_ACCOUNTS          0 to switch it off (default on)
  JARVIS_ACCOUNTS_KEYS     the fleet's keys (default C:/Mega Empire proxy/.env)
  JARVIS_ACCOUNTS_ROSTER   MegaEmpire's roster (default C:/MegaEmpire/config/megamind.yaml)
  WEBULL_APP_KEY, WEBULL_APP_SECRET   the user's Webull OpenAPI app
  WEBULL_ACCOUNT_ID        optional; the first account the app sees otherwise
  WEBULL_REGION            default "us"
  TRADIER_TOKEN, TRADIER_ACCOUNT_ID   the user's Tradier account; when unset
                           here, read from JARVIS_TRADIER_KEYS (default
                           C:/MOM/.env, where the MOM bot keeps them; the
                           token may also be named TRADIER_ACCESS_TOKEN)
"""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

import data_paths
import fleet
import market

DEFAULT_KEYS = "C:/Mega Empire proxy/.env"
DEFAULT_ROSTER = "C:/MegaEmpire/config/megamind.yaml"
DEFAULT_TRADIER_KEYS = "C:/MOM/.env"
ALPACA_URL = "https://paper-api.alpaca.markets"
TRADIER_URL = "https://api.tradier.com/v1"
CACHE_S = 60

ACCOUNT_RE = re.compile(r"[A-Z0-9]{5,14}")
OPTION_RE = re.compile(r"([A-Z]{1,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})")
CRYPTO_RE = re.compile(r"([A-Z]{2,6})/?(USD|USDT|USDC)")
STOP_TYPES = ("stop", "stop_limit", "trailing_stop")

OK, REFUSED, UNREACHABLE, NO_KEY, NO_SDK = "ok", "refused", "unreachable", "no_key", "no_sdk"


def enabled() -> bool:
    return os.getenv("JARVIS_ACCOUNTS", "1").strip().lower() not in ("0", "false", "no", "off")


def keys_path() -> Path:
    return Path(os.getenv("JARVIS_ACCOUNTS_KEYS", DEFAULT_KEYS))


def roster_path() -> Path:
    return Path(os.getenv("JARVIS_ACCOUNTS_ROSTER", DEFAULT_ROSTER))


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def roster() -> list[dict]:
    """Every bot holding a paper slot: name, slot, and its key prefix."""
    import yaml
    try:
        data = yaml.safe_load(roster_path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    out = []
    for b in data.get("bots") or []:
        if not isinstance(b, dict) or b.get("paper_slot") in (None, ""):
            continue
        name = fleet.clean(str(b.get("name") or ""))
        prefix = str(b.get("proxy_env_prefix") or "")
        if name and re.fullmatch(r"[A-Z0-9_]{1,24}", prefix):
            out.append({"bot": name, "slot": int(b["paper_slot"]), "prefix": prefix})
    return sorted(out, key=lambda r: r["slot"])


# ── what may be said ─────────────────────────────────────────────────────────

def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _money(v) -> str:
    if v is None:
        return "unknown"
    text = f"${abs(v):,.0f}" if abs(v) >= 100 else f"${abs(v):,.2f}"
    return "−" + text if v < 0 else text


def _signed(v) -> str:
    return "unknown" if v is None else ("+" if v >= 0 else "") + _money(v)


def _strike(v) -> str:
    return "?" if v is None else f"{v:g}"


def instrument(symbol, option: dict | None = None) -> str:
    """A symbol we can say, or "an instrument". Options read as
    "META 600 call, Dec 18 2026"."""
    s = str(symbol or "").strip().upper()
    if option:
        if not market.SYMBOL_RE.match(s):
            return "an option"
        kind = "call" if str(option.get("type", "")).upper().startswith("C") else "put"
        exp = str(option.get("expiry") or "")
        exp = exp if re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp) else "unknown expiry"
        return f"{s} {_strike(_num(option.get('strike')))} {kind}, {exp}"
    if market.SYMBOL_RE.match(s):
        return s
    m = OPTION_RE.fullmatch(s)
    if m:
        root, yy, mm, dd, cp, k = m.groups()
        return instrument(root, {"type": cp, "expiry": f"20{yy}-{mm}-{dd}", "strike": int(k) / 1000})
    m = CRYPTO_RE.fullmatch(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return "an instrument"


# ── Alpaca ───────────────────────────────────────────────────────────────────

def _alpaca_get(client: httpx.Client, headers: dict, path: str, params: dict | None = None):
    r = client.get(ALPACA_URL + path, headers=headers, params=params or {})
    if r.status_code in (401, 403):
        raise PermissionError(r.status_code)
    r.raise_for_status()
    return r.json()


def _stops(orders: list) -> dict[str, float | str]:
    """Symbol → its resting stop (price, or "trailing"), legs included."""
    out: dict[str, float | str] = {}
    flat = []
    for o in orders or []:
        flat.append(o)
        flat.extend(o.get("legs") or [])
    for o in flat:
        if o.get("type") not in STOP_TYPES or o.get("status") in ("filled", "canceled", "expired"):
            continue
        sym = str(o.get("symbol") or "").upper()
        price = _num(o.get("stop_price"))
        out.setdefault(sym, price if price is not None else "trailing")
    return out


def alpaca_account(bot: dict, keys: dict, client: httpx.Client) -> dict:
    row = {"broker": "alpaca", "bot": bot["bot"], "slot": bot["slot"], "account": "",
           "status": NO_KEY, "positions": []}
    key, secret = keys.get(bot["prefix"] + "_KEY", ""), keys.get(bot["prefix"] + "_SECRET", "")
    if not (key and secret):
        return row
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    try:
        acct = _alpaca_get(client, headers, "/v2/account")
        positions = _alpaca_get(client, headers, "/v2/positions")
        orders = _alpaca_get(client, headers, "/v2/orders",
                             {"status": "open", "nested": "true", "limit": 500})
    except PermissionError:
        return {**row, "status": REFUSED}
    except (httpx.HTTPError, ValueError):
        return {**row, "status": UNREACHABLE}
    number = str(acct.get("account_number") or "")
    equity, last = _num(acct.get("equity")), _num(acct.get("last_equity"))
    stops = _stops(orders)
    held = []
    for p in positions or []:
        sym = str(p.get("symbol") or "").upper()
        qty = _num(p.get("qty"))
        held.append({
            "instrument": instrument(sym),
            "qty": None if qty is None else abs(qty),
            "side": "short" if (p.get("side") == "short" or (qty or 0) < 0) else "long",
            "avg_price": _num(p.get("avg_entry_price")),
            "price": _num(p.get("current_price")),
            "value": _num(p.get("market_value")),
            "pl": _num(p.get("unrealized_pl")),
            "pl_pct": _num(p.get("unrealized_plpc")),
            "stop": stops.get(sym),
        })
    return {**row, "status": OK, "account": number if ACCOUNT_RE.fullmatch(number) else "",
            "equity": equity, "cash": _num(acct.get("cash")),
            "day_pl": None if equity is None or last is None else equity - last,
            "open_orders": len(orders or []), "positions": held}


# ── Webull ───────────────────────────────────────────────────────────────────

class WebullAPI:
    """The SDK's three account queries, as parsed JSON. Its first call asks
    Webull for an API token; on a new app the user approves it in the Webull
    app, and the SDK waits up to two minutes for that."""

    def __init__(self, app_key: str, app_secret: str, region: str):
        from webull.core.client import ApiClient
        from webull.core.http.initializer.client_initializer import ClientInitializer
        from webull.trade.trade.v2.account_info_v2 import AccountV2

        client = ApiClient(app_key, app_secret, region, token_check_duration_seconds=120)
        token_dir = data_paths.data_dir() / "webull_token"
        token_dir.mkdir(parents=True, exist_ok=True)
        client.set_token_dir(str(token_dir))
        ClientInitializer.initializer(client)
        self._account = AccountV2(client)

    def accounts(self) -> list:
        return self._account.get_account_list().json()

    def balance(self, account_id: str) -> dict:
        return self._account.get_account_balance(account_id).json()

    def positions(self, account_id: str) -> list:
        return self._account.get_account_position(account_id).json()


_webull_api: WebullAPI | None = None
_webull_lock = threading.Lock()


def _webull_client():
    global _webull_api
    with _webull_lock:
        if _webull_api is None:
            _webull_api = WebullAPI(os.getenv("WEBULL_APP_KEY", "").strip(),
                                    os.getenv("WEBULL_APP_SECRET", "").strip(),
                                    os.getenv("WEBULL_REGION", "us").strip() or "us")
        return _webull_api


def webull_account(api=None) -> dict:
    row = {"broker": "webull", "bot": "", "slot": None, "account": "", "status": NO_KEY,
           "positions": []}
    if api is None:
        if not (os.getenv("WEBULL_APP_KEY", "").strip() and os.getenv("WEBULL_APP_SECRET", "").strip()):
            return row
        try:
            api = _webull_client()
        except ImportError:
            return {**row, "status": NO_SDK}
        except Exception:
            return {**row, "status": REFUSED}
    try:
        listed = api.accounts() or []
        wanted = os.getenv("WEBULL_ACCOUNT_ID", "").strip()
        acct = next((a for a in listed if a.get("account_id") == wanted), None) if wanted else None
        acct = acct or (listed[0] if listed else None)
        if acct is None:
            return {**row, "status": REFUSED}
        account_id = str(acct.get("account_id") or "")
        bal = api.balance(account_id) or {}
        positions = api.positions(account_id) or []
    except Exception:
        return {**row, "status": UNREACHABLE}
    held = []
    for p in positions:
        legs = p.get("legs") or []
        leg = legs[0] if len(legs) == 1 else None
        if str(p.get("instrument_type", "")).upper() == "OPTION" and leg:
            name = instrument(p.get("symbol"), {"type": leg.get("option_type"),
                                                "expiry": leg.get("option_expire_date"),
                                                "strike": leg.get("option_exercise_price")})
        elif len(legs) > 1:
            name = f"{instrument(p.get('symbol'))} option spread"
        else:
            name = instrument(p.get("symbol"))
        qty = _num(p.get("quantity"))
        held.append({
            "instrument": name,
            "qty": None if qty is None else abs(qty),
            "side": "short" if (qty or 0) < 0 else "long",
            "avg_price": _num(p.get("cost_price")),
            "price": _num(p.get("last_price")),
            "value": _num(p.get("market_value")),
            "pl": _num(p.get("unrealized_profit_loss")),
            "pl_pct": _num(p.get("unrealized_profit_loss_rate")),
            "stop": None,
        })
    number = str(acct.get("account_number") or "")
    return {**row, "status": OK, "account": number if ACCOUNT_RE.fullmatch(number) else "",
            "equity": _num(bal.get("total_net_liquidation_value")),
            "cash": _num(bal.get("total_cash_balance")),
            "day_pl": _num(bal.get("total_day_profit_loss")),
            "open_orders": None, "positions": held}


# ── Tradier ──────────────────────────────────────────────────────────────────

def tradier_keys() -> tuple[str, str]:
    token = os.getenv("TRADIER_TOKEN", "").strip() or os.getenv("TRADIER_ACCESS_TOKEN", "").strip()
    account = os.getenv("TRADIER_ACCOUNT_ID", "").strip()
    if token and account:
        return token, account
    keys = read_env(Path(os.getenv("JARVIS_TRADIER_KEYS", DEFAULT_TRADIER_KEYS)))
    return (keys.get("TRADIER_TOKEN") or keys.get("TRADIER_ACCESS_TOKEN") or "",
            keys.get("TRADIER_ACCOUNT_ID", ""))


def _listed(body: dict, outer: str, inner: str) -> list:
    """Tradier's lists: "null" when empty, a dict when there is one."""
    got = (body or {}).get(outer)
    got = got.get(inner) if isinstance(got, dict) else None
    return [got] if isinstance(got, dict) else got if isinstance(got, list) else []


def tradier_account(client: httpx.Client) -> dict:
    row = {"broker": "tradier", "bot": "", "slot": None, "account": "", "status": NO_KEY,
           "positions": []}
    token, account = tradier_keys()
    if not (token and account):
        return row
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(path: str, params: dict | None = None):
        r = client.get(TRADIER_URL + path, headers=headers, params=params or {})
        if r.status_code in (401, 403):
            raise PermissionError(r.status_code)
        r.raise_for_status()
        return r.json()

    try:
        bal = (get(f"/accounts/{account}/balances") or {}).get("balances") or {}
        positions = _listed(get(f"/accounts/{account}/positions"), "positions", "position")
        orders = _listed(get(f"/accounts/{account}/orders", {"filter": "open"}), "orders", "order")
        symbols = sorted({str(p.get("symbol") or "") for p in positions} - {""})
        quotes = {}
        if symbols:
            body = get("/markets/quotes", {"symbols": ",".join(symbols)})
            quotes = {q.get("symbol"): q for q in _listed(body, "quotes", "quote")}
    except PermissionError:
        return {**row, "status": REFUSED}
    except (httpx.HTTPError, ValueError):
        return {**row, "status": UNREACHABLE}
    stops = {str(o.get("symbol") or ""): _num(o.get("stop_price")) or "trailing"
             for o in orders if o.get("type") in ("stop", "stop_limit", "trailing_stop")}
    held = []
    for p in positions:
        sym = str(p.get("symbol") or "").upper()
        qty, cost = _num(p.get("quantity")), _num(p.get("cost_basis"))
        q = quotes.get(sym) or {}
        last = _num(q.get("last"))
        mult = 100 if OPTION_RE.fullmatch(sym) else 1
        value = None if qty is None or last is None else qty * last * mult
        pl = None if value is None or cost is None else value - cost
        held.append({
            "instrument": instrument(sym),
            "qty": None if qty is None else abs(qty),
            "side": "short" if (qty or 0) < 0 else "long",
            "avg_price": None if cost is None or not qty else cost / (qty * mult),
            "price": last, "value": value, "pl": pl,
            "pl_pct": None if pl is None or not cost else pl / abs(cost),
            "stop": stops.get(sym),
        })
    number = str(bal.get("account_number") or "")
    return {**row, "status": OK, "account": number if ACCOUNT_RE.fullmatch(number) else "",
            "equity": _num(bal.get("total_equity")), "cash": _num(bal.get("total_cash")),
            "day_pl": _num(bal.get("close_pl")), "open_orders": len(orders), "positions": held}


# ── the snapshot ─────────────────────────────────────────────────────────────

_cache: dict = {}
_lock = threading.Lock()


def snapshot(force: bool = False, client: httpx.Client | None = None, webull=None) -> dict:
    """Every account, fetched at most once a minute (all at once)."""
    with _lock:
        if not force and _cache and time.time() - _cache["updated"] < CACHE_S:
            return _cache
        if not enabled():
            return {"enabled": False, "updated": time.time(), "accounts": []}
        keys = read_env(keys_path())
        bots = roster()
        own = client is None
        client = client or httpx.Client(timeout=15)
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                wb = pool.submit(webull_account, webull)
                tr = pool.submit(tradier_account, client)
                rows = list(pool.map(lambda b: alpaca_account(b, keys, client), bots))
                rows += [wb.result(), tr.result()]
        finally:
            if own:
                client.close()
        _cache.clear()
        _cache.update({"enabled": True, "updated": time.time(), "accounts": rows})
        return _cache


def _position_line(p: dict, alpaca: bool) -> str:
    qty = p["qty"]
    size = "" if qty is None else f"{qty:g} "
    side = " short" if p["side"] == "short" else ""
    pct = "" if p["pl_pct"] is None else f" ({100 * p['pl_pct']:+.1f}%)".replace("-", "−")
    line = f"{size}{p['instrument']}{side}, worth {_money(p['value'])}, {_signed(p['pl'])}{pct}"
    if alpaca:
        stop = p["stop"]
        line += (", no broker stop" if stop is None else ", trailing stop" if stop == "trailing"
                 else f", stop {stop:g}")
    return line


def _who(a: dict) -> str:
    return a["bot"].capitalize() if a["broker"] == "alpaca" else a["broker"].capitalize()


def describe(bot: str = "") -> str:
    if not enabled():
        return "Account reading is switched off (JARVIS_ACCOUNTS=0)."
    snap = snapshot()
    rows = snap["accounts"]
    if bot:
        rows = [a for a in rows if a["bot"] == bot or (a["broker"] != "alpaca" and a["broker"] == bot)]
        if not rows:
            return f"No account holds a paper slot for {bot}."
    lines = []
    fleet_rows = [a for a in snap["accounts"] if a["broker"] == "alpaca" and a["status"] == OK]
    if not bot and fleet_rows:
        total = sum(a["equity"] or 0 for a in fleet_rows)
        day = sum(a["day_pl"] or 0 for a in fleet_rows)
        n = sum(len(a["positions"]) for a in fleet_rows)
        lines.append(f"The fleet: {len(fleet_rows)} paper accounts, {_money(total)} in all, "
                     f"{_signed(day)} today, {n} open positions.")
    for a in rows:
        who = _who(a)
        if a["status"] == NO_KEY:
            lines.append({
                "webull": "Webull isn't connected: add WEBULL_APP_KEY and WEBULL_APP_SECRET "
                          "to my .env and restart me.",
                "tradier": "Tradier isn't connected: no TRADIER_TOKEN and TRADIER_ACCOUNT_ID "
                           "in my .env or the MOM bot's.",
            }.get(a["broker"], f"{who} has no key in the fleet's .env."))
            continue
        if a["status"] == NO_SDK:
            lines.append("Webull keys are set but the Webull SDK isn't installed "
                         "(pip install webull-openapi-python-sdk).")
            continue
        if a["status"] == REFUSED:
            lines.append(f"{who}: the broker refused the keys." + (
                " If this is a new Webull app, approve the API login in the Webull app."
                if a["broker"] == "webull" else ""))
            continue
        if a["status"] == UNREACHABLE:
            lines.append(f"{who}: I couldn't reach the broker.")
            continue
        acct = f" ({a['account']})" if a["account"] else ""
        head = f"{who}{acct}: {_money(a['equity'])}, {_signed(a['day_pl'])} today"
        if not a["positions"]:
            lines.append(head + ", no positions.")
            continue
        lines.append(head + ":")
        lines.extend("  " + _position_line(p, a["broker"] != "webull") for p in a["positions"])
    return "\n".join(lines) or "No accounts to read."
