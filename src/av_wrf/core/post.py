"""Stateful full-frame WRF -> Published-Grid Zarr slab post-processing (`post_frame`).

Assumptions & Field Semantics:
- Trailing statistical windows: `sensible_heat_flux_wm2`, `latent_heat_flux_wm2`, and
  `precip_mm_hr` represent trailing-window means across `(t_prev, t_curr]` (`1 h` in `f001..f036`,
  `3 h` in `f039..f048`). Surface heat fluxes allow signed negative nocturnal values; rainfall
  requires non-negative cumulative deltas.
- Low-cloud threshold: `p >= 0.8 * PSFC` follows ECMWF terrain-following sigma (`LCC`) so high
  plateaus (`PSFC < 800 hPa`) retain true low-cloud / fog detection; note YSU (`bl_pbl_physics=1`)
  without `icloud_bl` can under-diagnose subgrid fair-weather shallow `Cu` in `CLDFRA`.
- `height_msl_m`: `(PH + PHB) / 9.81` destaggered at exact `eta` midpoints (`0.5*(face_k+face_k+1)`
  per Skamarock et al. 2019 Sec. 3.2.1), matching `terrain_m` (`HGT`) datum at ground.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass

from av_wrf.contracts import (
    AccumBaseline,
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    GridWindMps,
    MixingRatioKgKg,
    Pascals,
    PubMass2D,
    PubMass3D,
    ScienceProfile,
    Slab2DF32,
    Slab3DF32,
    SpecificHumidityKgKg,
    TempK,
    VerticalVelocityMps,
    WrfPerturbPotTempK,
)
from av_wrf.core.grid import (
    crop_sponge_2d,
    crop_sponge_3d,
    destagger_x_3d,
    destagger_y_3d,
    destagger_z_3d,
)
from av_wrf.core.thermo import (
    G0_MPS2,
    clamp_spfh_2m_to_liquid_sat,
    geometric_w_to_omega_pas,
    groom_mantissa_f32,
    hypsometric_mslp_pa,
    max_random_cloud_pct,
    mixing_ratio_to_spfh,
    parameterized_gust_10m_mps,
    potential_to_temp_k,
    rotate_grid_to_earth,
    window_rate_from_accum,
)


@dataclass(frozen=True)
class RawWrfStepInput:
    """Croppable computational-grid WRF fields for one output timestamp."""

    valid_time_utc: str
    valid_time_unix_s: int
    terrain_m: CompMass2D
    cos_alpha: CompMass2D
    sin_alpha: CompMass2D
    psfc_pa: CompMass2D
    t2_k: CompMass2D
    q2_kgkg: CompMass2D
    u10_grid_mps: CompMass2D
    v10_grid_mps: CompMass2D
    wspd10max_mps: CompMass2D
    ustar_mps: CompMass2D
    pblh_m: CompMass2D
    afwa_cape_jkg: CompMass2D
    achfx_j_m2: CompMass2D
    i_achfx: tuple[int, ...]
    aclhf_j_m2: CompMass2D
    i_aclhf: tuple[int, ...]
    rainc_mm: CompMass2D
    i_rainc: tuple[int, ...]
    rainnc_mm: CompMass2D
    i_rainnc: tuple[int, ...]
    p_pert_pa: CompMass3D
    pb_pa: CompMass3D
    ph_stag_m2s2: CompStagZ3D
    phb_stag_m2s2: CompStagZ3D
    theta_pert_k: CompMass3D
    qvapor_kgkg: CompMass3D
    cldfra: CompMass3D
    u_stag_mps: CompStagX3D
    v_stag_mps: CompStagY3D
    w_stag_mps: CompStagZ3D


@dataclass(frozen=True)
class NormalizedWrfFrame:
    """All 22 Published-Grid arrays ready for Zarr v3 shard encoding."""

    valid_time_utc: str
    window_start_unix_s: int
    window_end_unix_s: int
    fields_2d: dict[str, PubMass2D]
    fields_3d: dict[str, PubMass3D]


def build_accum_baseline(raw: RawWrfStepInput, trim: int) -> AccumBaseline:
    """Extract cropped Published-Grid cumulative baseline from f000 or a restart frame."""
    return AccumBaseline(
        valid_time_utc=raw.valid_time_utc,
        valid_time_unix_s=raw.valid_time_unix_s,
        achfx_j_m2=crop_sponge_2d(raw.achfx_j_m2, trim),
        i_achfx=raw.i_achfx,
        aclhf_j_m2=crop_sponge_2d(raw.aclhf_j_m2, trim),
        i_aclhf=raw.i_aclhf,
        rainc_mm=crop_sponge_2d(raw.rainc_mm, trim),
        i_rainc=raw.i_rainc,
        rainnc_mm=crop_sponge_2d(raw.rainnc_mm, trim),
        i_rainnc=raw.i_rainnc,
    )


def _groom_2d(slab: PubMass2D, keepbits: int) -> PubMass2D:
    buf = array("f", (groom_mantissa_f32(v, keepbits) for v in slab.buf))
    return PubMass2D(Slab2DF32(ny=slab.ny, nx=slab.nx, buf=buf))


def _compute_cloud_slabs(
    cld_all: PubMass3D,
    p_out: array[float],
    psfc: PubMass2D,
    k_exp: int,
) -> tuple[array[float], array[float]]:
    plane = psfc.ny * psfc.nx
    cld_tot, cld_low = array("f"), array("f")
    for idx in range(plane):
        c_full = [cld_all.buf[k * plane + idx] for k in range(cld_all.nz)]
        c_low = [
            cld_all.buf[k * plane + idx]
            for k in range(k_exp)
            if p_out[k * plane + idx] >= 0.8 * psfc.buf[idx]
        ]
        cld_tot.append(groom_mantissa_f32(max_random_cloud_pct(c_full), 15))
        cld_low.append(groom_mantissa_f32(max_random_cloud_pct(c_low), 15))
    return cld_tot, cld_low


def _post_3d_slabs(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
) -> tuple[dict[str, PubMass3D], array[float], array[float]]:
    trim, k_exp = domain.bdy_trim_cells, domain.exported_levels_k
    ca, sa = crop_sponge_2d(raw.cos_alpha, trim), crop_sponge_2d(raw.sin_alpha, trim)
    psfc = crop_sponge_2d(raw.psfc_pa, trim)
    p_pert = crop_sponge_3d(raw.p_pert_pa, trim, k_exp)
    pb = crop_sponge_3d(raw.pb_pa, trim, k_exp)
    ph = crop_sponge_3d(destagger_z_3d(raw.ph_stag_m2s2), trim, k_exp)
    phb = crop_sponge_3d(destagger_z_3d(raw.phb_stag_m2s2), trim, k_exp)
    th_p = crop_sponge_3d(raw.theta_pert_k, trim, k_exp)
    qv = crop_sponge_3d(raw.qvapor_kgkg, trim, k_exp)
    cld_all = crop_sponge_3d(raw.cldfra, trim, raw.cldfra.nz)
    u_g = crop_sponge_3d(destagger_x_3d(raw.u_stag_mps), trim, k_exp)
    v_g = crop_sponge_3d(destagger_y_3d(raw.v_stag_mps), trim, k_exp)
    w_m = crop_sponge_3d(destagger_z_3d(raw.w_stag_mps), trim, k_exp)

    ny, nx = psfc.ny, psfc.nx
    plane = ny * nx
    p_o, z_o, t_o, q_o = array("f"), array("f"), array("f"), array("f")
    ue_o, ve_o, om_o = array("f"), array("f"), array("f")
    for k in range(k_exp):
        for idx in range(plane):
            pos = k * plane + idx
            p_pa = p_pert.buf[pos] + pb.buf[pos]
            tk = float(potential_to_temp_k(
                WrfPerturbPotTempK(th_p.buf[pos]), Pascals(p_pa), base_temp_k=science.base_temp_k
            ))
            sp = float(mixing_ratio_to_spfh(MixingRatioKgKg(qv.buf[pos])))
            om = float(geometric_w_to_omega_pas(
                VerticalVelocityMps(w_m.buf[pos]),
                Pascals(p_pa),
                TempK(tk),
                SpecificHumidityKgKg(sp),
            ))
            ue, ve = rotate_grid_to_earth(
                GridWindMps(u_g.buf[pos]), GridWindMps(v_g.buf[pos]), ca.buf[idx], sa.buf[idx]
            )
            p_o.append(groom_mantissa_f32(p_pa, 17))
            z_o.append(groom_mantissa_f32((ph.buf[pos] + phb.buf[pos]) / G0_MPS2, 17))
            t_o.append(groom_mantissa_f32(tk, 15))
            q_o.append(groom_mantissa_f32(sp, 15))
            ue_o.append(groom_mantissa_f32(float(ue), 15))
            ve_o.append(groom_mantissa_f32(float(ve), 15))
            om_o.append(groom_mantissa_f32(om, 15))

    cld_tot, cld_low = _compute_cloud_slabs(cld_all, p_o, psfc, k_exp)
    f3d = {
        "pressure_pa": PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=p_o)),
        "height_msl_m": PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=z_o)),
        "temp_k": PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=t_o)),
        "spfh_kgkg": PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=q_o)),
        "u_earth_mps": PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=ue_o)),
        "v_earth_mps": PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=ve_o)),
        "omega_pas": PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=om_o)),
    }
    return f3d, cld_tot, cld_low


def _accum_cell_rates(
    idx: int,
    prev: AccumBaseline,
    curr: AccumBaseline,
    science: ScienceProfile,
    dt_s: float,
) -> tuple[float, float, float]:
    sh = window_rate_from_accum(
        prev.achfx_j_m2.buf[idx], prev.i_achfx[idx],
        curr.achfx_j_m2.buf[idx], curr.i_achfx[idx],
        science.bucket_j, dt_s, allow_negative=True,
    )
    lh = window_rate_from_accum(
        prev.aclhf_j_m2.buf[idx], prev.i_aclhf[idx],
        curr.aclhf_j_m2.buf[idx], curr.i_aclhf[idx],
        science.bucket_j, dt_s, allow_negative=True,
    )
    rc = window_rate_from_accum(
        prev.rainc_mm.buf[idx], prev.i_rainc[idx],
        curr.rainc_mm.buf[idx], curr.i_rainc[idx],
        science.bucket_mm, dt_s, per_hour=True,
    )
    rnc = window_rate_from_accum(
        prev.rainnc_mm.buf[idx], prev.i_rainnc[idx],
        curr.rainnc_mm.buf[idx], curr.i_rainnc[idx],
        science.bucket_mm, dt_s, per_hour=True,
    )
    return sh, lh, rc + rnc


def _post_surface_slabs(
    prev: AccumBaseline,
    curr_base: AccumBaseline,
    raw: RawWrfStepInput,
    trim: int,
    science: ScienceProfile,
    dt_s: float,
    cld_tot: array[float],
    cld_low: array[float],
) -> dict[str, PubMass2D]:
    hgt = _groom_2d(crop_sponge_2d(raw.terrain_m, trim), 17)
    psfc = _groom_2d(crop_sponge_2d(raw.psfc_pa, trim), 17)
    t2 = _groom_2d(crop_sponge_2d(raw.t2_k, trim), 15)
    ca, sa = crop_sponge_2d(raw.cos_alpha, trim), crop_sponge_2d(raw.sin_alpha, trim)
    q2 = crop_sponge_2d(raw.q2_kgkg, trim)
    u10g, v10g = crop_sponge_2d(raw.u10_grid_mps, trim), crop_sponge_2d(raw.v10_grid_mps, trim)
    wmax, ust = crop_sponge_2d(raw.wspd10max_mps, trim), crop_sponge_2d(raw.ustar_mps, trim)
    pblh, cape = crop_sponge_2d(raw.pblh_m, trim), crop_sponge_2d(raw.afwa_cape_jkg, trim)
    ny, nx = hgt.ny, hgt.nx

    q2_s, shf, lhf, prcp = array("f"), array("f"), array("f"), array("f")
    mslp, u10e, v10e, gust = array("f"), array("f"), array("f"), array("f")
    for idx in range(ny * nx):
        raw_sp2 = float(mixing_ratio_to_spfh(MixingRatioKgKg(q2.buf[idx])))
        sp2 = clamp_spfh_2m_to_liquid_sat(raw_sp2, t2.buf[idx], psfc.buf[idx])
        sh, lh, pr = _accum_cell_rates(idx, prev, curr_base, science, dt_s)
        ue, ve = rotate_grid_to_earth(
            GridWindMps(u10g.buf[idx]), GridWindMps(v10g.buf[idx]), ca.buf[idx], sa.buf[idx]
        )
        q2_s.append(groom_mantissa_f32(sp2, 15))
        shf.append(groom_mantissa_f32(sh, 15))
        lhf.append(groom_mantissa_f32(lh, 15))
        prcp.append(groom_mantissa_f32(pr, 15))
        mslp.append(groom_mantissa_f32(
            hypsometric_mslp_pa(psfc.buf[idx], hgt.buf[idx], t2.buf[idx], sp2), 17
        ))
        u10e.append(groom_mantissa_f32(float(ue), 15))
        v10e.append(groom_mantissa_f32(float(ve), 15))
        gust.append(groom_mantissa_f32(parameterized_gust_10m_mps(
            float(ue), float(ve), wmax.buf[idx], ust.buf[idx],
            sh, lh, pblh.buf[idx], t2.buf[idx], sp2, psfc.buf[idx],
        ), 15))

    def wrap(buf: array[float]) -> PubMass2D:
        return PubMass2D(Slab2DF32(ny=ny, nx=nx, buf=buf))

    return {
        "terrain_m": hgt, "surface_pressure_pa": psfc,
        "temp_2m_k": t2, "spfh_2m_kgkg": wrap(q2_s),
        "sensible_heat_flux_wm2": wrap(shf), "latent_heat_flux_wm2": wrap(lhf),
        "precip_mm_hr": wrap(prcp), "mslp_pa": wrap(mslp),
        "u_10m_earth_mps": wrap(u10e), "v_10m_earth_mps": wrap(v10e),
        "gust_10m_mps": wrap(gust), "pbl_height_m": _groom_2d(pblh, 15),
        "cape_jkg": _groom_2d(cape, 15),
        "cloud_total_pct": wrap(cld_tot), "cloud_low_pct": wrap(cld_low),
    }


def post_frame(
    prev: AccumBaseline,
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
) -> tuple[NormalizedWrfFrame, AccumBaseline]:
    """Transform one raw WRF step + predecessor baseline into all 22 Published-Grid arrays."""
    dt_s = float(raw.valid_time_unix_s - prev.valid_time_unix_s)
    if dt_s <= 0.0:
        raise ValueError("raw.valid_time_unix_s must strictly exceed prev.valid_time_unix_s")
    fields_3d, cld_tot, cld_low = _post_3d_slabs(raw, domain, science)
    next_base = build_accum_baseline(raw, domain.bdy_trim_cells)
    fields_2d = _post_surface_slabs(
        prev, next_base, raw, domain.bdy_trim_cells, science, dt_s, cld_tot, cld_low
    )
    frame = NormalizedWrfFrame(
        valid_time_utc=raw.valid_time_utc,
        window_start_unix_s=prev.valid_time_unix_s,
        window_end_unix_s=raw.valid_time_unix_s,
        fields_2d=fields_2d,
        fields_3d=fields_3d,
    )
    return frame, next_base
