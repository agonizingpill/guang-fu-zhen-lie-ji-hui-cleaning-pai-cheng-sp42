"""End-to-end checks for the water-quality release gate.

Runs against an in-process FastAPI TestClient with an isolated SQLite database
and a fake Redis queue.  Exercises the state machine, the correction/handover
arbitration, and late-worker invalidation.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.mkstemp(suffix='.db')[1]
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACTS_DIR', tempfile.mkdtemp(prefix='wq-artifacts-'))
os.environ['SEED_DEMO'] = 'false'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
import src.app as app_module  # noqa: E402
from src.database import Session  # noqa: E402
from src.models import User, ArrayBlock, SoilingSurvey, WashMission  # noqa: E402
from src.operations_models import WastewaterRecord  # noqa: E402
from src.quality_models import WaterQualitySample  # noqa: E402
from src.auth import hash_password  # noqa: E402
from src import quality_model, quality_service, worker  # noqa: E402


class FakeQueue:
    def __init__(self): self.jobs = []
    def rpush(self, name, value): self.jobs.append((name, value))


queue = FakeQueue()
app_module.queue = queue
import src.quality_routes as quality_routes  # noqa: E402
quality_routes.queue = queue
import src.worker as worker_mod  # noqa: E402
worker_mod.queue = queue

client = TestClient(app_module.app)
PASS = []
FAIL = []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(('PASS ' if condition else 'FAIL ') + name + ('' if condition else f' -> {detail}'))


def seed():
    from src.database import Base, engine
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin', 'planner', 'viewer'):
            db.add(User(email=role + '@t', password=hash_password('pw'), role=role))
        block = ArrayBlock(code='B1', name='B1', sector='s', capacity_kw=100, slope=20, area_m2=500, water_liters=1000)
        db.add(block); db.flush()
        survey = SoilingSurvey(code='SV1', block_id=block.id, capacity_kw=100, slope=20, irradiance=800,
                               module_temp=30, rain_days=10, dust=2, observed_kw=70, state='closed')
        db.add(survey); db.flush()
        mission = WashMission(survey_id=survey.id, water_liters=100, available_liters=900, status='accepted',
                              verified_gain=10, post_kw=80, post_irradiance=800)
        db.add(mission); db.flush()
        db.add(WastewaterRecord(mission_id=mission.id, used_liters=100, recovered_liters=80, recovery_pct=80,
                                disposal_method='沉淀池', manifest='WW1', state='verified', verified_by='admin@t'))
        db.commit()


def token(email):
    r = client.post('/auth/login', json={'email': email, 'password': 'pw'})
    return {'Authorization': 'Bearer ' + r.json()['token']}


def run_job(index=-1):
    name, value = queue.jobs[index]
    env = json.loads(value)
    worker.classify_quality(int(env['id']), int(env['version']))


seed()
planner = token('planner@t'); admin = token('admin@t'); viewer = token('viewer@t')

# --- register ---------------------------------------------------------------
r = client.post('/wastewater/1/quality', json={'ph': 7.2, 'conductivity_us_cm': 600, 'turbidity_ntu': 20}, headers=planner)
check('planner registers sample', r.status_code == 200, r.text)
sample = r.json()
check('new sample is pending', sample['release_status'] == 'pending', sample)
check('handover blocked before classification', client.post('/wastewater/1/handover', json={'received_by': '环保王工', 'receipt': 'receipt-1'}, headers=planner).status_code == 409)
check('approval blocked while pending', client.post(f"/water-quality/{sample['id']}/approve", json={'version': 1, 'approval_basis': '先行放行理由'}, headers=admin).status_code == 409)

# --- low risk release -------------------------------------------------------
run_job()
r = client.get(f"/water-quality/{sample['id']}", headers=planner)
row = r.json()
check('low sample classified low', row['risk_level'] == 'low', row)
check('low sample is releasable', row['release_status'] == 'released', row)

# --- correction withdrawals release ----------------------------------------
r = client.put(f"/water-quality/{sample['id']}", json={'ph': 7.0, 'conductivity_us_cm': 3900, 'turbidity_ntu': 180}, headers=planner)
check('correction bumps version', r.json()['version'] == 2, r.text)
check('correction returns to pending', r.json()['release_status'] == 'pending', r.text)
check('correction stores prior version', r.json()['history'][0]['version'] == 1, r.json()['history'])

# late inference from old version must be rejected
with Session() as db:
    worker.classify_quality(sample['id'], 1)
row = client.get(f"/water-quality/{sample['id']}", headers=planner).json()
check('late inference on old version void', row['state'] == 'evaluating' and row['probability'] is None, row)

# --- high risk requires disposal basis --------------------------------------
with Session() as db:
    worker.classify_quality(sample['id'], 2)
row = client.get(f"/water-quality/{sample['id']}", headers=planner).json()
check('high-contamination sample flagged high', row['risk_level'] == 'high', row)
check('high sample needs disposal', row['release_status'] == 'disposal', row)
check('handover blocked for high risk', client.post('/wastewater/1/handover', json={'received_by': '环保王工', 'receipt': 'receipt-1'}, headers=planner).status_code == 409)
check('planner cannot approve', client.post(f"/water-quality/{sample['id']}/approve", json={'version': 2, 'approval_basis': '处置依据足够长'}, headers=planner).status_code == 403)
check('stale version approval rejected', client.post(f"/water-quality/{sample['id']}/approve", json={'version': 1, 'approval_basis': '处置依据足够长'}, headers=admin).status_code == 409)
r = client.post(f"/water-quality/{sample['id']}/approve", json={'version': 2, 'approval_basis': '实验室复检确认无污染，改排市政管网'}, headers=admin)
check('admin approval accepted', r.status_code == 200 and r.json()['release_status'] == 'released', r.text)

# --- handover freezes ---------------------------------------------------------
r = client.post('/wastewater/1/handover', json={'received_by': '环保王工', 'receipt': '沉淀池入场签收单A'}, headers=planner)
check('handover after approval', r.status_code == 200, r.text)
row = client.get(f"/water-quality/{sample['id']}", headers=planner).json()
check('handover freezes sample', row['release_status'] == 'handed_over', row)
check('frozen snapshot has prediction and approval', row['release_snapshot']['prediction']['risk_level'] == 'high' and row['release_snapshot']['approval']['approved_version'] == 2, row['release_snapshot'])
check('frozen decision records why it was released', row['release_snapshot']['release_decision'] == 'released' and '主管' in row['release_snapshot']['release_reason'], row['release_snapshot'])
check('correction after handover rejected', client.put(f"/water-quality/{sample['id']}", json={'ph': 7, 'conductivity_us_cm': 500, 'turbidity_ntu': 10}, headers=planner).status_code == 409)
check('second handover rejected', client.post('/wastewater/1/handover', json={'received_by': '环保王工', 'receipt': '沉淀池入场签收单A'}, headers=planner).status_code == 409)

# --- concurrent correction vs handover: first accepted wins -----------------
# Use a second verified wastewater record.
with Session() as db:
    m = WashMission.query if False else None
    sv=SoilingSurvey(code='SV2',block_id=1,capacity_kw=100,slope=20,irradiance=800,module_temp=30,rain_days=9,dust=2,observed_kw=70,state='closed')
    db.add(sv);db.flush()
    mm=WashMission(survey_id=sv.id, water_liters=50, available_liters=50, status='accepted', verified_gain=5)
    db.add(mm);db.flush()
    db.add(WastewaterRecord(mission_id=mm.id, used_liters=50, recovered_liters=50, recovery_pct=100,
                            disposal_method='沉淀池', manifest='WW2', state='verified', verified_by='admin@t'))
    db.commit()
r = client.post('/wastewater/2/quality', json={'ph': 7.9, 'conductivity_us_cm': 500, 'turbidity_ntu': 12}, headers=planner)
s2 = r.json(); run_job()
row = client.get(f"/water-quality/{s2['id']}", headers=planner).json()
check('second sample low', row['risk_level'] == 'low', row)
# Handover first
check('second handover accepted', client.post('/wastewater/2/handover', json={'received_by': '环保王工', 'receipt': '沉淀池签收单B'}, headers=planner).status_code == 200)
check('late correction after handover rejected', client.put(f"/water-quality/{s2['id']}", json={'ph': 7, 'conductivity_us_cm': 900, 'turbidity_ntu': 40}, headers=planner).status_code == 409)

# --- interrupted inference and retry does not add approval ------------------
with Session() as db:
    sv=SoilingSurvey(code='SV3',block_id=1,capacity_kw=100,slope=20,irradiance=800,module_temp=30,rain_days=9,dust=2,observed_kw=70,state='closed')
    db.add(sv);db.flush()
    mm=WashMission(survey_id=sv.id, water_liters=30, available_liters=30, status='accepted', verified_gain=3)
    db.add(mm);db.flush()
    db.add(WastewaterRecord(mission_id=mm.id, used_liters=30, recovered_liters=30, recovery_pct=100,
                            disposal_method='沉淀池', manifest='WW3', state='verified', verified_by='admin@t'))
    db.commit()
s3 = client.post('/wastewater/3/quality', json={'ph': 6.4, 'conductivity_us_cm': 2600, 'turbidity_ntu': 90}, headers=planner).json()
worker.mark_failed(s3['id'], 1, RuntimeError('worker killed'))
row = client.get(f"/water-quality/{s3['id']}", headers=planner).json()
check('interrupted sample needs disposal', row['release_status'] == 'disposal' and row['state'] == 'failed', row)
client.post(f"/water-quality/{s3['id']}/approve", json={'version': 1, 'approval_basis': '实验室已复检，准予改排'}, headers=admin)
row = client.get(f"/water-quality/{s3['id']}", headers=planner).json()
check('approval on interrupted sample releases', row['release_status'] == 'released', row)
client.post(f"/water-quality/{s3['id']}/retry", headers=planner)
row = client.get(f"/water-quality/{s3['id']}", headers=planner).json()
check('retry blocks release while re-evaluating', row['release_status'] == 'pending', row)
run_job()
row = client.get(f"/water-quality/{s3['id']}", headers=planner).json()
check('retry yields high risk needing fresh disposal', row['risk_level'] == 'high' and row['release_status'] == 'disposal' and not row['has_current_approval'], row)

# --- viewer read-only --------------------------------------------------------
check('viewer cannot register', client.post('/wastewater/3/quality', json={'ph': 7, 'conductivity_us_cm': 500, 'turbidity_ntu': 10}, headers=viewer).status_code == 403)
check('viewer can read list', client.get('/water-quality', headers=viewer).status_code == 200)
check('evidence exposes holdout metrics', client.get('/water-quality/evidence', headers=viewer).json()['holdout_rows'] == 100)

# --- gate on un-sampled record ----------------------------------------------
with Session() as db:
    sv=SoilingSurvey(code='SV4',block_id=1,capacity_kw=100,slope=20,irradiance=800,module_temp=30,rain_days=9,dust=2,observed_kw=70,state='closed')
    db.add(sv);db.flush()
    mm=WashMission(survey_id=sv.id, water_liters=10, available_liters=10, status='accepted', verified_gain=1)
    db.add(mm);db.flush()
    db.add(WastewaterRecord(mission_id=mm.id, used_liters=10, recovered_liters=10, recovery_pct=100,
                            disposal_method='沉淀池', manifest='WW4', state='verified', verified_by='admin@t'))
    db.commit()
r = client.post('/wastewater/4/handover', json={'received_by': '环保王工', 'receipt': '沉淀池签收单C'}, headers=planner)
check('un-sampled handover blocked', r.status_code == 409 and '水质' in r.json()['detail'], r.text)

# --- worker recovery re-queues interrupted samples without approval ----------
with Session() as db:
    sv=SoilingSurvey(code='SV5',block_id=1,capacity_kw=100,slope=20,irradiance=800,module_temp=30,rain_days=9,dust=2,observed_kw=70,state='closed')
    db.add(sv);db.flush()
    mm=WashMission(survey_id=sv.id, water_liters=20, available_liters=20, status='accepted', verified_gain=1)
    db.add(mm);db.flush()
    db.add(WastewaterRecord(mission_id=mm.id, used_liters=20, recovered_liters=20, recovery_pct=100,
                            disposal_method='沉淀池', manifest='WW5', state='verified', verified_by='admin@t'))
    db.commit()
s5=client.post('/wastewater/5/quality', json={'ph': 7.1, 'conductivity_us_cm': 700, 'turbidity_ntu': 25}, headers=planner).json()
queue.jobs.clear()
worker.recover_interrupted()
check('recovery requeues evaluating sample', len(queue.jobs) == 1 and json.loads(queue.jobs[0][1])['version'] == 1, queue.jobs)
run_job()
row=client.get(f"/water-quality/{s5['id']}", headers=planner).json()
check('recovered sample classified, no approval invented', row['state'] == 'classified' and not row['has_current_approval'], row)

# --- correction accepted first blocks the concurrent handover ---------------
with Session() as db:
    sv=SoilingSurvey(code='SV6',block_id=1,capacity_kw=100,slope=20,irradiance=800,module_temp=30,rain_days=9,dust=2,observed_kw=70,state='closed')
    db.add(sv);db.flush()
    mm=WashMission(survey_id=sv.id, water_liters=20, available_liters=20, status='accepted', verified_gain=1)
    db.add(mm);db.flush()
    db.add(WastewaterRecord(mission_id=mm.id, used_liters=20, recovered_liters=20, recovery_pct=100,
                            disposal_method='沉淀池', manifest='WW6', state='verified', verified_by='admin@t'))
    db.commit()
s6=client.post('/wastewater/6/quality', json={'ph': 7.9, 'conductivity_us_cm': 520, 'turbidity_ntu': 15}, headers=planner).json()
run_job()
check('third sample releasable first', client.get(f"/water-quality/{s6['id']}", headers=planner).json()['release_status'] == 'released')
check('correction accepted before handover', client.put(f"/water-quality/{s6['id']}", json={'ph': 6.9, 'conductivity_us_cm': 3100, 'turbidity_ntu': 110}, headers=planner).status_code == 200)
r=client.post('/wastewater/6/handover', json={'received_by': '环保王工', 'receipt': '沉淀池签收单D'}, headers=planner)
check('handover loses to accepted correction', r.status_code == 409 and '推断未完成' in r.json()['detail'], r.text)
run_job()
row=client.get(f"/water-quality/{s6['id']}", headers=planner).json()
check('corrected sample re-judged high', row['version'] == 2 and row['risk_level'] == 'high' and row['release_status'] == 'disposal', row)

# --- late worker write after handover is a no-op ----------------------------
with Session() as db:
    sv=SoilingSurvey(code='SV7',block_id=1,capacity_kw=100,slope=20,irradiance=800,module_temp=30,rain_days=9,dust=2,observed_kw=70,state='closed')
    db.add(sv);db.flush()
    mm=WashMission(survey_id=sv.id, water_liters=20, available_liters=20, status='accepted', verified_gain=1)
    db.add(mm);db.flush()
    db.add(WastewaterRecord(mission_id=mm.id, used_liters=20, recovered_liters=20, recovery_pct=100,
                            disposal_method='沉淀池', manifest='WW7', state='verified', verified_by='admin@t'))
    db.commit()
s7=client.post('/wastewater/7/quality', json={'ph': 7.1, 'conductivity_us_cm': 650, 'turbidity_ntu': 22}, headers=planner).json()
run_job()
client.post(f"/water-quality/{s7['id']}/approve", json={'version': 1, 'approval_basis': '证据不足，实验室加急复检合格后放行'}, headers=admin)
r=client.post('/wastewater/7/handover', json={'received_by': '环保王工', 'receipt': '沉淀池签收单E'}, headers=admin)
check('uncertain sample handover after approval', r.status_code == 200, r.text)
with Session() as db:
    before=db.get(WaterQualitySample, s7['id']).prediction_history
# a redelivered job for the frozen version must change nothing
worker.classify_quality(s7['id'], 1)
row=client.get(f"/water-quality/{s7['id']}", headers=planner).json()
check('redelivered job cannot alter frozen handover', row['release_status'] == 'handed_over' and row['prediction_history'] == before, row)

print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    print('FAILED:', FAIL)
    sys.exit(1)
