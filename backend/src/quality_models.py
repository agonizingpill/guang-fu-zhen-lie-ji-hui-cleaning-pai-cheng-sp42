from datetime import datetime
from sqlalchemy import String,Float,ForeignKey,JSON,CheckConstraint,DateTime,func,Integer
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base

class WaterQualitySample(Base):
    __tablename__='water_quality_samples'
    __table_args__=(CheckConstraint('ph>=0 AND ph<=14 AND conductivity_us_cm>=0 AND turbidity_ntu>=0 AND version>=1'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    wastewater_id:Mapped[int]=mapped_column(ForeignKey('wastewater_records.id'),unique=True)
    ph:Mapped[float]=mapped_column(Float)
    conductivity_us_cm:Mapped[float]=mapped_column(Float)
    turbidity_ntu:Mapped[float]=mapped_column(Float)
    version:Mapped[int]=mapped_column(Integer,default=1)
    state:Mapped[str]=mapped_column(String(24),default='evaluating')
    risk_level:Mapped[str]=mapped_column(String(16),default='')
    probability:Mapped[float|None]=mapped_column(Float,nullable=True)
    model_evidence:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    history:Mapped[list]=mapped_column(JSON,default=list)
    prediction_history:Mapped[list]=mapped_column(JSON,default=list)
    sampled_by:Mapped[str]=mapped_column(String(120),default='')
    sampled_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    updated_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),onupdate=func.now())
    approved_by:Mapped[str]=mapped_column(String(120),default='')
    approval_basis:Mapped[str]=mapped_column(String(500),default='')
    approved_version:Mapped[int|None]=mapped_column(Integer,nullable=True)
    approved_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    handed_over_at:Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    release_snapshot:Mapped[dict|None]=mapped_column(JSON,nullable=True)
