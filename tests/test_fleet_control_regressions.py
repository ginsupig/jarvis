import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
import autopilot
import fleet
import market


def test_unknown_regime_does_not_resume_a_lesson_pause():
    state={'paused':{'aegis':{'day':'2026-11-12','rule':'lesson','command_id':3}}}
    commands={'aegis':{'id':3,'action':'pause','reason':autopilot.REASON_PREFIX+'lesson'}}
    assert autopilot.decide({'accounts':[]},{'day':'2026-11-13','is_open':True},state,commands,[],'unknown')==[]


def test_later_pause_from_same_controller_is_not_ours_to_resume():
    state={'paused':{'aegis':{'day':'2026-11-12','rule':'day_loss','command_id':3}}}
    commands={'aegis':{'id':4,'action':'pause','reason':autopilot.REASON_PREFIX+'new decision'}}
    assert autopilot.decide({'accounts':[]},{'day':'2026-11-13','is_open':True},state,commands,[],'trending_up')==[]


def test_export_declares_delayed_research_not_execution_fresh(monkeypatch,tmp_path):
    monkeypatch.setenv('JARVIS_ENV_EXPORT',str(tmp_path/'market.json'))
    monkeypatch.setattr(market,'get_state',lambda *a:{'as_of':'2026-11-13T17:00:00Z',
        'data_through':'2026-11-13T16:44:00Z','regime':{'trend':'uptrend'}})
    assert fleet.export_view()
    saved=json.loads((tmp_path/'market.json').read_text())
    assert saved['execution_fresh'] is False and saved['data_through']=='2026-11-13T16:44:00Z'


def test_entry_regime_wins_over_end_of_day_label(tmp_path,monkeypatch):
    path=tmp_path/'fleet.db'
    monkeypatch.setenv('JARVIS_FLEET_DB',str(path))
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE outcomes(bot,id,strategy,attribution,symbol,direction,opened,closed,net,return_bps,regime,environment)')
        stamp=datetime(2026,11,13,15,tzinfo=timezone.utc).timestamp()
        db.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('aegis','1','orb','native_context','SPY','long',stamp,stamp+3600,10,10,'trending_down','high'))
    fleet.init_db()
    with closing(market._connect()) as db,db:
        db.execute("INSERT INTO fleet_regime_days VALUES('2026-11-13','uptrend','calm')")
    fleet.sync()
    with closing(market._connect()) as db:
        row=db.execute('SELECT trend,vol_state FROM fleet_trades').fetchone()
    assert tuple(row)==('trending_down','high')


def test_resume_carries_original_pause_id_into_submission():
    state={'paused':{'aegis':{'day':'2026-11-12','rule':'day_loss','command_id':3}}}
    commands={'aegis':{'id':3,'action':'pause','reason':autopilot.REASON_PREFIX+'loss'}}
    actions=autopilot.decide({'accounts':[]},{'day':'2026-11-13','is_open':False},state,commands,[],'unknown')
    assert actions[0]['expected_command_id']==3


def test_changed_pause_between_decision_and_submit_is_not_resumed(tmp_path,monkeypatch):
    import sys
    (tmp_path/'core').mkdir()
    (tmp_path/'core'/'fleet_learning.py').write_text('')
    monkeypatch.setenv('JARVIS_AUTOPILOT_ROOT',str(tmp_path))
    monkeypatch.setenv('JARVIS_FLEET_WATCH_PYTHON',sys.executable)
    monkeypatch.setattr(autopilot,'last_commands',lambda:{'aegis':{
        'id':4,'action':'pause','reason':autopilot.REASON_PREFIX+'new loss'}})
    monkeypatch.setattr(autopilot.subprocess,'run',lambda *a,**k: (_ for _ in ()).throw(AssertionError('must not submit')))
    result=autopilot.submit('resume','aegis','old pause done',expected_command_id=3)
    assert result=={'ok':False,'reason':'pause ownership changed'}
