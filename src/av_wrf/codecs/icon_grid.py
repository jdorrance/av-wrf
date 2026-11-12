"""Geometry and interpolation weights for a regular ICON cutout."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from av_wrf.codecs.wrf_nc_grid import normalize_rows


@dataclass(frozen=True)
class IconGrid:
    west: float
    south: float
    east: float
    north: float
    spacing: float
    nx: int
    ny: int
    uuid: str
    count: int
    donors: NDArray[np.intp]
    weights: NDArray[np.float64]


def icon_grid(
    uuid: str,
    lat: NDArray[np.float64],
    lon: NDArray[np.float64],
    bounds: tuple[float, float, float, float],
    spacing: float,
) -> IconGrid:
    """Build barycentric weights for target points covered by the native grid."""
    from scipy.spatial import Delaunay

    west, south, east, north = bounds
    middle = 0.5 * (west + east)
    lon = middle + (lon - middle + 180.0) % 360.0 - 180.0
    margin = 1.0
    nearby = np.flatnonzero(
        (lon >= west - margin)
        & (lon <= east + margin)
        & (lat >= south - margin)
        & (lat <= north + margin)
    )
    if nearby.size < 3:
        raise ValueError("ICON grid does not cover the WPS cutout")
    lon_scale = float(np.cos(np.deg2rad(0.5 * (south + north))))
    donors = np.column_stack((lon[nearby] * lon_scale, lat[nearby]))
    nx, ny = round((east - west) / spacing) + 1, round((north - south) / spacing) + 1
    gx, gy = np.meshgrid(west + spacing * np.arange(nx), south + spacing * np.arange(ny))
    targets = np.column_stack((gx.ravel() * lon_scale, gy.ravel()))
    mesh = Delaunay(donors)
    simplex = np.asarray(mesh.find_simplex(targets), dtype=np.intp)
    if np.any(simplex < 0):
        raise ValueError("ICON grid does not cover the full WPS cutout")
    vertices = np.asarray(mesh.simplices[simplex], dtype=np.intp)
    separation = np.linalg.norm(donors[vertices] - targets[:, None, :], axis=2)
    if np.max(separation) > 3.0 * spacing:
        raise ValueError("ICON grid has a gap in the WPS cutout")
    transforms = np.asarray(mesh.transform[simplex], dtype=np.float64)
    local = np.einsum("nij,nj->ni", transforms[:, :2], targets - transforms[:, 2])
    weights = np.maximum(np.column_stack((local, 1.0 - local.sum(axis=1))), 0.0)
    normalize_rows(weights, "the native grid")
    return IconGrid(
        west, south, east, north, spacing, nx, ny, uuid, lat.size, nearby[vertices], weights
    )


__all__ = ["IconGrid", "icon_grid"]
