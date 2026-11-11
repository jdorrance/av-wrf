"""Pure scalar and vectorized meteorological formulas, THM consistency, and bit-grooming.

Assumptions & Producer-Consumer Constant Split:
1. Exact WRF inversion constants (`WRF/share/module_model_constants.F`): `r_d = 287.0`,
   `cp = 7*r_d/2 = 1004.5` (`rovcp = 2/7` exact), `r_v = 461.6` (`rvovrd = 461.6/287.0`),
   `xlv = 2.5e6`, `g = 9.81`. `av-wrf` MUST invert WRF state (`T`, `THM`, `PH+PHB`) with WRF's
   own constants so `(PH+PHB)/9.81 == HGT` at ground (`k_w = 0`) with zero datum offset.
   Downstream `windy-av-layer` (`@av/thermodynamics`) then applies its WMO constants
   (`Rd = 287.05`, `cp = 1004`, `g = 9.80665`) uniformly across GFS, ICON-EU, RRFS, and WRF.
2. Hydrostatic `omega = -rho_m * g * w`: `windy-av-layer` (`geometricWFromOmegaMps`) inverts
   `omegaPaS` back to geometric `w` hydrostatically (`-omega * Rd * T / (p * g)`). Omitting
   horizontal pressure advection `v . grad(p')` preserves nonhydrostatic mountain-wave `w`
   across that round-trip within `< 0.7%` (dry `T` vs moist `T_v` factor).
3. Bolton (1980) 2 m saturation guard: WRF Revised MM5 `Q2` can exceed 105% liquid RH in
   nocturnal radiation fog, which trips `windy-av-layer` `column.ts:169` (`unavailable`).
   `clamp_spfh_2m_to_liquid_sat` caps `spfh_2m` at consumer Bolton liquid saturation.
4. Shuell MSLP (`CORDEX-WRF` GMD 2019 `psl_diag=1`) clamps `290.5 K` warm plateaus and `255 K`
   winter cold pools; consumer uses `mslpPa` strictly for map display, never parcel physics.
5. 10 m gust (`Panofsky et al. 1977`; ECMWF IFS `10fg` `C_ugn = 7.71`) uses actual high-terrain
   surface density `rho_sfc = PSFC / (Rd * T_v0)` and moist virtual heat flux (Deardorff 1970;
   Dr. Jack RASP `vhf`) rather than sea-level `rho * cp = 1200`.

References:
- WRF constants: https://github.com/wrf-model/WRF/blob/master/share/module_model_constants.F
- WRF T vs THM: https://forum.mmm.ucar.edu/threads/accessing-variables-on-head_grid.5569/
- WRF SINALPHA/COSALPHA: https://github.com/NCAR/wrf-python/issues/142
- ECMWF IFS 10fg & Bechtold & Bidlot (2009): https://codes.ecmwf.int/grib/param-db/49
- CORDEX-WRF Shuell MSLP: https://gmd.copernicus.org/articles/12/1029/2019/
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
R_DRY_J_KG_K = 287.0
R_VAPOR_J_KG_K = 461.6
CP_DRY_J_KG_K = 3.5 * R_DRY_J_KG_K  # 1004.5 J/(kg*K); KAPPA_DRY = 2/7 exact in WRF
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


def potential_to_temp_k(
    theta_perturb_k: WrfPerturbPotTempK,
    pressure_pa: Pascals,
    *,
    base_temp_k: float = 300.0,
) -> TempK:
    """Convert WRF perturbation potential temperature T = theta - base_temp to sensible T (K)."""
    if pressure_pa <= 0.0:
        raise ValueError("Pressure must be positive")
    theta = float(theta_perturb_k) + base_temp_k
    return TempK(theta * ((float(pressure_pa) / P0_PA) ** KAPPA_DRY))


def moist_thm_to_temp_k(
    thm_perturb_k: WrfPerturbMoistThetaK,
    qvapor_kgkg: MixingRatioKgKg,
    pressure_pa: Pascals,
    *,
    base_temp_k: float = 300.0,
) -> TempK:
    """Convert WRF THM = theta*(1 + (Rv/Rd)*qv) - base_temp to sensible air T (K)."""
    qv = max(0.0, float(qvapor_kgkg))
    theta = (float(thm_perturb_k) + base_temp_k) / (1.0 + MOIST_THETA_COEFF * qv)
    return potential_to_temp_k(
        WrfPerturbPotTempK(theta - base_temp_k), pressure_pa, base_temp_k=base_temp_k
    )


def verify_t_thm_consistency(
    t_dry: CompMass3D,
    thm: CompMass3D,
    p_pert: CompMass3D,
    pb: CompMass3D,
    qv: CompMass3D,
    science: ScienceProfile,
) -> None:
    """Verify wrfout dry `T` and moist `THM` invert to identical sensible T across all cells."""
    td_v = np.frombuffer(t_dry.buf, dtype=np.float32).astype(np.float64)
    thm_v = np.frombuffer(thm.buf, dtype=np.float32).astype(np.float64)
    p_pa = np.frombuffer(p_pert.buf, dtype=np.float32).astype(np.float64) + np.frombuffer(
        pb.buf, dtype=np.float32
    ).astype(np.float64)
    if bool(np.any(p_pa <= 0.0)):
        raise ValueError("Non-positive pressure in wrfout T/THM consistency check")
    exner = np.power(p_pa / P0_PA, KAPPA_DRY)
    tk_dry = (td_v + science.base_temp_k) * exner
    if science.use_theta_m == 1:
        qv_pos = np.maximum(0.0, np.frombuffer(qv.buf, dtype=np.float32).astype(np.float64))
        theta_m_dry = (thm_v + science.base_temp_k) / (1.0 + MOIST_THETA_COEFF * qv_pos)
    else:
        theta_m_dry = thm_v + science.base_temp_k
    tk_moist = theta_m_dry * exner
    diff = np.abs(tk_dry - tk_moist)
    worst = int(np.argmax(diff))
    if float(diff[worst]) > 0.05:
        raise ValueError(
            f"wrfout T vs THM mismatch ({tk_dry[worst]:.3f} vs {tk_moist[worst]:.3f} K) "
            f"at index {worst}"
        )


def mixing_ratio_to_spfh(qvapor_kgkg: MixingRatioKgKg) -> SpecificHumidityKgKg:
    """Convert water vapor mixing ratio r >= 0 to specific humidity q = r / (1 + r)."""
    r = max(0.0, float(qvapor_kgkg))
    return SpecificHumidityKgKg(r / (1.0 + r))


def clamp_spfh_2m_to_liquid_sat(
    spfh_2m_kgkg: float,
    t2_k: float,
    psfc_pa: float,
) -> float:
    """Cap diagnostic 2 m specific humidity at consumer Bolton (1980) liquid saturation."""
    q = max(0.0, spfh_2m_kgkg)
    tc = t2_k - CONSUMER_KELVIN_OFFSET
    es_pa = 611.2 * math.exp((17.67 * tc) / max(1.0, tc + 243.5))
    if psfc_pa <= es_pa * 1.01:
        return q
    r_sat = CONSUMER_EPSILON * es_pa / (psfc_pa - es_pa)
    q_sat_safe = (r_sat / (1.0 + r_sat)) * (1.0 - 2.0**-14)
    return min(q, q_sat_safe)


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


def hypsometric_mslp_pa(psfc_pa: float, terrain_m: float, t2_k: float, spfh_2m: float) -> float:
    """Reduce PSFC to MSLP with WMO / WRF Shuell (290.5 K / 255 K) plateau clamping."""
    z_m = max(0.0, terrain_m)
    tv_sfc = t2_k * (1.0 + VIRTUAL_TEMP_COEFF * max(0.0, spfh_2m))
    tv_0 = 0.5 * (255.0 + tv_sfc) if tv_sfc < 255.0 else tv_sfc
    tv_sl = tv_0 + STD_LAPSE_K_PER_M * z_m
    if tv_0 <= 290.5 and tv_sl > 290.5:
        tv_sl = 290.5 - 0.005 * ((tv_0 - 290.5) ** 2)
    elif tv_0 > 290.5 and tv_sl > 290.5:
        tv_0 = 0.5 * (290.5 + tv_0)
        tv_sl = tv_0
    tv_mean = 0.5 * (tv_0 + tv_sl)
    return psfc_pa * math.exp((G0_MPS2 * z_m) / (R_DRY_J_KG_K * max(180.0, tv_mean)))


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
    mean_10m = math.hypot(u10_mps, v10_mps)
    q2 = max(0.0, spfh_2m)
    tk = max(200.0, t2_k)
    tv0 = tk * (1.0 + VIRTUAL_TEMP_COEFF * q2)
    rho_sfc = max(10_000.0, psfc_pa) / (R_DRY_J_KG_K * tv0)
    virt_temp_flux = (1.0 + VIRTUAL_TEMP_COEFF * q2) * (
        sensible_flux_wm2 / (rho_sfc * CP_DRY_J_KG_K)
    ) + VIRTUAL_TEMP_COEFF * tk * (latent_flux_wm2 / (rho_sfc * LV_VAPOR_J_KG))
    wstar_cubed = (G0_MPS2 * max(0.0, pblh_m) / tv0) * max(0.0, virt_temp_flux)
    ustar_cubed = max(0.0, ustar_mps) ** 3
    turb_eddy = IFS_GUST_COEFF * math.pow(
        ustar_cubed + (0.5 * VON_KARMAN / 12.0) * wstar_cubed, 1.0 / 3.0
    )
    return max(wspd10max_mps, mean_10m + turb_eddy)


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
    """Compute mean rate across (t_prev, t_curr] from WRF bucketed cumulative totals."""
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
