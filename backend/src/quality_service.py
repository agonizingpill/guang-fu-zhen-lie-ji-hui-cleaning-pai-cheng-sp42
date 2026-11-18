import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select

from .operations_models import WastewaterRecord
from .quality_models import WaterQualitySample
from .field_service import require,audit

RISK_LABELS={'low':'低风险','high':'高风险','uncertain':'证据不足（风险不确定）','':'尚未判定'}


def envelope(sample_id,version):
    return json.dumps({'id':sample_id,'version':version})


def enqueue(queue,sample_id,version):
    queue.rpush('solar:quality',envelope(sample_id,version))


def release_status(sample):
    """Server-authoritative original-destination release gate.

    Returns one of pending / disposal / released / handed_over.
    """
    if sample is None:return 'missing','未登记水质采样，不得交接'
    if sample.handed_over_at:return 'handed_over','已交接，采样、预测与批准依据已冻结'
    if sample.state=='evaluating':return 'pending','推断未完成，暂不能判风险'
    approved=sample.approved_version==sample.version and bool(sample.approved_at)
    if sample.state=='failed':
        if approved:return 'released','推断中断且证据不足，主管已填写专门处置依据'
        return 'disposal','推断中断、证据不足，须主管填写专门处置依据后放行'
    if sample.risk_level=='low':return 'released','低风险，可沿原去向交接'
    if approved:return 'released',f'{RISK_LABELS.get(sample.risk_level,sample.risk_level)}，主管已填写专门处置依据'
    return 'disposal',f'{RISK_LABELS.get(sample.risk_level,sample.risk_level)}，须主管填写专门处置依据后放行'


def release_basis(sample):
    """Why a sample may leave by its original route: low risk or supervisor basis."""
    if sample.risk_level=='low' and not (sample.approved_version==sample.version and sample.approved_at):return 'low_risk'
    return 'supervisor_disposal'


def view(sample):
    data={column.name:getattr(sample,column.name) for column in sample.__table__.columns}
    data['release_status'],data['release_reason']=release_status(sample)
    data['release_basis']=release_basis(sample)
    data['risk_label']=RISK_LABELS.get(sample.risk_level,sample.risk_level)
    data['has_current_approval']=bool(sample.approved_at and sample.approved_version==sample.version)
    return data


def _wastewater(db,key,lock=False):
    return require(db,WastewaterRecord,key,lock)


def register(db,wastewater_id,body,user,queue):
    d=_wastewater(db,wastewater_id,True)
    if d.state=='handed_over':raise HTTPException(409,'已交接凭据冻结，不可补登或更正水质')
    if d.state!='verified':raise HTTPException(409,'须先主管核验废水，再登记水质采样')
    if db.scalar(select(WaterQualitySample).where(WaterQualitySample.wastewater_id==d.id)):
        raise HTTPException(409,'该废水已有水质采样，请在待判列表更正')
    sample=WaterQualitySample(
        wastewater_id=d.id,
        ph=body.ph,conductivity_us_cm=body.conductivity_us_cm,turbidity_ntu=body.turbidity_ntu,
        version=1,state='evaluating',sampled_by=user.email,
        history=[],prediction_history=[],
    )
    db.add(sample);db.flush()
    audit(db,user,f'登记水质采样 废水{d.id} pH{body.ph} 电导{body.conductivity_us_cm} 浊度{body.turbidity_ntu}')
    db.commit();enqueue(queue,sample.id,sample.version)
    return sample


def correct(db,key,body,user,queue):
    # Handover locks the wastewater row and then the sample; correction takes
    # the same order so the two never deadlock, and the sample row is the
    # arbitration point: whichever action locks it first is the accepted one.
    ref=require(db,WaterQualitySample,key)
    d=_wastewater(db,ref.wastewater_id,True)
    sample=require(db,WaterQualitySample,key,True)
    if sample.handed_over_at:raise HTTPException(409,'已交接凭据冻结，水质不可再更正')
    if d.state=='handed_over':raise HTTPException(409,'已交接凭据冻结，水质不可再更正')
    history=list(sample.history or [])
    history.append({
        'version':sample.version,'ph':sample.ph,'conductivity_us_cm':sample.conductivity_us_cm,
        'turbidity_ntu':sample.turbidity_ntu,'risk_level':sample.risk_level,
        'probability':sample.probability,'sampled_by':sample.sampled_by,
        'replaced_at':datetime.utcnow().isoformat(),'replaced_by':user.email,
    })
    predictions=list(sample.prediction_history or [])
    if sample.probability is not None:
        predictions.append({
            'version':sample.version,'probability':sample.probability,'risk_level':sample.risk_level,
            'at':datetime.utcnow().isoformat(),'outcome':'invalidated_by_correction',
        })
    sample.version=sample.version+1
    sample.ph=body.ph;sample.conductivity_us_cm=body.conductivity_us_cm;sample.turbidity_ntu=body.turbidity_ntu
    sample.state='evaluating';sample.risk_level='';sample.probability=None;sample.model_evidence=None
    # Accepting a correction withdraws the old release eligibility immediately.
    sample.approved_by='';sample.approval_basis='';sample.approved_version=None;sample.approved_at=None
    sample.history=history;sample.prediction_history=predictions
    audit(db,user,f'更正水质采样{key} 第{sample.version}版，撤回旧放行资格')
    db.commit();enqueue(queue,sample.id,sample.version)
    return sample


def approve(db,key,body,user):
    ref=require(db,WaterQualitySample,key)
    d=_wastewater(db,ref.wastewater_id,True)
    sample=require(db,WaterQualitySample,key,True)
    if d.state=='handed_over':raise HTTPException(409,'已交接凭据冻结，不可再批准放行')
    if sample.handed_over_at:raise HTTPException(409,'已交接凭据冻结，不可再批准放行')
    if sample.state=='evaluating':raise HTTPException(409,'推断未完成，请等待判定后再处置')
    if body.version!=sample.version:
        raise HTTPException(409,'水质已更正，所批版本失效，请基于最新版本重新填写处置依据')
    sample.approved_by=user.email;sample.approval_basis=body.approval_basis
    sample.approved_version=sample.version;sample.approved_at=datetime.utcnow()
    audit(db,user,f'主管批准水质放行{key} 第{sample.version}版 依据{body.approval_basis}')
    db.commit();return sample


def retry(db,key,user,queue):
    ref=require(db,WaterQualitySample,key)
    d=_wastewater(db,ref.wastewater_id,True)
    sample=require(db,WaterQualitySample,key,True)
    if sample.handed_over_at or d.state=='handed_over':raise HTTPException(409,'已交接凭据冻结，不可重试推断')
    if sample.state not in ['evaluating','failed']:raise HTTPException(409,'仅待判或推断中断的采样可重试')
    sample.state='evaluating';sample.risk_level='';sample.probability=None
    # A retry re-runs the same sampling version; it never creates or renews approval.
    audit(db,user,f'重试水质推断{key} 第{sample.version}版，不新增批准')
    db.commit();enqueue(queue,sample.id,sample.version)
    return sample


def handover_gate(db,wastewater):
    """Validate and freeze water quality as part of wastewater handover.

    Caller must already hold the wastewater row lock.  The sample row lock is
    taken here so a concurrent correction is serialized against handover.
    """
    sample_id=db.scalar(select(WaterQualitySample.id).where(WaterQualitySample.wastewater_id==wastewater.id))
    if sample_id is None:raise HTTPException(409,'交接前须登记水质采样（酸碱度、电导率、浊度）')
    sample=require(db,WaterQualitySample,sample_id,True)
    status,reason=release_status(sample)
    if status=='handed_over':raise HTTPException(409,'该废水已完成交接，凭据冻结')
    if status=='pending':raise HTTPException(409,'水质推断未完成，待判项不能交接')
    if status=='disposal':raise HTTPException(409,reason)
    return sample


def frozen_release(sample,decision,reason=''):
    return {
        'sample_id':sample.id,'wastewater_id':sample.wastewater_id,'version':sample.version,
        'sampling':{'ph':sample.ph,'conductivity_us_cm':sample.conductivity_us_cm,'turbidity_ntu':sample.turbidity_ntu,'sampled_by':sample.sampled_by,'sampled_at':sample.sampled_at.isoformat() if sample.sampled_at else None},
        'prediction':{'probability':sample.probability,'risk_level':sample.risk_level,'state':sample.state,'model_evidence':sample.model_evidence},
        'approval':{'approved_by':sample.approved_by,'approval_basis':sample.approval_basis,'approved_version':sample.approved_version,'approved_at':sample.approved_at.isoformat() if sample.approved_at else None},
        'release_decision':decision,'release_basis':release_basis(sample),'release_reason':reason,'frozen_at':datetime.utcnow().isoformat(),
    }
