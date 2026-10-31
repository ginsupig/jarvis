/**
 * "Is it working?" — the control group's verdict and the weekly learning
 * curve, drawn on the Learning tab.
 *
 * The chart is two lines over the last twelve weeks: the success rate of runs
 * that were given their relevant lessons, and of runs that had lessons
 * withheld at random. If the lessons help, the first pulls away from the
 * second. A week with no decided runs in an arm is a gap in that line, never
 * a dip to zero — nothing was measured there.
 *
 * Hand-built SVG via createElementNS, every label through textContent: the
 * dashboard never parses markup. The headline sentence is JARVIS's own
 * (learning_curve.say), numbers only.
 */
import type { LearningCurve, CurveWeek, ArmStats } from "./api";
import { el, readout, setTone, type Tone } from "./ui";

const NS = "http://www.w3.org/2000/svg";
// Close to the width the panel actually renders at, so 11px text in the
// viewBox stays roughly 11px on screen instead of being scaled up with it.
const W = 1100;
const H = 260;
const PAD = { top: 16, right: 110, bottom: 28, left: 44 };

const SERIES = [
  { key: "treated" as const, label: "With lessons", cls: "curve-line--treated" },
  { key: "holdout" as const, label: "Held back", cls: "curve-line--holdout" },
];

function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K, attrs: Record<string, string | number> = {},
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

function points(n: number): string {
  return n === 1 ? "1 point" : `${n} points`;
}

function shortWeek(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function armLine(a: ArmStats): string {
  return a.runs === 0 ? "no runs" : `${pct(a.rate)} of ${a.runs}`;
}

const VERDICT_TONE: Record<LearningCurve["overall"]["verdict"], Tone> = {
  helping: "ok", hurting: "bad", no_measurable_effect: "idle", not_enough_data: "idle",
};

/** The panel body: verdict, readouts, chart, and the table behind it. */
export function paintCurve(holder: HTMLElement, report: LearningCurve): void {
  const o = report.overall;
  const verdict = el("div", "curve-verdict", report.said);
  setTone(verdict, VERDICT_TONE[o.verdict]);

  const stats = el("div", "curve-stats");
  const liftText = o.lift === null ? "—"
    : `${o.lift >= 0 ? "+" : "−"}${points(Math.abs(Math.round(o.lift * 100)))}`;
  stats.append(
    readout("Difference", liftText, { tone: VERDICT_TONE[o.verdict], size: "lg",
                                      align: "start" }).root,
    readout("With lessons", armLine(o.treated), { align: "start" }).root,
    readout("Held back", armLine(o.holdout), { align: "start" }).root,
  );
  if (o.low !== null && o.high !== null) {
    stats.append(readout("95% range",
      `${Math.round(o.low * 100)} to ${Math.round(o.high * 100)} pts`, { align: "start" }).root);
  }
  if (o.needed > 0) {
    stats.append(readout("Runs still needed", String(o.needed), { align: "start" }).root);
  }

  const note = el("div", "curve-note",
    report.holdout_rate > 0
      ? `About ${Math.round(report.holdout_rate * 100)}% of runs that have relevant lessons `
        + `are sent without them, at random. A verdict needs ${report.min_per_arm} decided `
        + `runs in each group.`
      : "The control group is off (JARVIS_LEARNING_HOLDOUT=0), so nothing is held back "
        + "and the difference cannot be measured.");

  holder.replaceChildren(verdict, stats, chart(report.weeks), note, table(report.weeks));
}

function chart(weeks: CurveWeek[]): HTMLElement {
  const wrap = el("div", "curve-chart");
  const legend = el("div", "curve-legend");
  for (const s of SERIES) {
    const item = el("span", "curve-legend-item");
    const swatch = el("span", `curve-swatch ${s.cls}`);
    item.append(swatch, document.createTextNode(s.label));
    legend.append(item);
  }

  const n = weeks.length;
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (rate: number) => PAD.top + (1 - rate) * plotH;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "curve-svg", role: "img",
                             focusable: "false" });
  const any = weeks.some((w) => w.treated.runs > 0 || w.holdout.runs > 0);
  svg.setAttribute("aria-label", any
    ? `Weekly run success rate, with lessons and held back, over ${n} weeks`
    : `No decided runs in the last ${n} weeks`);

  // Layers, bottom to top: grid and axes, lines, dots and labels, hit areas.
  const gridG = svgEl("g");
  const lineG = svgEl("g");
  const markG = svgEl("g");
  const hitG = svgEl("g");
  svg.append(gridG, lineG, markG, hitG);

  for (const tick of [0, 0.5, 1]) {                       // recessive grid, one axis
    gridG.append(svgEl("line", { x1: PAD.left, x2: W - PAD.right, y1: y(tick), y2: y(tick),
                               class: "curve-grid" }));
    const t = svgEl("text", { x: PAD.left - 8, y: y(tick) + 4, class: "curve-axis",
                              "text-anchor": "end" });
    t.textContent = `${tick * 100}%`;
    gridG.append(t);
  }
  for (const i of [0, Math.floor((n - 1) / 2), n - 1]) {
    if (i < 0 || i >= n) continue;
    const t = svgEl("text", { x: x(i), y: H - 8, class: "curve-axis", "text-anchor": "middle" });
    t.textContent = shortWeek(weeks[i].week);
    gridG.append(t);
  }

  for (const s of SERIES) {
    // Split into runs of consecutive measured weeks: a gap is a gap.
    let d = "";
    let pen = false;
    let last: { i: number; rate: number } | null = null;
    weeks.forEach((w, i) => {
      const rate = w[s.key].rate;
      if (rate === null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(rate).toFixed(1)}`;
      pen = true;
      last = { i, rate };
      markG.append(svgEl("circle", { cx: x(i), cy: y(rate), r: 4, class: `curve-dot ${s.cls}` }));
    });
    if (d) lineG.append(svgEl("path", { d, class: `curve-line ${s.cls}` }));
    const end = last as { i: number; rate: number } | null;
    if (end) {                                             // direct label at the line's end
      const t = svgEl("text", { x: x(end.i) + 10, y: y(end.rate) + 4, class: "curve-direct" });
      t.textContent = s.label;
      markG.append(t);
    }
  }

  // Hover: a crosshair and a tooltip per week, over a full-height hit column.
  const cross = svgEl("line", { y1: PAD.top, y2: H - PAD.bottom, class: "curve-cross" });
  cross.style.display = "none";
  gridG.append(cross);
  const tip = el("div", "curve-tip");
  tip.hidden = true;
  const slot = n <= 1 ? plotW : plotW / (n - 1);
  weeks.forEach((w, i) => {
    const hit = svgEl("rect", { x: x(i) - slot / 2, y: PAD.top, width: slot,
                                height: plotH, class: "curve-hit" });
    hit.addEventListener("mouseenter", () => {
      cross.setAttribute("x1", String(x(i)));
      cross.setAttribute("x2", String(x(i)));
      cross.style.display = "";
      tip.replaceChildren(
        el("div", "curve-tip-head", `Week of ${shortWeek(w.week)}`),
        el("div", undefined, `With lessons: ${armLine(w.treated)}`),
        el("div", undefined, `Held back: ${armLine(w.holdout)}`),
        el("div", undefined, `No lessons relevant: ${armLine(w.none)}`),
        el("div", undefined, `Learned ${w.learned} · retired ${w.retired}`));
      tip.hidden = false;
      // Beside the crosshair, on whichever side has room — never over the
      // line it is describing.
      const right = x(i) > W * 0.6;
      tip.style.left = `${(x(i) / W) * 100}%`;
      tip.classList.toggle("curve-tip--left", right);
    });
    hit.addEventListener("mouseleave", () => {
      cross.style.display = "none";
      tip.hidden = true;
    });
    hitG.append(hit);
  });

  wrap.append(legend, svg, tip);
  return wrap;
}

function table(weeks: CurveWeek[]): HTMLElement {
  const details = el("details", "curve-table");
  details.append(el("summary", undefined, "Show the numbers"));
  const t = el("table");
  const head = el("tr");
  for (const h of ["Week", "With lessons", "Held back", "No lessons relevant",
                   "Skill calls", "Learned", "Retired"]) {
    head.append(el("th", undefined, h));
  }
  t.append(head);
  for (const w of [...weeks].reverse()) {
    const r = el("tr");
    const calls = w.skill_calls.calls ? `${pct(w.skill_calls.rate)} of ${w.skill_calls.calls}` : "—";
    for (const cell of [shortWeek(w.week), armLine(w.treated), armLine(w.holdout),
                        armLine(w.none), calls, String(w.learned), String(w.retired)]) {
      r.append(el("td", undefined, cell));
    }
    t.append(r);
  }
  details.append(t);
  return details;
}
