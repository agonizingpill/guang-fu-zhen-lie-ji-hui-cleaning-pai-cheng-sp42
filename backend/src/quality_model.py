import hashlib
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split

FEATURES=['ph','conductivity_us_cm','turbidity_ntu']
LABEL='lab_contaminated'
# Only the low band may hand over along the original destination.  High and
# uncertain both need a supervisor disposal basis, so the holdout gate below
# measures release (probability < LOW) against block (everything else).
LOW_THRESHOLD=0.35
HIGH_THRESHOLD=0.60
FIXTURE=Path(__file__).resolve().parents[1]/'fixtures/water_quality_reference.json'


@lru_cache(maxsize=1)
def train_reference():
    raw=FIXTURE.read_bytes();rows=json.loads(raw)
    x=np.array([[r[k] for k in FEATURES] for r in rows]);y=np.array([r[LABEL] for r in rows])
    train_x,test_x,train_y,test_y=train_test_split(x,y,test_size=.25,random_state=42,stratify=y)
    model=RandomForestClassifier(n_estimators=100,max_depth=8,min_samples_leaf=2,random_state=42,n_jobs=1,class_weight='balanced')
    model.fit(train_x,train_y)
    probability=model.predict_proba(test_x)[:,1]
    low=probability<LOW_THRESHOLD
    contaminated=test_y==1
    missed=int(((low)&contaminated).sum());caught=int(((~low)&contaminated).sum())
    false_blocked=int(((~low)&(~contaminated)).sum());clean_low=int((low&(~contaminated)).sum())
    high=probability>=HIGH_THRESHOLD
    high_count=int(high.sum());high_true=int((high&contaminated).sum())
    evidence={
        'reference_rows':len(rows),
        'train_rows':len(train_x),
        'holdout_rows':len(test_x),
        'holdout_contaminated':int(contaminated.sum()),
        'holdout_clean':int((~contaminated).sum()),
        'low_threshold':LOW_THRESHOLD,
        'high_threshold':HIGH_THRESHOLD,
        'holdout_missed_contamination':missed,
        'holdout_miss_rate':round(missed/max(1,missed+caught),6),
        'holdout_false_blocked':false_blocked,
        'holdout_false_block_rate':round(false_blocked/max(1,false_blocked+clean_low),6),
        'holdout_high_flagged':high_count,
        'holdout_high_true':high_true,
        'holdout_high_precision':round(high_true/max(1,high_count),6),
        'holdout_accuracy':round(float(accuracy_score(test_y,(probability>=LOW_THRESHOLD).astype(int))),6),
        'holdout_confusion':{
            'clean_low':clean_low,
            'clean_blocked':false_blocked,
            'contaminated_low':missed,
            'contaminated_blocked':caught,
        },
        'feature_importance':dict(zip(FEATURES,model.feature_importances_.tolist())),
        'random_state':42,
        'split':'75/25 stratified holdout, random_state 42',
        'method':'RandomForestClassifier / 100 trees / max_depth 8 / class_weight balanced',
        'sha256':hashlib.sha256(raw).hexdigest(),
    }
    return model,evidence


def expect(ph,conductivity_us_cm,turbidity_ntu):
    """Return (probability, risk_level, evidence) for one field sample."""
    model,evidence=train_reference()
    probability=float(model.predict_proba([[ph,conductivity_us_cm,turbidity_ntu]])[0][1])
    if probability<LOW_THRESHOLD:risk='low'
    elif probability>=HIGH_THRESHOLD:risk='high'
    else:risk='uncertain'
    return probability,risk,evidence
