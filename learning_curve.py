"""Is JARVIS actually learning? The control group's answer, week by week.

A lesson can look useful just by being attached to work that would have gone
well anyway. So `spawn_run` holds lessons back from a random share of the
runs that HAD relevant lessons (the `holdout` arm) and gives them to the rest
(`treated`). Both arms were eligible for the same advice; the only
difference between them is whether they got it. Runs with nothing relevant
(`none`) are counted in the weekly totals and left out of the comparison.

`lift` is the treated success rate minus the holdout one, with a 95%
interval. The verdict only says "helping" when the whole interval is above
zero, and says nothing at all until each arm has enough decided runs —
a learning curve that announces progress on six runs is a learning curve
nobody should believe.

Pure functions over rows from learning_store: no I/O, no clock of its own.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

MIN_PER_ARM = 20          # decided runs in each arm before any verdict
WEEKS = 12
Z95 = 1.96


def _rate(ok: int, n: int) -> float | None:
    return ok / n if n else None


def compare(treated_ok: int, treated_n: int, holdout_ok: int, holdout_n: int,
            min_per_arm: int = MIN_PER_ARM) -> dict:
    """Difference of two success rates, its 95% interval, and a verdict."""
    t, h = _rate(treated_ok, treated_n), _rate(holdout_ok, holdout_n)
    out = {"treated": {"runs": treated_n, "succeeded": treated_ok, "rate": t},
           "holdout": {"runs": holdout_n, "succeeded": holdout_ok, "rate": h},
           "lift": None, "low": None, "high": None, "verdict": "not_enough_data",
           "needed": max(0, min_per_arm - treated_n) + max(0, min_per_arm - holdout_n)}
    if t is None or h is None:
        return out
    lift = t - h
    se = math.sqrt(t * (1 - t) / treated_n + h * (1 - h) / holdout_n)
    # Both arms at 0% or 100% leave no variance to measure; widen to the
    # resolution one run would give rather than claim a zero-width interval.
    se = max(se, 1 / math.sqrt(treated_n * holdout_n))
    out.update(lift=round(lift, 4), low=round(lift - Z95 * se, 4),
               high=round(lift + Z95 * se, 4))
    if treated_n < min_per_arm or holdout_n < min_per_arm:
        return out
    out["needed"] = 0
    if out["low"] > 0:
        out["verdict"] = "helping"
    elif out["high"] < 0:
        out["verdict"] = "hurting"
    else:
        out["verdict"] = "no_measurable_effect"
    return out


def week_start(ts: float) -> datetime:
    """Monday 00:00 of the local week `ts` falls in."""
    d = datetime.fromtimestamp(ts)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0,
                                                     microsecond=0)


def curve(runs: list[dict], skill_calls: list[dict], lessons: list[dict],
          now: float, weeks: int = WEEKS, holdout_rate: float = 0.0,
          min_per_arm: int = MIN_PER_ARM) -> dict:
    """The whole report: overall comparison plus one row per week, oldest first."""
    first = week_start(now) - timedelta(weeks=weeks - 1)
    starts = [first + timedelta(weeks=i) for i in range(weeks)]
    rows = {s: {"week": s.strftime("%Y-%m-%d"),
                "treated": [0, 0], "holdout": [0, 0], "none": [0, 0],
                "skill_calls": [0, 0], "learned": 0, "retired": 0}
            for s in starts}

    def row_for(ts):
        return rows.get(week_start(ts))

    overall = {"treated": [0, 0], "holdout": [0, 0], "none": [0, 0]}
    for run in runs:
        if run.get("outcome") not in ("success", "failure"):
            continue                       # pending, cancelled, or no verdict
        ok = int(run["outcome"] == "success")
        arm = run.get("arm")
        if arm not in overall:
            continue
        overall[arm][0] += ok
        overall[arm][1] += 1
        row = row_for(run["ts"])
        if row is not None:
            row[arm][0] += ok
            row[arm][1] += 1
    for call in skill_calls:
        row = row_for(call["ts"])
        if row is not None:
            row["skill_calls"][0] += int(bool(call["ok"]))
            row["skill_calls"][1] += 1
    for lesson in lessons:
        row = row_for(lesson["created_at"])
        if row is not None:
            row["learned"] += 1
        if lesson["status"] == "retired":
            row = row_for(lesson["updated_at"])
            if row is not None:
                row["retired"] += 1

    def finish(row):
        out = {"week": row["week"], "learned": row["learned"], "retired": row["retired"]}
        for key in ("treated", "holdout", "none", "skill_calls"):
            ok, n = row[key]
            out[key] = {"runs" if key != "skill_calls" else "calls": n,
                        "succeeded": ok, "rate": _rate(ok, n)}
        return out

    return {
        "holdout_rate": holdout_rate,
        "min_per_arm": min_per_arm,
        "overall": compare(overall["treated"][0], overall["treated"][1],
                           overall["holdout"][0], overall["holdout"][1], min_per_arm),
        "no_lessons": {"runs": overall["none"][1], "succeeded": overall["none"][0],
                       "rate": _rate(overall["none"][0], overall["none"][1])},
        "weeks": [finish(rows[s]) for s in starts],
    }


def say(report: dict) -> str:
    """One sentence a butler could say about it."""
    o = report["overall"]
    if report.get("holdout_rate", 0) <= 0:
        return ("I'm not holding any runs back, so I can't measure whether my "
                "lessons help; set JARVIS_LEARNING_HOLDOUT to start.")
    if o["verdict"] == "not_enough_data":
        return (f"Too early to tell whether my lessons help: "
                f"{o['treated']['runs']} runs with them and {o['holdout']['runs']} "
                f"held back so far, and I want {report['min_per_arm']} of each.")
    lift = f"{o['lift'] * 100:+.0f} points"
    span = f"{o['low'] * 100:+.0f} to {o['high'] * 100:+.0f}"
    rates = (f"{o['treated']['rate']:.0%} of runs with lessons succeeded against "
             f"{o['holdout']['rate']:.0%} without")
    if o["verdict"] == "helping":
        return f"My lessons are helping: {rates}, {lift} ({span})."
    if o["verdict"] == "hurting":
        return f"My lessons are hurting: {rates}, {lift} ({span}). Retire some."
    return f"No measurable effect yet: {rates}, {lift}, but anywhere from {span}."
