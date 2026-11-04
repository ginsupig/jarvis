/**
 * The Debate view: the hypothesis tournament between Claude and the local
 * model, from /api/debate.
 *
 * Every score was computed by debate.py; this file only lays them out. The
 * same two rules as the Market view: a number we do not have is "—", never
 * 0, and everything goes through textContent — statements and objections
 * are model-written text, and they are shown as text and nothing else.
 *
 * Growth is shown as each round's cumulative numbers, side by side, rather
 * than as a claim: survival should rise, and both Brier scores should fall.
 */
import {
  getDebate, startDebateRound,
  type DebateCard, type DebateHypothesis, type DebateStatus,
} from "./api";
import { el, panel, readout, row, pill, emptyState, button, stack, type Tone } from "./ui";

let status: DebateStatus | null | undefined;      // undefined = not fetched yet
let fetchError = "";
let started = false;

const LABEL: Record<string, string> = { claude: "Claude", local: "Local model" };

const STATUS_TONE: Record<string, Tone> = {
  confirmed: "ok", testing: "warn", retired: "bad", rejected: "dim",
  duplicate: "dim", invalid: "bad",
};

function share(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "—" : `${Math.round(v * 100)}%`;
}

function score(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(3);
}

function ago(epochSec: number | null): string {
  if (!epochSec) return "not this session";
  const s = Date.now() / 1000 - epochSec;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

// ── panels ──────────────────────────────────────────────────────────────────

function header(s: DebateStatus): HTMLElement {
  const p = panel("Hypothesis tournament");
  p.setMeta(`last round ${ago(s.last_round)} · ${s.tests_so_far} ideas tested · `
    + `next needs discovery t ${s.next_bar.toFixed(2)}`);
  const play = button(s.busy ? "Round running…" : "Play a round", () => {
    play.disabled = true;
    play.textContent = "Round running…";
    void startDebateRound().finally(() => setTimeout(() => void refreshDebateView(), 15_000));
  }, { quiet: true });
  play.disabled = s.busy || !s.enabled;
  play.title = "Both models propose and critique; uses two Claude calls on your subscription";
  p.head.append(play);

  if (!s.enabled) {
    p.body.append(emptyState("The tournament is switched off (JARVIS_DEBATE=0)."));
    return p.root;
  }
  p.body.append(el("p", "market-summary",
    "After each close, Claude and the local model each propose market rules and critique the "
    + "other's before anything is tested. Every rule then goes through the market study's own "
    + "test, with a bar that rises as more ideas are tried. Neither model is retrained: what "
    + "grows is the record each one is shown next round. Lower Brier is better."));
  if (s.last_error) p.body.append(el("p", "market-summary", `Last problems: ${s.last_error}`));
  return p.root;
}

function cardPanel(player: string, c: DebateCard | undefined, reference?: number): HTMLElement {
  const p = panel(LABEL[player] ?? player, {
    meta: c ? `${c.proposed} proposed · ${c.tested} tested` : "no rounds yet",
  });
  if (!c) {
    p.body.append(emptyState("Has not played yet.", true));
    return p.root;
  }
  const grid = el("div", "debate-readouts");
  grid.append(
    readout("confirmed", String(c.confirmed), { tone: c.confirmed ? "ok" : undefined }).root,
    readout("survival", share(c.survival)).root,
    readout("confidence Brier", score(c.brier_own),
      { tone: reference !== undefined && c.brier_own !== null && c.brier_own < reference ? "ok" : undefined }).root,
    readout("critic Brier", score(c.brier_critic),
      { tone: reference !== undefined && c.brier_critic !== null && c.brier_critic < reference ? "ok" : undefined }).root,
  );
  p.body.append(grid);
  const tally = [`${c.rejected} rejected`, `${c.testing} waiting`, `${c.retired} retired`,
    `${c.duplicates} duplicates`, `${c.invalid} invalid`, `${c.critiques} critiques`];
  if (c.forward_mean !== null) {
    tally.push(`forward ${c.forward_signals} signals, ${(c.forward_mean * 100).toFixed(2)}% mean`);
  }
  p.body.append(el("p", "market-summary", tally.join(" · ")));

  const list = stack(true);
  for (const h of c.history.slice(-10).reverse()) {
    const r = row({ dim: true });
    r.setTitle(`Round ${h.round}`);
    r.setSub(`${h.proposed} ideas · survival ${share(h.survival)} · `
      + `confidence ${score(h.brier_own)} · critic ${score(h.brier_critic)}`);
    list.append(r.root);
  }
  if (c.history.length) p.body.append(list);
  return p.root;
}

function hypothesisRow(h: DebateHypothesis): HTMLElement {
  const tone = STATUS_TONE[h.status];
  const r = row({ tone, dim: h.status === "rejected" || h.status === "duplicate" });
  r.setLead(pill(h.status, tone, h.status === "confirmed" ? "solid" : "ghost"));
  r.setTitle(h.name);
  r.setSub([h.rule, h.statement].filter(Boolean).join(" — "));
  r.addMeta(LABEL[h.proposer] ?? h.proposer, { w: "12ch" });
  r.addMeta(`said ${share(h.confidence)}`, { w: "9ch" });
  r.addMeta(`round ${h.round}`, { w: "9ch" });
  const d = h.stats.discovery;
  const note = [h.reason ? `${h.reason}.` : ""];
  if (d && d.n) note.push(`older: ${d.n} events, t ${d.t.toFixed(2)}${h.bar ? ` (needed ${h.bar.toFixed(2)})` : ""}`);
  if (h.critique) {
    note.push(`${LABEL[h.critique.critic] ?? h.critique.critic} gave it ${share(h.critique.p_pass)}`
      + ` (${h.critique.flaw}): ${h.critique.objection}`);
  }
  r.setNote(note.filter(Boolean).join("  ·  "));
  return r.root;
}

function hypothesesPanel(s: DebateStatus): HTMLElement {
  const p = panel("Hypotheses", { meta: s.hypotheses.length ? `last ${s.hypotheses.length}` : "" });
  if (!s.hypotheses.length) {
    p.body.append(emptyState("No hypotheses yet — the first round plays after a close.", true));
    return p.root;
  }
  const list = stack(true);
  for (const h of s.hypotheses) list.append(hypothesisRow(h));
  p.body.append(list);
  return p.root;
}

// ── paint ───────────────────────────────────────────────────────────────────

function paint(): void {
  const root = document.getElementById("debate-view");
  if (!root) return;
  if (status === undefined) {
    root.replaceChildren(emptyState("Reading the tournament…"));
    return;
  }
  if (status === null) {
    root.replaceChildren(emptyState(`Could not read /api/debate${fetchError ? `: ${fetchError}` : ""}.`));
    return;
  }
  const ref = status.scorecard.reference_brier;
  const cards = el("div", "debate-cards");
  cards.append(cardPanel("claude", status.scorecard.claude, ref),
    cardPanel("local", status.scorecard.local, ref));
  root.replaceChildren(header(status), cards, hypothesesPanel(status));
}

export async function refreshDebateView(): Promise<void> {
  try {
    status = await getDebate();
    fetchError = "";
  } catch (err) {
    console.error("[debate] fetch failed", err);
    status = null;
    fetchError = err instanceof Error ? err.message : "";
  }
  paint();
}

export function initDebate(): void {
  if (started) return;
  started = true;
  paint();
  void refreshDebateView();
  // A round is a minute of work once a day; a minute's poll is plenty.
  setInterval(() => void refreshDebateView(), 60_000);
}
