from pydantic import Field,field_validator
from .revisit_schemas import Inputs

class QualityInput(Inputs):
    ph:float=Field(ge=0,le=14)
    conductivity_us_cm:float=Field(ge=0,le=100000)
    turbidity_ntu:float=Field(ge=0,le=100000)

class ApprovalInput(Inputs):
    version:int=Field(ge=1)
    approval_basis:str=Field(min_length=5,max_length=500)
    @field_validator('approval_basis')
    @classmethod
    def reason(cls,v):
        if len(v.strip())<5:raise ValueError('处置依据至少5字')
        return v.strip()
