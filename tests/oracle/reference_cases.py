"""Independent analytical reference oracle locked by SHA-256 in manifest.lock."""

from __future__ import annotations

import math

# Independent NH and SH secant spherical Lambert reference vectors (R = 6,370,000 m)
NH_MIDLAT_LCC = (40.20, 44.50, 38.50, 41.80)
SH_MIDLAT_LCC = (-43.50, 170.50, -46.00, -41.00)

# Independent analytical wind-rotation case: 30 deg counter-clockwise grid angle
ROTATION_30DEG_COS = math.cos(math.radians(30.0))
ROTATION_30DEG_SIN = math.sin(math.radians(30.0))

# Independent WRF dry Poisson reference (`module_model_constants.F`: r_d=287.0, cp=1004.5 -> 2/7)
THETA_PERTURB_10K = 10.0
PRESSURE_850HPA_PA = 85_000.0
EXPECTED_TEMP_850HPA_K = 310.0 * ((85_000.0 / 100_000.0) ** (2.0 / 7.0))
