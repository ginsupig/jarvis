/**
 * The Learning view: what JARVIS has learned, what is waiting for the user,
 * and the two buttons that are the user's whole say over it — approve and
 * retire.
 *
 * Lesson titles and bodies were written by a local model out of failed runs'
 * output, and a run's output is text a stranger can influence. Same
 * discipline as Sessions and Memory: textContent only, never innerHTML. A
 * lesson the server flags as reading like an instruction is marked as such
 * and asks for a confirmation before it is approved, because approving it is
 * what puts it in front of unattended runs.
 *
 * No socket backs this: lessons change when a run finishes or the local
 * model has reflected, neither of which the dashboard hears about. It polls,
 * and re-fetches when the tab is opened and after every action.
 */
import {
  getLearning, approveLearning, retireLearning, ApiError,
  type LearningSnapshot, type LearnedLesson, type LearnedSkill,
} from "./api";
import { el, row, button, pill, emptyState, kv } from "./ui";

const POLL_MS = 30_000;

let started = false;
let acting = false;

function byId(id: string): HTMLElement | null {
  return document.getElementById(id);
}

function setMeta(id: string, count: number): void {
  const meta = byId(id);
  if (meta) meta.textContent = count > 0 ? String(count) : "";
}

function setBadge(count: number): void {
  const badge = byId("learning-tab-badge");
  if (!badge) return;
  badge.textContent = String(count);
  badge.hidden = count === 0;
}

function showBanner(text: string | null): void {
  const banner = byId("learning-banner");
  if (!banner) return;
  banner.hidden = text === null;
  banner.textContent = text ?? "";
}

/** The sentence the server answered with: the outcome of the last action. */
function say(text: string): void {
  const said = byId("learning-said");
  if (!said) return;
  said.hidden = text === "";
  said.textContent = text;
}

function setUnavailable(unavailable: boolean): void {
  const notice = byId("learning-unavailable");
  const body = byId("learning-body");
  if (notice) notice.hidden = !unavailable;
  if (body) body.hidden = unavailable;
}

async function act(action: "approve" | "retire", ref: string): Promise<void> {
  if (acting) return;
  acting = true;
  try {
    say(await (action === "approve" ? approveLearning(ref) : retireLearning(ref)));
  } catch (e) {
    console.error(`[learning] ${action} failed`, e);
    say(`Could not ${action} that — the server did not accept it.`);
  } finally {
    acting = false;
  }
  await reconcile();
}

function approveLesson(lesson: LearnedLesson): void {
  if (lesson.risky && !window.confirm(
    `Lesson ${lesson.id} reads like an instruction rather than advice:\n\n` +
    `${lesson.title}\n${lesson.body}\n\nApprove it anyway?`)) {
    return;
  }
  void act("approve", `lesson ${lesson.id}`);
}

function scopeLabel(scope: LearnedLesson["scope"]): string {
  if (scope === "brain") return "how JARVIS works";
  if (scope === "trading") return "the bots' trading";
  return "advice for runs";
}

function lessonRow(lesson: LearnedLesson, waiting: boolean): HTMLElement {
  const r = row({ tone: lesson.risky ? "bad" : waiting ? "accent" : undefined });
  r.setTitle(lesson.title);
  r.setSub(lesson.body);
  const merged = lesson.parents.length > 0
    ? ` · merges ${lesson.parents.map((p) => `#${p}`).join(", ")}` : "";
  r.setNote(`#${lesson.id} · ${scopeLabel(lesson.scope)} · from ${lesson.source}${merged}`);
  if (lesson.risky) r.addTrail(pill("reads like an instruction", "bad"));
  if (!waiting) {
    const uses = lesson.uses === 1 ? "1 use" : `${lesson.uses} uses`;
    const record = lesson.scope === "runs"
      ? `${Math.round(lesson.reliability * 100)}% over ${uses}`
      : lesson.scope === "trading" ? "from the record" : "in every conversation";
    r.addMeta(record, { cls: "learning-row-record" });
  }
  if (waiting) {
    r.addTrail(button("Approve", () => approveLesson(lesson), { tone: "ok" }));
  }
  r.addTrail(button(waiting ? "Reject" : "Retire",
    () => void act("retire", `lesson ${lesson.id}`), { quiet: true }));
  return r.root;
}

/** `waitingList` is the "Waiting For You" copy of a skill, which is the only
 * one that offers Approve: a skill whose v1 is in use and whose v2 waits
 * appears in both lists, and one button for one decision is enough. */
function skillRow(skill: LearnedSkill, waitingList: boolean): HTMLElement {
  const waiting = skill.status === "candidate";
  const green = skill.total > 0 && skill.passed === skill.total;
  const r = row({ tone: waiting ? "accent" : skill.status === "retired" ? "dim" : undefined,
                  dim: skill.status === "retired" });
  r.setTitle(skill.name);
  r.setSub(skill.description);
  const inUse = skill.active_version !== null ? `v${skill.active_version} in use` : "not in use";
  r.setNote(`${inUse} · latest v${skill.version} · ${skill.note}`);
  r.addMeta(`${skill.passed}/${skill.total} tests`, { cls: "learning-row-record" });
  if (skill.calls > 0) {
    r.addMeta(`${skill.calls - skill.call_failures}/${skill.calls} calls ok`,
              { cls: "learning-row-record" });
  }
  if (waitingList && waiting && green) {
    r.addTrail(button(`Approve v${skill.version}`,
      () => void act("approve", skill.name), { tone: "ok" }));
  }
  if (skill.active_version !== null || waiting) {
    r.addTrail(button("Retire", () => void act("retire", `skill ${skill.name}`),
                      { quiet: true }));
  }
  return r.root;
}

function paintState(snap: LearningSnapshot): void {
  const holder = byId("learning-state");
  if (!holder) return;
  const llm = snap.local_llm;
  const facts = kv();
  facts.add("Local model", llm.enabled ? `${llm.model} at ${llm.url}` :
    "not configured — set JARVIS_LOCAL_LLM_MODEL", llm.enabled ? "ok" : "warn");
  facts.add("Auto-apply", snap.auto_apply
    ? "on — run lessons go on trial without approval"
    : "off — nothing is used until you approve it");
  facts.add("Lessons", `${snap.lessons.active} in use · ${snap.lessons.candidate} waiting · ` +
                       `${snap.lessons.retired} retired`);
  const failures = snap.experiences.failure ?? 0;
  const pending = snap.experiences.unreflected_failures ?? 0;
  facts.add("Experiences", `${snap.experiences.success ?? 0} succeeded · ${failures} failed` +
                           (pending ? ` · ${pending} not yet reflected on` : ""),
            pending && !llm.enabled ? "warn" : undefined);
  const empire = snap.empire;
  if (empire?.configured) {
    const last = empire.last_study;
    facts.add("MegaEmpire", last
      ? `${empire.trades_studied} trades studied · ${last.findings} finding(s) · read-only`
      : "connected, not studied yet · read-only");
  }
  if (snap.busy.length) facts.add("Working on", snap.busy.join(", "), "accent");
  holder.replaceChildren(facts.root);
}

function paintPending(snap: LearningSnapshot): void {
  const list = byId("learning-pending-list");
  if (!list) return;
  list.replaceChildren();
  const waitingSkills = snap.skills.filter((s) => s.status === "candidate");
  const count = snap.lessons.candidate + waitingSkills.length;
  setMeta("learning-pending-meta", count);
  setBadge(count);
  if (snap.pending.length === 0 && waitingSkills.length === 0) {
    list.append(emptyState("Nothing waiting for you.", true));
    return;
  }
  if (snap.pending.length > 1) {
    const all = el("div", "learning-bulk");
    all.append(
      button("Approve all", () => void act("approve", "all pending"), { tone: "ok" }),
      el("span", "learning-hint",
         "Lessons that read like instructions are held back; approve those one by one."));
    list.append(all);
  }
  for (const lesson of snap.pending) list.append(lessonRow(lesson, true));
  for (const skill of waitingSkills) list.append(skillRow(skill, true));
}

function paintActive(lessons: LearnedLesson[]): void {
  const list = byId("learning-active-list");
  if (!list) return;
  list.replaceChildren();
  setMeta("learning-active-meta", lessons.length);
  if (lessons.length === 0) {
    list.append(emptyState("No lessons in use yet.", true));
    return;
  }
  for (const lesson of lessons) list.append(lessonRow(lesson, false));
}

function paintSkills(skills: LearnedSkill[]): void {
  const list = byId("learning-skills-list");
  if (!list) return;
  list.replaceChildren();
  // Everything but a brand-new skill nobody has approved yet: that one lives
  // only under "Waiting For You".
  const shown = skills.filter((s) => s.active_version !== null || s.status !== "candidate");
  setMeta("learning-skills-meta", shown.length);
  if (shown.length === 0) {
    list.append(emptyState("No skills in use. Ask JARVIS to learn one.", true));
    return;
  }
  for (const skill of shown) list.append(skillRow(skill, false));
}

function paintActivity(lines: string[]): void {
  const list = byId("learning-activity-list");
  if (!list) return;
  list.replaceChildren();
  if (lines.length === 0) {
    list.append(emptyState("Nothing since the server started.", true));
    return;
  }
  for (const line of lines) list.append(el("div", "learning-activity", line));
}

async function reconcile(): Promise<void> {
  let snap: LearningSnapshot;
  try {
    snap = await getLearning();
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      setUnavailable(true);
      showBanner(null);
      return;
    }
    console.error("[learning] reconcile failed", e);
    showBanner("Cannot reach the JARVIS server.");
    return;
  }
  setUnavailable(false);
  showBanner(null);
  paintState(snap);
  paintPending(snap);
  paintActive(snap.active);
  paintSkills(snap.skills);
  paintActivity(snap.activity);
}

export function initLearning(): void {
  if (started) return;
  started = true;
  void reconcile();
  setInterval(() => void reconcile(), POLL_MS);
}

export function refreshLearning(): void {
  void reconcile();
}
