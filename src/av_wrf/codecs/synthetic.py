"""Synthetic computational-grid WRF step builder for local smoke runs and tests."""

from __future__ import annotations

from array import array

from av_wrf.contracts import (
    CompBucket2D,
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    RawWrfStepInput,
    Slab2DF32,
    Slab2DI32,
    Slab3DF32,
)
from av_wrf.core import MOIST_THETA_COEFF, estimate_auto_levels_agl_m


def _synthetic_base_3d(
    domain: DomainProfile,
) -> tuple[CompMass3D, CompStagZ3D, CompStagZ3D, CompStagX3D, CompStagY3D, CompStagZ3D]:
    ny, nx, nz = domain.e_sn - 1, domain.e_we - 1, domain.e_vert - 1
    n2 = ny * nx
    mids = estimate_auto_levels_agl_m(domain, terrain_m=1200.0, etac=0.2)
    faces_agl = [0.0]
    for m in mids:
        faces_agl.append(faces_agl[-1] + 2.0 * (m - faces_agl[-1]))
    p_pb = array("f")
    z_phb = array("f", [(1200.0 + faces_agl[0]) * 9.81]) * n2
    for k in range(nz):
        p_pb.extend(array("f", [88_000.0 * (0.97**k)]) * n2)
        z_phb.extend(array("f", [(1200.0 + faces_agl[k + 1]) * 9.81]) * n2)
    pb = CompMass3D(Slab3DF32(nz=nz, ny=ny, nx=nx, buf=p_pb))
    ph = CompStagZ3D(Slab3DF32(nz=nz + 1, ny=ny, nx=nx, buf=array("f", [0.0]) * ((nz + 1) * n2)))
    phb = CompStagZ3D(Slab3DF32(nz=nz + 1, ny=ny, nx=nx, buf=z_phb))
    u = CompStagX3D(
        Slab3DF32(nz=nz, ny=ny, nx=nx + 1, buf=array("f", [6.0]) * (nz * ny * (nx + 1)))
    )
    v = CompStagY3D(
        Slab3DF32(nz=nz, ny=ny + 1, nx=nx, buf=array("f", [2.0]) * (nz * (ny + 1) * nx))
    )
    w = CompStagZ3D(Slab3DF32(nz=nz + 1, ny=ny, nx=nx, buf=array("f", [0.1]) * ((nz + 1) * n2)))
    return pb, ph, phb, u, v, w


def synthetic_thm_from_dry_theta(
    theta_pert_k: float, qv_kgkg: float, base_temp_k: float = 300.0
) -> float:
    """Compute exact WRF THM perturbation corresponding to dry T perturbation and QVAPOR."""
    return (theta_pert_k + base_temp_k) * (1.0 + MOIST_THETA_COEFF * qv_kgkg) - base_temp_k


def build_synthetic_wrf_step(
    domain: DomainProfile, valid_time_utc: str, valid_time_unix_s: int, lead_hour: int
) -> RawWrfStepInput:
    """Build a physically consistent computational-grid WRF step for local smoke runs."""
    ny, nx, nz = domain.e_sn - 1, domain.e_we - 1, domain.e_vert - 1
    n2 = ny * nx

    def c2(val: float) -> CompMass2D:
        return CompMass2D(Slab2DF32(ny=ny, nx=nx, buf=array("f", [val]) * n2))

    def ci(val: int) -> CompBucket2D:
        return CompBucket2D(Slab2DI32(ny=ny, nx=nx, buf=array("i", [val]) * n2))

    def c3(val: float) -> CompMass3D:
        return CompMass3D(Slab3DF32(nz=nz, ny=ny, nx=nx, buf=array("f", [val]) * (nz * n2)))

    pb, ph, phb, u_s, v_s, w_s = _synthetic_base_3d(domain)
    zeros = ci(0)
    return RawWrfStepInput(
        valid_time_utc=valid_time_utc,
        valid_time_unix_s=valid_time_unix_s,
        terrain_m=c2(1200.0),
        cos_alpha=c2(1.0),
        sin_alpha=c2(0.0),
        psfc_pa=c2(88_000.0),
        t2_k=c2(294.0),
        q2_kgkg=c2(0.007),
        u10_grid_mps=c2(4.5),
        v10_grid_mps=c2(1.5),
        wspd10max_mps=c2(6.0),
        ustar_mps=c2(0.42),
        pblh_m=c2(1900.0),
        afwa_cape_jkg=c2(320.0),
        achfx_j_m2=c2(float(lead_hour) * 240.0 * 3600.0),
        i_achfx=zeros,
        aclhf_j_m2=c2(float(lead_hour) * 80.0 * 3600.0),
        i_aclhf=zeros,
        rainc_mm=c2(0.0),
        i_rainc=zeros,
        rainnc_mm=c2(float(lead_hour) * 0.25),
        i_rainnc=zeros,
        p_pert_pa=c3(0.0),
        pb_pa=pb,
        ph_stag_m2s2=ph,
        phb_stag_m2s2=phb,
        theta_pert_k=c3(8.0),
        qvapor_kgkg=c3(0.005),
        cldfra=c3(0.15),
        u_stag_mps=u_s,
        v_stag_mps=v_s,
        w_stag_mps=w_s,
    )


__all__ = ["build_synthetic_wrf_step", "synthetic_thm_from_dry_theta"]
