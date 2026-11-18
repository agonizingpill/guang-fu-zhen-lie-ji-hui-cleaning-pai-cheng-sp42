import os
import redis
from fastapi import APIRouter,Depends
from sqlalchemy import select
from .database import session
from .auth import current,editor,admin
from .quality_models import WaterQualitySample
from .quality_schemas import QualityInput,ApprovalInput
from . import quality_service,quality_model
from .field_service import require
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
router=APIRouter()

@router.get('/water-quality')
def samples(user=Depends(current),db=Depends(session)):
    rows=db.scalars(select(WaterQualitySample).order_by(WaterQualitySample.id.desc())).all()
    return [quality_service.view(row) for row in rows]

@router.get('/water-quality/evidence')
def evidence(user=Depends(current)):
    return quality_model.train_reference()[1]

@router.post('/wastewater/{key}/quality')
def register(key:int,body:QualityInput,user=Depends(editor),db=Depends(session)):
    return quality_service.view(quality_service.register(db,key,body,user,queue))

@router.put('/water-quality/{key}')
def correct(key:int,body:QualityInput,user=Depends(editor),db=Depends(session)):
    return quality_service.view(quality_service.correct(db,key,body,user,queue))

@router.post('/water-quality/{key}/retry')
def retry(key:int,user=Depends(editor),db=Depends(session)):
    return quality_service.view(quality_service.retry(db,key,user,queue))

@router.post('/water-quality/{key}/approve')
def approve(key:int,body:ApprovalInput,user=Depends(admin),db=Depends(session)):
    return quality_service.view(quality_service.approve(db,key,body,user))

@router.get('/water-quality/{key}')
def sample(key:int,user=Depends(current),db=Depends(session)):
    return quality_service.view(require(db,WaterQualitySample,key))
