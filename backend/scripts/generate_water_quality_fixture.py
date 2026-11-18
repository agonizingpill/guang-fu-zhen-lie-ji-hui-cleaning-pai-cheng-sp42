"""Generate the fixed lab-labelled water-quality reference fixture.

Reproducible reference data for the contamination-risk classifier.  Run from
``backend/`` with ``python -m scripts.generate_water_quality_fixture``.
The committed JSON is what the model actually reads; this script documents how
it was produced (Python random.Random(42), 400 rows, pH/conductivity/turbidity).

Contaminated aliquots are drawn around a slightly acidic, high-conductivity,
high-turbidity distribution; clean aliquots around a neutral, low-conductivity,
low-turbidity distribution.  Values are clamped to plausible field ranges and
rounded the way a handheld meter would report them.
"""
import json
import math
import random
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/water_quality_reference.json'
N = 400
SEED = 42
CONTAMINATED_RATE = 0.36


def generate():
    rng = random.Random(SEED)
    rows = []
    for _ in range(N):
        contaminated = rng.random() < CONTAMINATED_RATE
        if contaminated:
            ph = rng.gauss(7.05, 0.85)
            conductivity = math.exp(rng.gauss(6.90, 0.42))
            turbidity = math.exp(rng.gauss(3.95, 0.55))
        else:
            ph = rng.gauss(7.75, 0.38)
            conductivity = math.exp(rng.gauss(6.20, 0.30))
            turbidity = math.exp(rng.gauss(3.15, 0.40))
        rows.append({
            'ph': min(9.5, max(5.5, round(ph, 2))),
            'conductivity_us_cm': min(4000.0, max(150.0, round(conductivity, 1))),
            'turbidity_ntu': min(220.0, max(3.0, round(turbidity, 1))),
            'lab_contaminated': 1 if contaminated else 0,
        })
    return rows


if __name__ == '__main__':
    rows = generate()
    FIXTURE.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    print({'rows': len(rows), 'contaminated': sum(r['lab_contaminated'] for r in rows), 'path': str(FIXTURE)})
