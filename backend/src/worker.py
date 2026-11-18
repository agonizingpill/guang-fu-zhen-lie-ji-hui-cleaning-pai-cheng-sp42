import os,time,json,traceback
from pathlib import Path
from sqlalchemy import select
import redis
from .database import Session
from .models import ArrayBlock,SoilingSurvey
from .quality_models import WaterQualitySample
from .power_model import estimate
from . import quality_service,quality_model
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)

def assess_survey(key):
    with Session() as db:
        survey=db.scalar(select(SoilingSurvey).where(SoilingSurvey.id==key).with_for_update())
        if not survey or survey.state!='evaluating':return
        block=db.get(ArrayBlock,survey.block_id)
        clean,loss,evidence=estimate(survey)
        survey.clean_kw=clean;survey.predicted_loss=loss;survey.model_evidence=evidence
        survey.recommended_water=round(block.area_m2*.6*(1+.1*survey.dust),1)
        survey.state='assessed';db.commit()
        Path(os.getenv('ARTIFACTS_DIR','/artifacts')).mkdir(parents=True,exist_ok=True)
        Path(f'{os.getenv("ARTIFACTS_DIR","/artifacts")}/survey-{key}.json').write_text(json.dumps({'survey_id':key,'clean_kw':clean,'loss_kw':loss,'model':evidence}))

def classify_quality(sample_id,version):
    # Read inputs without the lock, run the model, then lock only to write and
    # re-check version/state.  Keeping inference outside the row lock means a
    # concurrent correction or handover never waits on scikit-learn.
    with Session() as db:
        sample=db.get(WaterQualitySample,sample_id)
        if not sample or sample.version!=version or sample.state!='evaluating' or sample.handed_over_at:return
        fields=(sample.ph,sample.conductivity_us_cm,sample.turbidity_ntu)
    probability,risk,evidence=quality_model.expect(*fields)
    with Session() as db:
        sample=db.scalar(select(WaterQualitySample).where(WaterQualitySample.id==sample_id).with_for_update())
        # A correction bumped the version or a handover froze the record: the
        # late inference is invalid and must not resurrect a released sample.
        if not sample or sample.version!=version or sample.state!='evaluating' or sample.handed_over_at:return
        history=list(sample.prediction_history or [])
        # A fresh prediction supersedes any approval granted while evidence was
        # missing (interrupted inference).  Retry never renews an approval.
        if sample.approved_version==version and sample.approved_at:
            history.append({'version':version,'outcome':'approval_superseded_by_new_prediction'})
            sample.approved_by='';sample.approval_basis='';sample.approved_version=None;sample.approved_at=None
        sample.probability=probability;sample.risk_level=risk;sample.model_evidence=evidence;sample.state='classified'
        history.append({'version':version,'probability':probability,'risk_level':risk,'outcome':'classified'})
        sample.prediction_history=history;db.commit()
    Path(os.getenv('ARTIFACTS_DIR','/artifacts')).mkdir(parents=True,exist_ok=True)
    Path(f'{os.getenv("ARTIFACTS_DIR","/artifacts")}/water-quality-{sample_id}-v{version}.json').write_text(json.dumps({'sample_id':sample_id,'version':version,'ph':fields[0],'conductivity_us_cm':fields[1],'turbidity_ntu':fields[2],'probability':probability,'risk_level':risk,'model':evidence}))

def mark_failed(sample_id,version,error):
    with Session() as db:
        sample=db.scalar(select(WaterQualitySample).where(WaterQualitySample.id==sample_id).with_for_update())
        if not sample or sample.version!=version or sample.state!='evaluating':return
        sample.state='failed';sample.risk_level='';sample.probability=None;sample.model_evidence={'error':str(error)[:500]}
        history=list(sample.prediction_history or [])
        history.append({'version':version,'outcome':'failed','error':str(error)[:500]})
        sample.prediction_history=history;db.commit()

def recover_interrupted():
    """On worker start, re-queue every sample left mid-inference."""
    with Session() as db:
        stuck=db.scalars(select(WaterQualitySample).where(WaterQualitySample.state=='evaluating',WaterQualitySample.handed_over_at.is_(None))).all()
        for sample in stuck:quality_service.enqueue(queue,sample.id,sample.version)

def main():
    recover_interrupted()
    while True:
        Path('/tmp/solar-heartbeat').touch()
        job=queue.blpop(['solar:assessment','solar:quality'],timeout=3)
        if not job:continue
        try:
            if job[0]=='solar:assessment':
                assess_survey(int(job[1]))
            else:
                envelope=json.loads(job[1]);classify_quality(int(envelope['id']),int(envelope['version']))
        except Exception as error:
            traceback.print_exc()
            if job[0]=='solar:assessment':
                with Session() as db:
                    survey=db.get(SoilingSurvey,int(job[1]))
                    if survey:survey.state='blocked';survey.model_evidence={'error':str(error)};db.commit()
            else:
                envelope=json.loads(job[1]);mark_failed(int(envelope['id']),int(envelope['version']),error)

if __name__=='__main__':main()
