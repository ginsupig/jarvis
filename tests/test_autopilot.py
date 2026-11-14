"""The autopilot: fixed rules, pause/resume only, undoes only its own pauses."""
import autopilot
import brain
import jarvis_mcp
import server

OPEN = {"is_open": True, "day": "2026-11-03"}
CLOSED = {"is_open": False, "day": "2026-11-03"}


def acct(bot, equity=100_000.0, day_pl=0.0):
    return {"broker": "alpaca", "status": "ok", "bot": bot, "equity": equity,
            "day_pl": day_pl, "positions": []}


def snap(*rows):
    return {"accounts": list(rows)}


def mine(action="pause"):
    return {"id": 1, "action": action, "reason": autopilot.REASON_PREFIX + "x"}


def test_day_loss_pauses_one_bot_and_nothing_else():
    acts = autopilot.decide(snap(acct("ebt", day_pl=-2500), acct("aegis", day_pl=-100)),
                            OPEN, {}, {}, [], "up")
    assert acts == [{"action": "pause", "bot": "ebt", "rule": "day_loss",
                     "reason": "down 2.50% today"}]


def test_fleet_loss_pauses_everything_once():
    rows = [acct(b, day_pl=-1600) for b in ("ebt", "hqc", "aegis")]
    acts = autopilot.decide(snap(*rows), OPEN, {}, {}, [], "up")
    assert [a["bot"] for a in acts] == ["all"]
    state = {"paused": {"all": {"day": OPEN["day"], "rule": "fleet_loss"}}}
    assert autopilot.decide(snap(*rows), OPEN, state, {"all": mine()}, [], "up") == []


def test_confirmed_losing_lesson_pauses_only_in_its_market():
    lesson = [{"bot": "aegis", "trend": "down", "status": "confirmed", "sign": -1}]
    assert autopilot.decide(snap(acct("aegis")), OPEN, {}, {}, lesson, "down")[0]["rule"] == "lesson"
    assert autopilot.decide(snap(acct("aegis")), OPEN, {}, {}, lesson, "up") == []
    testing = [dict(lesson[0], status="testing")]
    assert autopilot.decide(snap(acct("aegis")), OPEN, {}, {}, testing, "down") == []


def test_next_day_resumes_its_own_pause_but_not_someone_elses():
    state = {"paused": {"ebt": {"day": "2026-11-02", "rule": "day_loss"},
                        "hqc": {"day": "2026-11-02", "rule": "day_loss"}}}
    commands = {"ebt": mine(), "hqc": {"id": 2, "action": "pause", "reason": "user: manual"}}
    acts = autopilot.decide(snap(), CLOSED, state, commands, [], "up")
    assert [(a["action"], a["bot"]) for a in acts] == [("resume", "ebt")]


def test_lesson_pause_lifts_when_the_market_changes():
    state = {"paused": {"aegis": {"day": "2026-11-01", "rule": "lesson"}}}
    lesson = [{"bot": "aegis", "trend": "down", "status": "confirmed", "sign": -1}]
    assert autopilot.decide(snap(acct("aegis")), OPEN, state, {"aegis": mine()}, lesson, "down") == []
    acts = autopilot.decide(snap(acct("aegis")), OPEN, state, {"aegis": mine()}, lesson, "up")
    assert acts[0]["action"] == "resume"


def test_closed_market_takes_no_new_action():
    assert autopilot.decide(snap(acct("ebt", day_pl=-9000)), CLOSED, {}, {}, [], "up") == []


def test_tick_records_state_and_only_ever_submits_pause_or_resume(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTOPILOT", "1")
    monkeypatch.setattr(autopilot, "last_commands", lambda: {})
    sent = []

    def submitter(action, bot, reason):
        sent.append((action, bot, reason))
        return {"ok": True, "command_id": 7}
    said = autopilot.tick(snap(acct("ebt", day_pl=-3000), acct("aegis", equity=900_000)),
                          OPEN, [], "up", submitter)
    assert sent == [("pause", "ebt", "down 3.00% today")]
    assert said == ["Requested pause of new entries for Ebt: down 3.00% today. Awaiting fleet acknowledgement; exits and stops keep working."]
    assert autopilot.load_state()["paused"]["ebt"]["rule"] == "day_loss"
    assert "ebt (down 3.00% today)" in autopilot.describe()


def test_submit_refuses_anything_but_pause_and_resume():
    import pytest
    with pytest.raises(ValueError):
        autopilot.submit("budget_cap", "ebt", "x")
    with pytest.raises(ValueError):
        autopilot.submit("pause", "rm -rf /", "x")
    assert autopilot.submit("pause", "ebt", "x") == {"ok": False, "reason": "MegaEmpire not found"}


def test_switched_off_does_nothing():
    assert autopilot.tick(snap(acct("ebt", day_pl=-9000)), OPEN, [], "up",
                          lambda *a: {"ok": True}) == []


def test_tools_are_wired():
    for name in ("fleet_autopilot", "fleet_study", "fleet_study_now"):
        assert name in server.TOOL_HANDLERS
        assert "mcp__jarvis__" + name in brain.ALLOWED_TOOLS
        assert name in {t["name"] for t in jarvis_mcp.TOOL_SPECS}
    assert "fleet_study_now" in server.ACTING_TOOLS
    assert "fleet_autopilot" not in server.ACTING_TOOLS
