/**
 * The Accounts view: the fleet's Alpaca paper accounts and the user's Webull
 * and Tradier accounts, from /api/accounts.
 *
 * Read-only, like the endpoint: there is no button here that reaches a
 * broker. accounts.py already held every symbol and name to a pattern; this
 * file only lays the numbers out, through textContent, and a number we do
 * not have is "—", never 0.
 */
import { getAccounts, type BrokerAccount, type BrokerPosition, type AccountsStatus } from "./api";
import { el, panel, readout, row, pill, emptyState, stack, type Tone } from "./ui";

let status: AccountsStatus | null | undefined;      // undefined = not fetched yet
let fetchError = "";
let started = false;

const PROBLEM: Record<string, string> = {
  refused: "the broker refused the keys",
  unreachable: "couldn't reach the broker",
  no_key: "no key",
  no_sdk: "Webull SDK not installed",
};

function money(v: number | null | undefined, signed = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  const text = "$" + abs.toLocaleString(undefined,
    { maximumFractionDigits: abs >= 100 ? 0 : 2, minimumFractionDigits: abs >= 100 ? 0 : 2 });
  return v < 0 ? "−" + text : signed ? "+" + text : text;
}

function pct(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "—"
    : `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(1)}%`;
}

function tone(v: number | null | undefined): Tone | undefined {
  return v === null || v === undefined || v === 0 ? undefined : v > 0 ? "ok" : "bad";
}

function who(a: BrokerAccount): string {
  return a.broker === "alpaca" ? a.bot : a.broker === "webull" ? "Webull" : "Tradier";
}

function positionRow(p: BrokerPosition, alpaca: boolean): HTMLElement {
  const r = row({ tone: tone(p.pl) });
  r.setTitle(`${p.qty ?? "—"} ${p.instrument}${p.side === "short" ? " short" : ""}`);
  r.setSub(`avg ${money(p.avg_price)} · now ${money(p.price)}`);
  r.addMeta(money(p.value), { w: "10ch" });
  r.addMeta(`${money(p.pl, true)} (${pct(p.pl_pct)})`, { w: "18ch" });
  if (alpaca) {
    r.addTrail(p.stop === null
      ? pill("no stop", "warn")
      : pill(p.stop === "trailing" ? "trailing stop" : `stop ${p.stop}`, "ok", "ghost"));
  }
  return r.root;
}

function accountPanel(a: BrokerAccount): HTMLElement {
  const meta = [a.account, a.broker === "alpaca" ? `slot ${a.slot}` : "real money"]
    .filter(Boolean).join(" · ");
  const p = panel(who(a), { meta });
  if (a.status !== "ok") {
    p.body.append(emptyState(PROBLEM[a.status] ?? a.status, true));
    return p.root;
  }
  const grid = el("div", "debate-readouts");
  grid.append(
    readout("equity", money(a.equity)).root,
    readout("today", money(a.day_pl, true), { tone: tone(a.day_pl) }).root,
    readout("cash", money(a.cash)).root,
    readout("positions", String(a.positions.length)).root,
  );
  p.body.append(grid);
  if (a.positions.length) {
    const list = stack(true);
    for (const pos of a.positions) list.append(positionRow(pos, a.broker !== "webull"));
    p.body.append(list);
  }
  return p.root;
}

function summary(s: AccountsStatus): HTMLElement {
  const fleet = s.accounts.filter((a) => a.broker === "alpaca" && a.status === "ok");
  const total = fleet.reduce((t, a) => t + (a.equity ?? 0), 0);
  const day = fleet.reduce((t, a) => t + (a.day_pl ?? 0), 0);
  const open = fleet.reduce((t, a) => t + a.positions.length, 0);
  const unstopped = fleet.reduce((t, a) => t + a.positions.filter((x) => x.stop === null).length, 0);
  const p = panel("Paper fleet", {
    meta: `read ${new Date(s.updated * 1000).toLocaleTimeString()} · read-only`,
  });
  const grid = el("div", "debate-readouts");
  grid.append(
    readout("accounts", `${fleet.length}`).root,
    readout("equity", money(total)).root,
    readout("today", money(day, true), { tone: tone(day) }).root,
    readout("open positions", String(open)).root,
    readout("without a stop", String(unstopped), { tone: unstopped ? "warn" : undefined }).root,
  );
  p.body.append(grid);
  return p.root;
}

function paint(): void {
  const root = document.getElementById("accounts-view");
  if (!root) return;
  if (status === undefined) {
    root.replaceChildren(emptyState("Reading the accounts…"));
    return;
  }
  if (status === null) {
    root.replaceChildren(emptyState(`Could not read /api/accounts${fetchError ? `: ${fetchError}` : ""}.`));
    return;
  }
  if (!status.enabled) {
    root.replaceChildren(emptyState("Account reading is switched off (JARVIS_ACCOUNTS=0)."));
    return;
  }
  const cards = el("div", "debate-cards");
  const own = status.accounts.filter((a) => a.broker !== "alpaca");
  for (const a of [...own, ...status.accounts.filter((x) => x.broker === "alpaca")]) {
    cards.append(accountPanel(a));
  }
  root.replaceChildren(summary(status), cards);
}

export async function refreshAccountsView(): Promise<void> {
  try {
    status = await getAccounts();
    fetchError = "";
  } catch (err) {
    console.error("[accounts] fetch failed", err);
    status = null;
    fetchError = err instanceof Error ? err.message : "";
  }
  paint();
}

export function initAccounts(): void {
  if (started) return;
  started = true;
  paint();
  void refreshAccountsView();
  // The server reads the brokers at most once a minute; poll on that.
  setInterval(() => void refreshAccountsView(), 60_000);
}
