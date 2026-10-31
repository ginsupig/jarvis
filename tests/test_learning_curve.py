"""The control group and the learning curve: are the lessons measured to help?

`learning_curve` is pure; the engine decides each run's arm; spawn_run must
send a held-back run its prompt untouched and never credit lessons it was not
given. A fixed RNG stands in for chance throughout."""

import importlib
import random
import time
from datetime import datetime, timedelta

import pytest

import learning
import learning_curve as lc
import learning_store as store


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JARVIS_LEARNING_HOLDOUT", raising=False)
    store.init_db()


class Off:
    enabled, model, base_url = False, "", ""


# --- the comparison -------------------------------------------------------------

def test_no_verdict_until_each_arm_has_enough_runs():
    out = lc.compare(19, 19, 0, 10)
    assert out["verdict"] == "not_enough_data" and out["needed"] == 1 + 10
    assert out["lift"] == pytest.approx(1.0)        # computed, just not believed


def test_a_clear_gap_is_helping_and_a_reversed_one_hurting():
    assert lc.compare(80, 100, 50, 100)["verdict"] == "helping"
    assert lc.compare(50, 100, 80, 100)["verdict"] == "hurting"


def test_a_small_gap_on_modest_samples_is_no_measurable_effect():
    out = lc.compare(14, 20, 12, 20)
    assert out["verdict"] == "no_measurable_effect"
    assert out["low"] < 0 < out["high"]


def test_identical_perfect_arms_do_not_claim_certainty():
    out = lc.compare(30, 30, 30, 30)
    assert out["lift"] == 0 and out["low"] < 0 < out["high"]


# --- the curve ----------------------------------------------------------------------

def _ts(weeks_ago: int, now: float) -> float:
    return (lc.week_start(now) - timedelta(weeks=weeks_ago) + timedelta(days=2)).timestamp()


def test_runs_land_in_their_week_and_undecided_ones_are_left_out():
    now = time.time()
    runs = [
        {"ts": _ts(0, now), "arm": "treated", "outcome": "success"},
        {"ts": _ts(0, now), "arm": "treated", "outcome": "failure"},
        {"ts": _ts(0, now), "arm": "holdout", "outcome": "failure"},
        {"ts": _ts(1, now), "arm": "treated", "outcome": "success"},
        {"ts": _ts(1, now), "arm": "none", "outcome": "success"},
        {"ts": _ts(1, now), "arm": "treated", "outcome": "neutral"},
        {"ts": _ts(1, now), "arm": "treated", "outcome": None},
        {"ts": _ts(30, now), "arm": "treated", "outcome": "success"},  # before the window
    ]
    report = lc.curve(runs, [], [], now, holdout_rate=0.2)
    this_week, last_week = report["weeks"][-1], report["weeks"][-2]
    assert this_week["treated"] == {"runs": 2, "succeeded": 1, "rate": 0.5}
    assert this_week["holdout"]["rate"] == 0.0
    assert last_week["treated"]["runs"] == 1 and last_week["none"]["runs"] == 1
    assert report["weeks"][0]["treated"]["rate"] is None            # a gap, not a zero
    assert report["overall"]["treated"]["runs"] == 4                # all-time, all decided
    assert report["no_lessons"]["runs"] == 1


def test_lessons_learned_and_retired_are_counted_by_week():
    now = time.time()
    lessons = [{"created_at": _ts(2, now), "updated_at": _ts(0, now), "status": "retired"},
               {"created_at": _ts(0, now), "updated_at": _ts(0, now), "status": "active"}]
    calls = [{"ts": _ts(0, now), "ok": 1}, {"ts": _ts(0, now), "ok": 0}]
    weeks = lc.curve([], calls, lessons, now)["weeks"]
    assert weeks[-3]["learned"] == 1 and weeks[-1]["learned"] == 1
    assert weeks[-1]["retired"] == 1
    assert weeks[-1]["skill_calls"] == {"calls": 2, "succeeded": 1, "rate": 0.5}


def test_what_jarvis_says():
    base = {"holdout_rate": 0.2, "min_per_arm": 20}
    assert "Too early" in lc.say({**base, "overall": lc.compare(3, 4, 1, 2)})
    assert lc.say({**base, "overall": lc.compare(80, 100, 50, 100)}).startswith(
        "My lessons are helping: 80% of runs with lessons succeeded against 50% without")
    assert "can't measure" in lc.say({**base, "holdout_rate": 0,
                                      "overall": lc.compare(0, 0, 0, 0)})


# --- choosing the arm ------------------------------------------------------------------

def test_only_runs_with_relevant_lessons_are_randomised():
    engine = learning.LearningEngine(Off(), holdout_rate=0.5, rng=random.Random(1))
    assert engine.choose_arm([]) == ("none", [])
    lessons = [{"id": 1}]
    arms = [engine.choose_arm(lessons)[0] for _ in range(400)]
    assert set(arms) == {"treated", "holdout"}
    assert 150 < arms.count("holdout") < 250
    assert all(engine.choose_arm(lessons)[1] in ([], lessons) for _ in range(20))


def test_holdout_rate_is_read_and_clamped(monkeypatch):
    assert learning.holdout_rate_from_env() == learning.DEFAULT_HOLDOUT
    for raw, want in (("0", 0.0), ("0.3", 0.3), ("0.9", 0.5), ("-1", 0.0), ("lots", 0.2)):
        monkeypatch.setenv("JARVIS_LEARNING_HOLDOUT", raw)
        assert learning.holdout_rate_from_env() == want, raw


def test_zero_holdout_gives_every_eligible_run_its_lessons():
    engine = learning.LearningEngine(Off(), holdout_rate=0.0, rng=random.Random(1))
    assert {engine.choose_arm([{"id": 1}])[0] for _ in range(50)} == {"treated"}


@pytest.mark.asyncio
async def test_a_finished_run_settles_its_arm_once():
    engine = learning.LearningEngine(Off(), holdout_rate=0.2)
    store.record_run_arm("r1", "holdout", 2)
    await engine.observe_run({"id": "r1", "status": "failed", "prompt": "x"})
    await engine.observe_run({"id": "r1", "status": "succeeded", "prompt": "x"})
    [row] = store.run_arms_since(0)
    assert (row["arm"], row["lessons"], row["outcome"]) == ("holdout", 2, "failure")


# --- spawn_run -------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "srv"))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    store.init_db()
    spawned = []

    class _Executor:
        async def spawn(self, prompt, name, path, origin, resume_from=None, model=None):
            spawned.append(prompt)
            return run_store.create_run(prompt, name, path, origin, resume_from)

    monkeypatch.setattr(server_module, "run_executor_instance", _Executor())
    monkeypatch.setattr(server_module, "cached_projects",
                        [{"name": "chitauri", "path": str(tmp_path / "chitauri")}])
    return server_module, spawned


@pytest.mark.asyncio
async def test_a_held_back_run_gets_its_prompt_untouched_and_credits_nothing(server,
                                                                            monkeypatch):
    srv, spawned = server
    lesson_id = store.add_lesson("runs", "Run the tests first", "Run pytest before "
                                 "editing python code", "python, pytest", "user", store.ACTIVE)
    monkeypatch.setattr(srv.learning_engine, "holdout_rate", 1.0)
    await srv.tool_spawn_run({"project": "chitauri", "prompt": "fix the pytest failures"})
    assert spawned == [srv.compose_run_prompt("fix the pytest failures")]
    assert store.get_lesson(lesson_id)["uses"] == 0
    [row] = store.run_arms_since(0)
    assert (row["arm"], row["lessons"]) == ("holdout", 1)


@pytest.mark.asyncio
async def test_a_treated_run_is_recorded_as_one(server, monkeypatch):
    srv, spawned = server
    store.add_lesson("runs", "Run the tests first", "Run pytest before editing python "
                     "code", "python, pytest", "user", store.ACTIVE)
    monkeypatch.setattr(srv.learning_engine, "holdout_rate", 0.0)
    await srv.tool_spawn_run({"project": "chitauri", "prompt": "fix the pytest failures"})
    await srv.tool_spawn_run({"project": "chitauri", "prompt": "rename the logo"})
    assert learning.RUNS_HEADER in spawned[0] and learning.RUNS_HEADER not in spawned[1]
    assert [(r["arm"], r["lessons"]) for r in store.run_arms_since(0)] == [
        ("treated", 1), ("none", 0)]


@pytest.mark.asyncio
async def test_the_api_and_learning_status_report_the_verdict(server):
    srv, _ = server
    from fastapi.testclient import TestClient
    now = time.time()
    for i in range(25):
        store.record_run_arm(f"t{i}", "treated", 1)
        store.settle_run_arm(f"t{i}", "success" if i < 22 else "failure")
        store.record_run_arm(f"h{i}", "holdout", 1)
        store.settle_run_arm(f"h{i}", "success" if i < 10 else "failure")
    with TestClient(srv.app) as client:
        body = client.get("/api/learning/curve").json()
    assert body["overall"]["verdict"] == "helping"
    assert len(body["weeks"]) == lc.WEEKS and body["said"].startswith("My lessons are helping")
    text = await srv.tool_learning_status({})
    assert "My lessons are helping: 88% of runs with lessons succeeded against 40% without" in text
