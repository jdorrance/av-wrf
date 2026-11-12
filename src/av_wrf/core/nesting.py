"""Concentric 1-way parent-buffer geometry for coarse-source downscaling."""

from __future__ import annotations

import dataclasses

from av_wrf.contracts import DomainProfile

# Conservative meters per degree at the equator; the meridional spacing of every supported
# source (GFS 0.25 deg, IFS 0.1 deg, ICON-EU 0.0625 deg) is at or below this bound.
M_PER_DEG_EQUATORIAL = 111_320.0


def nesting_active(domain: DomainProfile, source_grid_spacing_deg: float) -> bool:
    """Return whether a 1-way parent buffer is required for the source-to-child jump."""
    if domain.parent_grid_ratio <= 1:
        return False
    source_spacing_m = source_grid_spacing_deg * M_PER_DEG_EQUATORIAL
    return source_spacing_m > domain.nest_trigger_ratio * max(domain.dx_m, domain.dy_m)


def parent_domain_shape(domain: DomainProfile) -> tuple[int, int]:
    """Return concentric d01 `(e_we, e_sn)` covering the child plus symmetric halos."""
    if domain.parent_grid_ratio <= 1 or domain.parent_halo_cells < 1:
        raise ValueError("parent_domain_shape requires parent_grid_ratio > 1 and halo cells")
    r = domain.parent_grid_ratio
    coarse_nx, coarse_ny = (domain.e_we - 1) // r, (domain.e_sn - 1) // r
    halo = 2 * domain.parent_halo_cells
    return coarse_nx + halo + 1, coarse_ny + halo + 1


def forcing_domain(domain: DomainProfile, source_grid_spacing_deg: float) -> DomainProfile:
    """Return the outermost active domain used to size forcing cutouts and geogrid."""
    if not nesting_active(domain, source_grid_spacing_deg):
        return domain
    e_we, e_sn = parent_domain_shape(domain)
    # Parent and child share center/std parallels; only extent and grid ratio change so the
    # published child coordinates are identical to the single-domain run (proven in tests).
    return dataclasses.replace(
        domain,
        e_we=e_we,
        e_sn=e_sn,
        dx_m=domain.dx_m * domain.parent_grid_ratio,
        dy_m=domain.dy_m * domain.parent_grid_ratio,
        parent_grid_ratio=1,
        parent_halo_cells=0,
    )


__all__ = [
    "M_PER_DEG_EQUATORIAL",
    "forcing_domain",
    "nesting_active",
    "parent_domain_shape",
]
