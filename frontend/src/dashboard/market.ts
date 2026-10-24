/**
 * The Market view: what JARVIS's market study knows, from /api/market.
 *
 * Every number here was computed by market.py from price bars; this file
 * only lays them out. Two rules carried over from the rest of the dashboard:
 * never render a number we do not have (a missing value is "—", never 0),
 * and everything goes through textContent.
 *
 * The patterns panel is the point of the page. Most price patterns fail an
 * honest test, so "rejected" is drawn as the normal, quiet state and only
 * "confirmed" is drawn as something to look at.
 */
import {
  getMarket, refreshMarket, changeWatchlist,
  type MarketPattern, type MarketStatus, type MarketSymbol,
} from "./api";
import {
  el, panel, readout, row, pill, emptyState, button, stack, type Tone,
} from "./ui";

let status: MarketStatus | null | undefined;     // undefined = not fetched yet
let fetchError = "";
let started = false;

// ── formatting ──────────────────────────────────────────────────────────────

function pct(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined || !Number.isFinite(v)
    ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function num(v: number | null | undefined, digits = 1, unit = ""): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "—" : `${v.toFixed(digits)}${unit}`;
}

function signTone(v: number | null | undefined): Tone | undefined {
  if (v === null || v === undefined || Math.abs(v) < 0.05) return undefined;
  return v > 0 ? "ok" : "bad";
}

function ago(epochSec: number | null): string {
  if (!epochSec) return "never";
  const s = Date.now() / 1000 - epochSec;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

const TREND_TONE: Record<string, Tone> = {
  uptrend: "ok", recovering: "ok", pullback: "warn", rebounding: "warn",
  weak: "bad", downtrend: "bad", mixed: "warn",
};

const STATUS_TONE: Record<string, Tone> = {
  confirmed: "ok", testing: "warn", retired: "bad", rejected: "dim",
};

const FLAG_WORDS: Record<string, string> = {
  big_move: "big move", heavy_volume: "heavy volume", "52w_high": "52w high",
  "52w_low": "52w low", crossed_above_50d: "above 50d", crossed_below_50d: "below 50d",
  crossed_above_200d: "above 200d", crossed_below_200d: "below 200d",
};

// ── panels ──────────────────────────────────────────────────────────────────

function header(s: MarketStatus): HTMLElement {
  const p = panel("Market study");
  const snap = s.snapshot;
  const phase = s.phase === "open" ? "market open" : s.phase === "closed" ? "market closed" : s.phase;
  p.setMeta(`updated ${ago(s.last_tick)} · ${phase} · every ${s.interval_min} min while open · data 15 min delayed`);
  const refresh = button("Refresh now", () => {
    refresh.disabled = true;
    void refreshMarket().finally(() => setTimeout(() => void refreshMarketView(), 20_000));
  }, { quiet: true });
  p.head.append(refresh);

  if (!s.enabled) {
    p.body.append(emptyState("Market study is switched off (JARVIS_MARKET=0)."));
    return p.root;
  }
  if (!s.configured) {
    p.body.append(emptyState(
      "No market data keys. Add APCA_API_KEY_ID and APCA_API_SECRET_KEY (a free Alpaca account) to .env and restart JARVIS."));
    return p.root;
  }
  if (s.last_error) p.body.append(el("div", "market-error tone-bad", `Last cycle failed: ${s.last_error}`));
  if (!snap) {
    p.body.append(emptyState("The first pass over the market has not finished yet — it backfills five years."));
    return p.root;
  }
  const r = snap.regime;
  const figures = el("div", "market-figures");
  figures.append(
    readout("trend", r.trend ?? "—", { tone: TREND_TONE[r.trend ?? ""], align: "start" }).root,
    readout("S&P vs 200-day", pct(r.spy_vs_200d), { tone: signTone(r.spy_vs_200d), align: "start" }).root,
    readout("above 50-day", num(r.breadth_50d, 0, "%"), { align: "start" }).root,
    readout("volatility", r.vol_state ?? "—", {
      tone: r.vol_state === "stressed" ? "bad" : r.vol_state === "elevated" ? "warn" : undefined,
      align: "start",
    }).root,
    readout("20d vol", num(r.vol_20d, 0, "%"), { align: "start" }).root,
    readout("VIXY 5d", pct(r.vixy_5d), { tone: signTone(r.vixy_5d === null ? null : -(r.vixy_5d ?? 0)), align: "start" }).root,
  );
  p.body.append(figures, el("p", "market-summary", r.summary ?? ""));
  return p.root;
}

function briefPanel(s: MarketStatus): HTMLElement | null {
  const snap = s.snapshot;
  if (!snap) return null;
  const p = panel("Brief", { meta: `${snap.session.day} · ${snap.session.state === "open" ? "session in progress" : "after the close"}` });
  for (const line of snap.brief.split("\n")) p.body.append(el("p", "market-brief-line", line));
  return p.root;
}

function symbolRow(v: MarketSymbol, onRemove?: () => void): HTMLElement {
  const r = row({ tone: signTone(v.change) });
  r.setLead(pill(v.trend, TREND_TONE[v.trend], "ghost"));
  r.setTitle(v.name ? `${v.symbol} · ${v.name}` : v.symbol);
  r.addMeta(num(v.price, 2), { w: "8ch", strong: true });
  r.addMeta(pct(v.change), { w: "7ch", cls: signTone(v.change) ? `tone-${signTone(v.change)}` : "" });
  r.addMeta(`5d ${pct(v.ret_5d)}`, { w: "11ch" });
  r.addMeta(`20d ${pct(v.ret_20d)}`, { w: "12ch" });
  r.addMeta(`RS ${pct(v.rs_20d)}`, { w: "11ch" });
  r.addMeta(`RSI ${num(v.rsi14, 0)}`, { w: "7ch" });
  r.addMeta(`vol ${num(v.rvol, 1, "×")}`, { w: "8ch" });
  for (const f of v.flags) r.addTrail(pill(FLAG_WORDS[f] ?? f, f.includes("below") || f === "52w_low" ? "bad" : "accent"));
  if (onRemove) r.addTrail(button("×", onRemove, { quiet: true }));
  r.setNote(`${pct(v.vs_50d)} vs 50-day · ${pct(v.vs_200d)} vs 200-day · daily range ${num(v.atr_pct, 1, "%")} · `
    + `${pct(v.from_high)} from 52w high · today ${num(v.move_z === null ? null : Math.abs(v.move_z), 1, "×")} usual range`);
  return r.root;
}

function sectorsPanel(s: MarketStatus): HTMLElement | null {
  const snap = s.snapshot;
  if (!snap) return null;
  const p = panel("Sectors", { meta: "ranked by 20-day strength against the S&P" });
  const list = stack(true);
  for (const v of snap.sectors) list.append(symbolRow(v));
  p.body.append(list);
  const m = panel("Market", { meta: "indexes, bonds, gold, volatility" });
  const mlist = stack(true);
  for (const v of snap.market) mlist.append(symbolRow(v));
  m.body.append(mlist);
  const wrap = el("div", "market-pair");
  wrap.append(m.root, p.root);
  return wrap;
}

function watchlistPanel(s: MarketStatus): HTMLElement {
  const p = panel("Watchlist", { meta: `${s.watchlist.length} symbols` });
  const form = el("form", "market-add");
  const input = el("input");
  input.placeholder = "Add tickers, e.g. SHOP, UBER";
  input.setAttribute("aria-label", "Add tickers");
  const add = el("button", "", "Add");
  add.type = "submit";
  form.append(input, add);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const symbols = input.value.split(/[\s,]+/).filter(Boolean);
    if (!symbols.length) return;
    add.disabled = true;
    void changeWatchlist(symbols, []).then((res) => {
      input.value = res.refused.length ? `Not added: ${res.refused.join(", ")}` : "";
      void refreshMarketView();
      setTimeout(() => void refreshMarketView(), 30_000);     // the backfill lands shortly after
    }).finally(() => { add.disabled = false; });
  });
  p.body.append(form);

  const views = new Map((s.snapshot?.watchlist ?? []).map((v) => [v.symbol, v]));
  const list = stack(true);
  for (const sym of s.watchlist) {
    const remove = () => void changeWatchlist([], [sym]).then(() => void refreshMarketView());
    const v = views.get(sym);
    if (v) {
      list.append(symbolRow(v, remove));
    } else {
      const r = row({ dim: true });
      r.setTitle(sym);
      r.setSub("no data yet — fetched on the next pass");
      r.addTrail(button("×", remove, { quiet: true }));
      list.append(r.root);
    }
  }
  if (!s.watchlist.length) list.append(emptyState("Nothing on the watchlist.", true));
  p.body.append(list);
  return p.root;
}

function alertsPanel(s: MarketStatus): HTMLElement {
  const p = panel("Alerts", { meta: s.alerts.length ? `last ${s.alerts.length}` : "" });
  if (!s.alerts.length) {
    p.body.append(emptyState("Nothing unusual yet.", true));
    return p.root;
  }
  const list = stack(true);
  for (const a of s.alerts) {
    const r = row({ tone: a.kind.includes("below") || a.kind === "52w_low" ? "bad" : "accent" });
    r.setTitle(a.text);
    r.addMeta(a.day, { w: "11ch" });
    list.append(r.root);
  }
  p.body.append(list);
  return p.root;
}

function statLine(label: string, st?: { n: number; mean: number; t: number; hit: number }): string {
  if (!st || !st.n) return `${label}: none`;
  return `${label}: ${st.n} × ${pct(st.mean * 100, 2)} t ${st.t.toFixed(1)} · ${Math.round(st.hit * 100)}% won`;
}

function patternsPanel(s: MarketStatus): HTMLElement | null {
  const snap = s.snapshot;
  if (!snap) return null;
  const counts = new Map<string, number>();
  for (const pt of snap.patterns) counts.set(pt.status, (counts.get(pt.status) ?? 0) + 1);
  const p = panel("Patterns", {
    meta: [...counts].map(([k, n]) => `${n} ${k}`).join(" · "),
  });
  p.body.append(el("p", "market-summary",
    "Each pattern is measured net of costs against an ordinary day for the same symbol, "
    + "discovered on the older two-thirds of five years and checked on the newest third, "
    + "then tracked forward from the day it was first tested. Most fail; that is the expected result."));
  const firing = new Map(snap.setups.map((x) => [x.pattern, x.symbols]));
  const list = stack(true);
  for (const pt of snap.patterns) list.append(patternRow(pt, firing.get(pt.name)));
  p.body.append(list);
  return p.root;
}

function patternRow(pt: MarketPattern, firing?: string[]): HTMLElement {
  const r = row({ tone: STATUS_TONE[pt.status], dim: pt.status === "rejected" });
  r.setLead(pill(pt.status, STATUS_TONE[pt.status], pt.status === "confirmed" ? "solid" : "ghost"));
  r.setTitle(pt.label);
  r.setSub(`${pt.description}. ${pt.reason}.`);
  r.addMeta(`${pt.direction > 0 ? "long" : "short"} ${pt.horizon}d`, { w: "9ch" });
  if (firing?.length) r.addTrail(pill(`fired: ${firing.slice(0, 6).join(", ")}${firing.length > 6 ? "…" : ""}`, "accent"));
  const st = pt.stats;
  r.setNote([
    statLine("older", st.discovery), statLine("recent", st.holdout), statLine("forward", st.live),
    pt.pending_signals ? `${pt.pending_signals} open` : "",
  ].filter(Boolean).join("  ·  "));
  return r.root;
}

// ── paint ───────────────────────────────────────────────────────────────────

function paint(): void {
  const root = document.getElementById("market-view");
  if (!root) return;
  if (status === undefined) {
    root.replaceChildren(emptyState("Reading the market study…"));
    return;
  }
  if (status === null) {
    root.replaceChildren(emptyState(`Could not read /api/market${fetchError ? `: ${fetchError}` : ""}.`));
    return;
  }
  const parts = [header(status), briefPanel(status), patternsPanel(status),
    watchlistPanel(status), sectorsPanel(status), alertsPanel(status)];
  root.replaceChildren(...parts.filter((x): x is HTMLElement => x !== null));
}

export async function refreshMarketView(): Promise<void> {
  try {
    status = await getMarket();
    fetchError = "";
  } catch (err) {
    console.error("[market] fetch failed", err);
    status = null;
    fetchError = err instanceof Error ? err.message : "";
  }
  paint();
}

export function initMarket(): void {
  if (started) return;
  started = true;
  paint();
  void refreshMarketView();
  // The study runs on its own schedule with no socket; a minute is finer
  // than its fastest cadence, so the page is never more than one pass behind.
  setInterval(() => void refreshMarketView(), 60_000);
}
