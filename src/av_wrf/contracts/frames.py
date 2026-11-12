"""Frozen computational-grid WRF input and Published-Grid normalized frame contracts."""

from __future__ import annotations

from dataclasses import dataclass

from av_wrf.contracts.quantities import (
    CompBucket2D,
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    PubMass2D,
    PubMass3D,
)


@dataclass(frozen=True)
class RawWrfBaselineInput:
    """Minimal 2D computational-grid baseline fields (avoids reading 3D volumes)."""

    valid_time_utc: str
    valid_time_unix_s: int
    terrain_m: CompMass2D
    psfc_pa: CompMass2D
    achfx_j_m2: CompMass2D
    aclhf_j_m2: CompMass2D
    rainc_mm: CompMass2D
    i_rainc: CompBucket2D
    rainnc_mm: CompMass2D
    i_rainnc: CompBucket2D


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
    hfx_wm2: CompMass2D
    lh_wm2: CompMass2D
    ustar_mps: CompMass2D
    pblh_m: CompMass2D
    afwa_cape_jkg: CompMass2D
    achfx_j_m2: CompMass2D
    aclhf_j_m2: CompMass2D
    rainc_mm: CompMass2D
    i_rainc: CompBucket2D
    rainnc_mm: CompMass2D
    i_rainnc: CompBucket2D
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
    afwa_mslp_pa: CompMass2D | None = None


@dataclass(frozen=True)
class NormalizedWrfFrame:
    """All 22 Published-Grid arrays plus full-domain column and output-frame Courant invariants."""

    valid_time_utc: str
    window_start_unix_s: int
    window_end_unix_s: int
    fields_2d: dict[str, PubMass2D]
    fields_3d: dict[str, PubMass3D]
    min_dz_m: float
    max_dz_m: float
    min_bl_levels_2km: int
    max_cfl: float


__all__ = ["NormalizedWrfFrame", "RawWrfBaselineInput", "RawWrfStepInput"]
