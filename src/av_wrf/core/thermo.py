"""Pure scalar and vectorized meteorological formulas, THM consistency, and bit-grooming.

WRF inversion uses fixed `WRF_T0_K = 300.0 K`, `r_d = 287.0`, `cp = 1004.5`, `r_v = 461.6`,
`xlv = 2.5e6`, and `g = 9.81` (`WRF/share/module_model_constants.F`); MSLP uses a C0-continuous
Shuell plateau cap across `290.5 K`; 10 m gusts use surface air density `PSFC / (Rd * T_v0)`.
"""

from __future__ import annotations

import math
import struct
from array import array
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import (
    CompMass3D,
    EarthWindMps,
    GridWindMps,
    MixingRatioKgKg,
    OmegaPaS,
    Pascals,
    ScienceProfile,
    SpecificHumidityKgKg,
    TempK,
    VerticalVelocityMps,
    WrfPerturbMoistThetaK,
    WrfPerturbPotTempK,
)

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


def potential_to_temp_k(theta_perturb_k: WrfPerturbPotTempK, pressure_pa: Pascals) -> TempK:
    """Convert WRF perturbation potential temperature T = theta - 300 K to sensible T (K)."""
    if pressure_pa <= 0.0 or not math.isfinite(pressure_pa) or not math.isfinite(theta_perturb_k):
        raise ValueError("Pressure and theta_perturb_k must be finite with pressure > 0")
    theta = float(theta_perturb_k) + WRF_T0_K
    return TempK(theta * ((float(pressure_pa) / P0_PA) ** KAPPA_DRY))


def moist_thm_to_temp_k(
    thm_perturb_k: WrfPerturbMoistThetaK,
    qvapor_kgkg: MixingRatioKgKg,
    pressure_pa: Pascals,
) -> TempK:
    """Convert WRF THM = theta*(1 + (Rv/Rd)*qv) - 300 K to sensible air T (K)."""
    qv = max(0.0, float(qvapor_kgkg))
    theta = (float(thm_perturb_k) + WRF_T0_K) / (1.0 + MOIST_THETA_COEFF * qv)
    return potential_to_temp_k(WrfPerturbPotTempK(theta - WRF_T0_K), pressure_pa)


def verify_t_thm_consistency(
    t_dry: CompMass3D,
    thm: CompMass3D,
    p_pert: CompMass3D,
    pb: CompMass3D,
    qv: CompMass3D,
    science: ScienceProfile,
) -> None:
    """Verify wrfout dry `T` and moist `THM` invert to identical sensible T using fixed 300 K."""
    td_v = np.frombuffer(t_dry.buf, dtype=np.float32).astype(np.float64)
    thm_v = np.frombuffer(thm.buf, dtype=np.float32).astype(np.float64)
    qv_raw = np.frombuffer(qv.buf, dtype=np.float32).astype(np.float64)
    p_pa = np.frombuffer(p_pert.buf, dtype=np.float32).astype(np.float64) + np.frombuffer(
        pb.buf, dtype=np.float32
    ).astype(np.float64)
    if not (
        np.all(np.isfinite(td_v))
        and np.all(np.isfinite(thm_v))
        and np.all(np.isfinite(qv_raw))
        and np.all(np.isfinite(p_pa))
    ):
        raise ValueError("Non-finite value in wrfout T/THM/P/QVAPOR")
    if bool(np.any(p_pa <= 0.0)):
        raise ValueError("Non-positive pressure in wrfout T/THM consistency check")
    theta_dry = td_v + WRF_T0_K
    if science.use_theta_m == 1:
        theta_from_thm = (thm_v + WRF_T0_K) / (1.0 + MOIST_THETA_COEFF * np.maximum(0.0, qv_raw))
    else:
        theta_from_thm = thm_v + WRF_T0_K
    exner = np.power(p_pa / P0_PA, KAPPA_DRY)
    diff = np.abs((theta_dry - theta_from_thm) * exner)
    worst = int(np.argmax(diff))
    if float(diff[worst]) > 0.05:
        raise ValueError(f"wrfout T vs THM mismatch ({float(diff[worst]):.3f} K) at index {worst}")


def mixing_ratio_to_spfh(qvapor_kgkg: MixingRatioKgKg) -> SpecificHumidityKgKg:
    """Convert water vapor mixing ratio r >= 0 to specific humidity q = r / (1 + r)."""
    r = max(0.0, float(qvapor_kgkg))
    return SpecificHumidityKgKg(r / (1.0 + r))


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


def clamp_spfh_2m_to_liquid_sat(spfh_2m_kgkg: float, t2_k: float, psfc_pa: float) -> float:
    """Cap diagnostic 2 m specific humidity at consumer Bolton (1980) liquid saturation."""
    res = clamp_spfh_2m_vec(
        np.array([spfh_2m_kgkg], dtype=np.float64),
        np.array([t2_k], dtype=np.float64),
        np.array([psfc_pa], dtype=np.float64),
    )
    return float(res[0])


def geometric_w_to_omega_pas(
    w_mps: VerticalVelocityMps,
    pressure_pa: Pascals,
    temp_k: TempK,
    spfh_kgkg: SpecificHumidityKgKg,
) -> OmegaPaS:
    """Convert geometric vertical velocity w (m/s) to hydrostatic omega = -rho_m * g0 * w."""
    if pressure_pa <= 0.0 or temp_k <= 0.0:
        raise ValueError("Pressure and temperature must be positive")
    tv_k = float(temp_k) * (1.0 + VIRTUAL_TEMP_COEFF * max(0.0, float(spfh_kgkg)))
    rho_moist = float(pressure_pa) / (R_DRY_J_KG_K * tv_k)
    return OmegaPaS(-rho_moist * G0_MPS2 * float(w_mps))


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


def hypsometric_mslp_pa(psfc_pa: float, terrain_m: float, t2_k: float, spfh_2m: float) -> float:
    """Scalar wrapper delegating to `hypsometric_mslp_vec` for single-owner MSLP math."""
    res = hypsometric_mslp_vec(
        np.array([psfc_pa], dtype=np.float64),
        np.array([terrain_m], dtype=np.float64),
        np.array([t2_k], dtype=np.float64),
        np.array([spfh_2m], dtype=np.float64),
    )
    return float(res[0])


def parameterized_gust_10m_vec(
    u10_mps: npt.NDArray[np.float64],
    v10_mps: npt.NDArray[np.float64],
    wspd10max_mps: npt.NDArray[np.float64],
    ustar_mps: npt.NDArray[np.float64],
    sensible_flux_wm2: npt.NDArray[np.float64],
    latent_flux_wm2: npt.NDArray[np.float64],
    pblh_m: npt.NDArray[np.float64],
    t2_k: npt.NDArray[np.float64],
    spfh_2m: npt.NDArray[np.float64],
    psfc_pa: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Vectorized Panofsky (1977) / ECMWF IFS 10 m gust with surface air density."""
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
    return np.maximum(wspd10max_mps, np.hypot(u10_mps, v10_mps) + eddy)


def parameterized_gust_10m_mps(
    u10_mps: float,
    v10_mps: float,
    wspd10max_mps: float,
    ustar_mps: float,
    sensible_flux_wm2: float,
    latent_flux_wm2: float,
    pblh_m: float,
    t2_k: float,
    spfh_2m: float = 0.0,
    psfc_pa: float = P0_PA,
) -> float:
    """Combine resolved WSPD10MAX with Panofsky (1977) / ECMWF IFS surface-density gusts."""
    res = parameterized_gust_10m_vec(
        np.array([u10_mps], dtype=np.float64),
        np.array([v10_mps], dtype=np.float64),
        np.array([wspd10max_mps], dtype=np.float64),
        np.array([ustar_mps], dtype=np.float64),
        np.array([sensible_flux_wm2], dtype=np.float64),
        np.array([latent_flux_wm2], dtype=np.float64),
        np.array([pblh_m], dtype=np.float64),
        np.array([t2_k], dtype=np.float64),
        np.array([spfh_2m], dtype=np.float64),
        np.array([psfc_pa], dtype=np.float64),
    )
    return float(res[0])


def max_random_cloud_pct(cldfra_levels: Sequence[float]) -> float:
    """Compute column cloud cover percentage (0..100) via Geleyn/Hogan max-random overlap."""
    if not cldfra_levels:
        return 0.0
    clear_prob = 1.0 - min(1.0, max(0.0, cldfra_levels[0]))
    for idx in range(1, len(cldfra_levels)):
        c_prev = min(1.0, max(0.0, cldfra_levels[idx - 1]))
        c_curr = min(1.0, max(0.0, cldfra_levels[idx]))
        if c_prev < 0.999999:
            ratio = (1.0 - max(c_prev, c_curr)) / (1.0 - c_prev)
            clear_prob *= min(1.0, max(0.0, ratio))
        else:
            clear_prob = 0.0
    return 100.0 * (1.0 - min(1.0, max(0.0, clear_prob)))


def rotate_grid_to_earth(
    u_grid: GridWindMps,
    v_grid: GridWindMps,
    cos_alpha: float,
    sin_alpha: float,
) -> tuple[EarthWindMps, EarthWindMps]:
    """Rotate grid-relative winds to Earth-relative using WRF COSALPHA and SINALPHA."""
    ug, vg = float(u_grid), float(v_grid)
    return (
        EarthWindMps(ug * cos_alpha - vg * sin_alpha),
        EarthWindMps(ug * sin_alpha + vg * cos_alpha),
    )


def verify_wrf_alpha_against_lambert(
    cos_alpha: float,
    sin_alpha: float,
    cos_theta: float,
    sin_theta: float,
    tol: float = 1e-4,
) -> None:
    """Verify WRF's (COSALPHA, SINALPHA) matches analytical Lambert (cos(theta), -sin(theta))."""
    if abs(cos_alpha - cos_theta) > tol or abs(sin_alpha + sin_theta) > tol:
        raise ValueError(
            f"WRF ({cos_alpha:.5f}, {sin_alpha:.5f}) != Lambert ({cos_theta:.5f}, {-sin_theta:.5f})"
        )


def window_rate_from_accum(
    prev_val: float,
    prev_bucket_count: int,
    curr_val: float,
    curr_bucket_count: int,
    bucket_scale: float,
    dt_seconds: float,
    *,
    per_hour: bool = False,
    allow_negative: bool = False,
) -> float:
    """Compute mean rate across (t_prev, t_curr] from cumulative or bucketed WRF totals."""
    if dt_seconds <= 0.0:
        raise ValueError("Window duration dt_seconds must be positive")
    delta = (curr_val + curr_bucket_count * bucket_scale) - (
        prev_val + prev_bucket_count * bucket_scale
    )
    if not allow_negative and delta < -1e-6:
        raise ValueError(f"Negative cumulative delta ({delta}) across window")
    clamped = delta if allow_negative else max(0.0, delta)
    rate_per_s = clamped / dt_seconds
    return rate_per_s * 3600.0 if per_hour else rate_per_s


def groom_mantissa_f32(value: float, keepbits: int) -> float:
    """Round-to-nearest-even IEEE-754 float32 retaining `keepbits` significand bits."""
    if not (1 <= keepbits <= 23):
        raise ValueError("keepbits must be in 1..23 for float32")
    if not math.isfinite(value) or value == 0.0:
        return value
    (bits,) = struct.unpack("<I", struct.pack("<f", value))
    drop = 23 - keepbits
    if drop == 0:
        (exact,) = struct.unpack("<f", struct.pack("<I", bits))
        return float(exact)
    half = 1 << (drop - 1)
    lsb = (bits >> drop) & 1
    rounded = (bits + half - 1 + lsb) & ~((1 << drop) - 1)
    (groomed,) = struct.unpack("<f", struct.pack("<I", rounded))
    return float(groomed)


def groom_f32_buf(values: npt.NDArray[np.float64], keepbits: int) -> array[float]:
    """Batch round-to-nearest-even IEEE-754 float32 bit-grooming matching `groom_mantissa_f32`."""
    if not (1 <= keepbits <= 23):
        raise ValueError("keepbits must be in 1..23 for float32")
    f32 = np.ascontiguousarray(values, dtype=np.float32).ravel()
    drop = 23 - keepbits
    if drop > 0:
        bits = f32.view(np.uint32).copy()
        exp = (bits >> np.uint32(23)) & np.uint32(0xFF)
        active = (exp != np.uint32(0xFF)) & ((bits & np.uint32(0x7FFFFFFF)) != np.uint32(0))
        half = np.uint32(1 << (drop - 1))
        lsb = (bits >> np.uint32(drop)) & np.uint32(1)
        mask = np.uint32(0xFFFFFFFF ^ ((1 << drop) - 1))
        rounded = (bits + half - np.uint32(1) + lsb) & mask
        f32 = np.where(active, rounded, bits).astype(np.uint32, copy=False).view(np.float32)
    out = array("f")
    out.frombytes(np.ascontiguousarray(f32, dtype=np.float32).tobytes())
    return out
