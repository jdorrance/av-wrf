"""Pure vectorized meteorological formulas, THM consistency, and bit-grooming.

WRF inversion uses fixed `WRF_T0_K = 300.0 K`, `r_d = 287.0`, `cp = 1004.5`, `r_v = 461.6`,
`xlv = 2.5e6`, and `g = 9.81` (`WRF/share/module_model_constants.F`); MSLP uses a C0-continuous
Shuell plateau cap across `290.5 K`; 10 m gusts use surface air density `PSFC / (Rd * T_v0)`.
"""

from __future__ import annotations

from array import array
from itertools import pairwise

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import CompMass3D, ScienceProfile

P0_PA = 100_000.0
WRF_T0_K = 300.0
R_DRY_J_KG_K = 287.0
R_VAPOR_J_KG_K = 461.6
CP_DRY_J_KG_K = 3.5 * R_DRY_J_KG_K
KAPPA_DRY = R_DRY_J_KG_K / CP_DRY_J_KG_K
LV_VAPOR_J_KG = 2_500_000.0
MOIST_THETA_COEFF = R_VAPOR_J_KG_K / R_DRY_J_KG_K
VIRTUAL_TEMP_COEFF = MOIST_THETA_COEFF - 1.0
G0_MPS2 = 9.81
STD_LAPSE_K_PER_M = 0.0065
IFS_GUST_COEFF = 7.71
VON_KARMAN = 0.40
CONSUMER_KELVIN_OFFSET = 273.15
CONSUMER_EPSILON = 0.622


def potential_to_temp_vec(
    theta_perturb_k: npt.NDArray[np.float64], pressure_pa: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    """Vectorized WRF dry perturbation potential temperature (`theta - 300 K`) inversion."""
    return (theta_perturb_k + WRF_T0_K) * np.power(pressure_pa / P0_PA, KAPPA_DRY)


def verify_t_thm_consistency(
    t_dry: CompMass3D,
    thm: CompMass3D,
    p_pert: CompMass3D,
    pb: CompMass3D,
    qv: CompMass3D,
    science: ScienceProfile,
) -> None:
    """Verify wrfout dry `T` and moist `THM` invert to identical sensible T using fixed 300 K."""
    td_v = np.frombuffer(t_dry.buf, dtype=np.float32)
    thm_v = np.frombuffer(thm.buf, dtype=np.float32)
    qv_raw = np.frombuffer(qv.buf, dtype=np.float32)
    p_pa = np.add(
        np.frombuffer(p_pert.buf, dtype=np.float32),
        np.frombuffer(pb.buf, dtype=np.float32),
        dtype=np.float64,
    )
    if not (
        np.all(np.isfinite(td_v))
        and np.all(np.isfinite(thm_v))
        and np.all(np.isfinite(qv_raw))
        and np.all(np.isfinite(p_pa))
    ):
        raise ValueError("Non-finite value in wrfout T/THM/P/QVAPOR")
    if bool(np.any(p_pa <= 0.0)):
        raise ValueError("Non-positive pressure in wrfout T/THM consistency check")
    diff = np.subtract(td_v, thm_v, dtype=np.float64)
    if science.use_theta_m == 1:
        moist = np.maximum(qv_raw, 0.0, dtype=np.float64) * MOIST_THETA_COEFF
        diff += (np.add(td_v, WRF_T0_K, dtype=np.float64)) * moist
        diff /= 1.0 + moist
    np.abs(diff, out=diff)
    # Most frames satisfy the tolerance even at the greatest possible Exner factor.
    if float(np.max(diff)) * (float(np.max(p_pa)) / P0_PA) ** KAPPA_DRY <= 0.05:
        return
    diff *= np.power(p_pa / P0_PA, KAPPA_DRY)
    worst = int(np.argmax(diff))
    if float(diff[worst]) > 0.05:
        raise ValueError(f"wrfout T vs THM mismatch ({float(diff[worst]):.3f} K) at index {worst}")


def mixing_ratio_to_spfh_vec(r_kgkg: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Convert dry-air mixing ratio to specific humidity."""
    r = np.maximum(0.0, r_kgkg)
    return r / (1.0 + r)


def clamp_spfh_2m_vec(
    spfh_2m_kgkg: npt.NDArray[np.float64],
    t2_k: npt.NDArray[np.float64],
    psfc_pa: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Vectorized Bolton (1980) 2 m liquid-saturation cap."""
    q = np.maximum(0.0, spfh_2m_kgkg)
    es_pa = 611.2 * np.exp(
        (17.67 * (t2_k - CONSUMER_KELVIN_OFFSET)) / np.maximum(1.0, t2_k - 29.65)
    )
    safe_p = psfc_pa > es_pa * 1.01
    r_sat = np.where(safe_p, CONSUMER_EPSILON * es_pa / np.maximum(1.0, psfc_pa - es_pa), q)
    return np.where(safe_p, np.minimum(q, (r_sat / (1.0 + r_sat)) * (1.0 - 2.0**-14)), q)


def geometric_w_to_omega_vec(
    w_mps: npt.NDArray[np.float64],
    pressure_pa: npt.NDArray[np.float64],
    temp_k: npt.NDArray[np.float64],
    spfh_kgkg: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Vectorized hydrostatic vertical velocity conversion `omega = -rho_m * g0 * w`."""
    tv_k = temp_k * (1.0 + VIRTUAL_TEMP_COEFF * np.maximum(0.0, spfh_kgkg))
    return -(pressure_pa / (R_DRY_J_KG_K * tv_k)) * G0_MPS2 * w_mps


def hypsometric_mslp_vec(
    psfc_pa: npt.NDArray[np.float64],
    terrain_m: npt.NDArray[np.float64],
    t2_k: npt.NDArray[np.float64],
    spfh_2m: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Continuous WMO / Shuell plateau MSLP reduction with zero branch jump across 290.5 K."""
    z_m = np.maximum(0.0, terrain_m)
    tv_sfc = t2_k * (1.0 + VIRTUAL_TEMP_COEFF * np.maximum(0.0, spfh_2m))
    tv_0 = np.where(tv_sfc < 255.0, 0.5 * (255.0 + tv_sfc), tv_sfc)
    tv_sl = tv_0 + STD_LAPSE_K_PER_M * z_m
    hot_plat = 0.5 * (290.5 + tv_0)
    tv_0_adj = np.where((tv_0 > 290.5) & (tv_sl > 290.5), hot_plat, tv_0)
    tv_sl_adj = np.where(
        (tv_0 <= 290.5) & (tv_sl > 290.5),
        290.5,
        np.where((tv_0 > 290.5) & (tv_sl > 290.5), hot_plat, tv_sl),
    )
    tv_mean = np.maximum(180.0, 0.5 * (tv_0_adj + tv_sl_adj))
    return psfc_pa * np.exp((G0_MPS2 * z_m) / (R_DRY_J_KG_K * tv_mean))


def parameterized_gust_10m_vec(
    u10_mps: npt.NDArray[np.float64],
    v10_mps: npt.NDArray[np.float64],
    ustar_mps: npt.NDArray[np.float64],
    sensible_flux_wm2: npt.NDArray[np.float64],
    latent_flux_wm2: npt.NDArray[np.float64],
    pblh_m: npt.NDArray[np.float64],
    t2_k: npt.NDArray[np.float64],
    spfh_2m: npt.NDArray[np.float64],
    psfc_pa: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Instantaneous 10 m wind plus a Panofsky-style turbulence increment."""
    q2, tk = np.maximum(0.0, spfh_2m), np.maximum(200.0, t2_k)
    tv0 = tk * (1.0 + VIRTUAL_TEMP_COEFF * q2)
    rho = np.maximum(10_000.0, psfc_pa) / (R_DRY_J_KG_K * tv0)
    vflux = (1.0 + VIRTUAL_TEMP_COEFF * q2) * (
        sensible_flux_wm2 / (rho * CP_DRY_J_KG_K)
    ) + VIRTUAL_TEMP_COEFF * tk * (latent_flux_wm2 / (rho * LV_VAPOR_J_KG))
    wstar3 = (G0_MPS2 * np.maximum(0.0, pblh_m) / tv0) * np.maximum(0.0, vflux)
    eddy = IFS_GUST_COEFF * np.cbrt(
        np.power(np.maximum(0.0, ustar_mps), 3.0) + (0.5 * VON_KARMAN / 12.0) * wstar3
    )
    return np.hypot(u10_mps, v10_mps) + eddy


def max_random_cloud_pct_vec(cloud: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Compute Geleyn/Hogan maximum-random overlap over clipped `(level, cell)` fractions."""
    clear: npt.NDArray[np.float64] = np.subtract(1.0, cloud[0], dtype=np.float64)
    for prev, curr in pairwise(cloud):
        active = prev < 0.999999
        ratio = (1.0 - np.maximum(prev, curr)) / np.where(active, 1.0 - prev, 1.0)
        clear *= np.where(active, ratio, 0.0)
    np.subtract(1.0, clear, out=clear)
    clear *= 100.0
    return clear


def rotate_grid_to_earth_vec(
    u: npt.NDArray[np.float64],
    v: npt.NDArray[np.float64],
    cos_alpha: npt.NDArray[np.float64],
    sin_alpha: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Rotate grid-relative winds using WRF COSALPHA and SINALPHA."""
    return u * cos_alpha - v * sin_alpha, u * sin_alpha + v * cos_alpha


def groom_f32_buf(values: npt.NDArray[np.float64], keepbits: int) -> array[float]:
    """In-place round-to-nearest-even IEEE-754 float32 bit-grooming with single buffer copy."""
    if not (1 <= keepbits <= 23):
        raise ValueError("keepbits must be in 1..23 for float32")
    out = array("f", [0.0]) * values.size
    f32 = np.frombuffer(out, dtype=np.float32).reshape(values.shape)
    np.copyto(f32, values, casting="same_kind")
    drop = 23 - keepbits
    if drop > 0:
        bits = f32.view(np.uint32)
        exp = (bits >> np.uint32(23)) & np.uint32(0xFF)
        active = (exp != np.uint32(0xFF)) & ((bits & np.uint32(0x7FFFFFFF)) != np.uint32(0))
        half_m1 = np.uint32((1 << (drop - 1)) - 1)
        mask = np.uint32(0xFFFFFFFF ^ ((1 << drop) - 1))
        rounded = (bits + half_m1 + ((bits >> np.uint32(drop)) & np.uint32(1))) & mask
        np.copyto(bits, rounded, where=active)
    return out
