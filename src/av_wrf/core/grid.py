"""WPS spherical Lambert geometry, mass-grid cropping, and destaggering."""

from __future__ import annotations

import math
from array import array

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import (
    WRF_SPHERE_RADIUS_M,
    CompBucket2D,
    CompMass2D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    PubBucket2D,
    PubMass2D,
    PubMass3D,
    Slab2DF32,
    Slab2DI32,
    Slab3DF32,
)

MIN_DOMAIN_MASS_CELLS = 100


def _to_f32_array(arr: npt.NDArray[np.float32]) -> array[float]:
    out = array("f")
    out.frombytes(memoryview(np.ascontiguousarray(arr, dtype=np.float32)).cast("B"))
    return out


def validate_domain_profile(domain: DomainProfile) -> None:
    """Enforce the WPS Lambert coordinate and computational domain limits."""
    # WPS geogrid passes one Lambert spacing and uses its default spherical radius.
    if domain.dx_m != domain.dy_m or not 0.0 < domain.dx_m < math.inf:
        raise ValueError("WPS Lambert grid spacing must be equal, finite and positive")
    if domain.earth_radius_m != WRF_SPHERE_RADIUS_M:
        raise ValueError(f"WPS Lambert Earth radius must be {WRF_SPHERE_RADIUS_M:.0f} m")
    if not (abs(domain.center_lon_deg) <= 180.0 and abs(domain.central_meridian_deg) <= 180.0):
        raise ValueError("Lambert longitudes must lie in [-180, 180] degrees")
    p1, p2 = domain.std_parallel_1_deg, domain.std_parallel_2_deg
    # WPS uses one hemisphere (selected by TRUELAT1) for both standard parallels.
    if not (abs(domain.center_lat_deg) < 90.0 and max(abs(p1), abs(p2)) < 90.0 and p1 * p2 > 0):
        raise ValueError("WPS Lambert standard parallels must share a hemisphere between the poles")
    _cone_constants(p1, p2, domain.center_lat_deg, domain.earth_radius_m)
    comp_nj, comp_ni = domain.e_sn - 1, domain.e_we - 1
    if comp_ni < MIN_DOMAIN_MASS_CELLS or comp_nj < MIN_DOMAIN_MASS_CELLS:
        raise ValueError(
            f"Domain policy requires >= {MIN_DOMAIN_MASS_CELLS}x{MIN_DOMAIN_MASS_CELLS} "
            f"mass cells (e_we >= {MIN_DOMAIN_MASS_CELLS + 1}, got {domain.e_we}x{domain.e_sn})"
        )
    published_grid_shape(domain)


def _wrap_lon_rad(delta_rad: float) -> float:
    return (delta_rad + math.pi) % (2.0 * math.pi) - math.pi


def _effective_second_parallel(p1: float, p2: float) -> float:
    # WPS lc_cone selects tangent geometry using default REAL arithmetic.
    return p1 if abs(np.float32(p1) - np.float32(p2)) <= np.float32(0.1) else p2


def _cone_constants(
    std_lat1_deg: float, std_lat2_deg: float, origin_lat_deg: float, radius_m: float
) -> tuple[float, float, float]:
    if not all(-90.0 < lat < 90.0 for lat in (std_lat1_deg, std_lat2_deg, origin_lat_deg)):
        raise ValueError("Lambert projection latitudes must lie between the poles")
    phi1 = math.radians(std_lat1_deg)
    phi2 = math.radians(_effective_second_parallel(std_lat1_deg, std_lat2_deg))
    phi0 = math.radians(origin_lat_deg)
    t1, t2 = math.tan(0.25 * math.pi + 0.5 * phi1), math.tan(0.25 * math.pi + 0.5 * phi2)
    t0 = math.tan(0.25 * math.pi + 0.5 * phi0)
    n = (
        math.sin(phi1)
        if phi1 == phi2
        else math.log(math.cos(phi1) / math.cos(phi2)) / math.log(t2 / t1)
    )
    if abs(n) < 1e-12:
        raise ValueError("Lambert standard parallels define a zero cone")
    f_const = (math.cos(phi1) * (t1**n)) / n
    return n, f_const, radius_m * f_const / (t0**n)


def latlon_to_lambert_xy_m(
    lat_deg: float,
    lon_deg: float,
    *,
    origin_lat_deg: float,
    central_meridian_deg: float,
    std_parallel_1_deg: float,
    std_parallel_2_deg: float,
    radius_m: float,
) -> tuple[float, float]:
    """Project geographic (lat, lon) in degrees to spherical Lambert (x, y) in meters."""
    n, f_const, rho0 = _cone_constants(
        std_parallel_1_deg, std_parallel_2_deg, origin_lat_deg, radius_m
    )
    phi = math.radians(lat_deg)
    theta = n * _wrap_lon_rad(math.radians(lon_deg - central_meridian_deg))
    rho = radius_m * f_const / (math.tan(0.25 * math.pi + 0.5 * phi) ** n)
    return rho * math.sin(theta), rho0 - rho * math.cos(theta)


def lambert_xy_m_to_latlon(
    x_m: float,
    y_m: float,
    *,
    origin_lat_deg: float,
    central_meridian_deg: float,
    std_parallel_1_deg: float,
    std_parallel_2_deg: float,
    radius_m: float,
) -> tuple[float, float]:
    """Invert spherical Lambert (x, y) in meters to (lat_deg, lon_deg) across NH and SH."""
    n, f_const, rho0 = _cone_constants(
        std_parallel_1_deg, std_parallel_2_deg, origin_lat_deg, radius_m
    )
    sign_n, dy = (1.0 if n >= 0.0 else -1.0), rho0 - y_m
    rho, theta = sign_n * math.hypot(x_m, dy), math.atan2(sign_n * x_m, sign_n * dy)
    lat_rad = 2.0 * math.atan(math.pow(radius_m * f_const / rho, 1.0 / n)) - 0.5 * math.pi
    lon_rad = _wrap_lon_rad(math.radians(central_meridian_deg) + theta / n)
    return math.degrees(lat_rad), math.degrees(lon_rad)


def lambert_grid_to_earth_trig(
    domain: DomainProfile, longitude: npt.NDArray[np.float32]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Return grid-to-Earth cosine and sine using the signed Lambert cone."""
    p1, p2 = domain.std_parallel_1_deg, domain.std_parallel_2_deg
    n, _, _ = _cone_constants(p1, p2, domain.center_lat_deg, domain.earth_radius_m)
    # Preserve the sign of a 180° tie as in WPS get_rotang.
    delta = domain.central_meridian_deg - longitude.astype(np.float64)
    delta -= 360.0 * (delta > 180.0) - 360.0 * (delta < -180.0)
    angle = np.deg2rad(n * delta)
    return np.cos(angle), np.sin(angle)


def lambert_map_scale(
    lat_deg: float,
    *,
    std_parallel_1_deg: float,
    std_parallel_2_deg: float,
    origin_lat_deg: float,
    radius_m: float,
) -> float:
    """Return isotropic Lambert map scale factor m = projected_step / earth_step at lat_deg."""
    if abs(lat_deg) >= 89.999:
        raise ValueError("Lambert map scale is singular at the poles")
    n, f_const, _ = _cone_constants(
        std_parallel_1_deg, std_parallel_2_deg, origin_lat_deg, radius_m
    )
    phi = math.radians(lat_deg)
    rho = radius_m * f_const / math.pow(math.tan(0.25 * math.pi + 0.5 * phi), n)
    return abs(n * rho) / (radius_m * math.cos(phi))


def published_grid_shape(domain: DomainProfile) -> tuple[int, int]:
    """Return (nj, ni) mass-point dimensions after cropping bdy_trim_cells on each edge."""
    comp_nj, comp_ni, trim = domain.e_sn - 1, domain.e_we - 1, 2 * domain.bdy_trim_cells
    if domain.bdy_trim_cells < 1 or comp_nj <= trim or comp_ni <= trim:
        raise ValueError("Published grid dimensions must remain positive after sponge trim")
    return comp_nj - trim, comp_ni - trim


def _domain_center_xy_m(d: DomainProfile) -> tuple[float, float]:
    return latlon_to_lambert_xy_m(
        d.center_lat_deg,
        d.center_lon_deg,
        origin_lat_deg=d.center_lat_deg,
        central_meridian_deg=d.central_meridian_deg,
        std_parallel_1_deg=d.std_parallel_1_deg,
        std_parallel_2_deg=d.std_parallel_2_deg,
        radius_m=d.earth_radius_m,
    )


def computational_grid_origin_xy_m(domain: DomainProfile) -> tuple[float, float]:
    """Return (x0_comp_m, y0_comp_m) of computational mass cell (j=0, i=0) in Lambert meters."""
    cx_m, cy_m = _domain_center_xy_m(domain)
    return (
        cx_m - 0.5 * (domain.e_we - 2) * domain.dx_m,
        cy_m - 0.5 * (domain.e_sn - 2) * domain.dy_m,
    )


def published_grid_origin_xy_m(domain: DomainProfile) -> tuple[float, float]:
    """Return (x0M, y0M) of Published Grid cell (j=0, i=0) relative to projection origin."""
    published_grid_shape(domain)
    x0_c, y0_c = computational_grid_origin_xy_m(domain)
    return x0_c + domain.bdy_trim_cells * domain.dx_m, y0_c + domain.bdy_trim_cells * domain.dy_m


def published_crs_wkt(domain: DomainProfile) -> str:
    """Return OGC WKT spherical Lambert Conformal CRS string for GeoZarr readers."""
    r = domain.earth_radius_m
    p1 = domain.std_parallel_1_deg
    p2 = _effective_second_parallel(p1, domain.std_parallel_2_deg)
    return (
        'PROJCS["WRF_Lambert_Conformal_Spherical",'
        f'GEOGCS["GCS_WRF_Sphere",DATUM["D_Sphere",SPHEROID["Sphere",{r:.1f},0.0]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Lambert_Conformal_Conic_2SP"],'
        f'PARAMETER["Central_Meridian",{domain.central_meridian_deg}],'
        f'PARAMETER["Latitude_Of_Origin",{domain.center_lat_deg}],'
        f'PARAMETER["Standard_Parallel_1",{p1}],'
        f'PARAMETER["Standard_Parallel_2",{p2}],'
        'UNIT["Meter",1.0]]'
    )


def published_lambert_grid_metadata(domain: DomainProfile) -> dict[str, int | float | str | bool]:
    """Build exact windy-av-layer LambertConformalGrid descriptor for Published Grid."""
    nj, ni = published_grid_shape(domain)
    x0_m, y0_m = published_grid_origin_xy_m(domain)
    p1 = domain.std_parallel_1_deg
    return {
        "kind": "lambert-conformal",
        "width": ni,
        "height": nj,
        "earthRadiusM": domain.earth_radius_m,
        "centralMeridianDeg": domain.central_meridian_deg,
        "latitudeOfOriginDeg": domain.center_lat_deg,
        "standardParallel1Deg": p1,
        "standardParallel2Deg": _effective_second_parallel(p1, domain.std_parallel_2_deg),
        "x0M": x0_m,
        "y0M": y0_m,
        "dxM": domain.dx_m,
        "dyM": domain.dy_m,
        "wrapLongitude": False,
    }


def computational_latlon_bounds(
    domain: DomainProfile, *, halo_deg: float = 1.0, snap_deg: float = 0.25
) -> tuple[float, float, float, float]:
    """Return snapped bounds enclosing the full cell footprint and its forcing halo."""
    if not math.isfinite(halo_deg) or not math.isfinite(snap_deg) or halo_deg < 0 or snap_deg <= 0:
        raise ValueError("Forcing halo must be nonnegative and snap spacing must be positive")
    cx_m, cy_m = _domain_center_xy_m(domain)
    # Outer cell faces lie half a cell beyond the first/last mass centers.
    hx, hy = (
        0.5 * max(1, domain.e_we - 1) * domain.dx_m,
        0.5 * max(1, domain.e_sn - 1) * domain.dy_m,
    )
    x_min, x_max, y_min, y_max = cx_m - hx, cx_m + hx, cy_m - hy, cy_m + hy
    _, _, cone_y = _cone_constants(
        domain.std_parallel_1_deg,
        domain.std_parallel_2_deg,
        domain.center_lat_deg,
        domain.earth_radius_m,
    )
    if x_min <= 0.0 <= x_max and y_min <= cone_y <= y_max:
        raise ValueError("A regional Lambert cutout cannot contain the projection pole")
    # Latitude is monotonic from the cone apex; extrema lie at a corner or nearest point.
    points = [(x, y) for x in (x_min, x_max) for y in (y_min, y_max)]
    points.append((min(x_max, max(x_min, 0.0)), min(y_max, max(y_min, cone_y))))
    lats, lons_u = [], []
    for x_m, y_m in points:
        lat, lon = lambert_xy_m_to_latlon(
            x_m,
            y_m,
            origin_lat_deg=domain.center_lat_deg,
            central_meridian_deg=domain.central_meridian_deg,
            std_parallel_1_deg=domain.std_parallel_1_deg,
            std_parallel_2_deg=domain.std_parallel_2_deg,
            radius_m=domain.earth_radius_m,
        )
        lats.append(lat)
        lons_u.append(
            domain.center_lon_deg
            + math.degrees(_wrap_lon_rad(math.radians(lon - domain.center_lon_deg)))
        )
    w = math.floor((min(lons_u) - halo_deg) / snap_deg) * snap_deg
    s = max(-90.0, math.floor((min(lats) - halo_deg) / snap_deg) * snap_deg)
    e = math.ceil((max(lons_u) + halo_deg) / snap_deg) * snap_deg
    n = min(90.0, math.ceil((max(lats) + halo_deg) / snap_deg) * snap_deg)
    if w < -180.0 and e <= 180.0:
        w, e = w + 360.0, e + 360.0
    return (round(w, 4), round(s, 4), round(e, 4), round(n, 4))


def _avg_axis(arr: npt.NDArray[np.float32], axis: int) -> array[float]:
    sl0, sl1 = [slice(None)] * 3, [slice(None)] * 3
    sl0[axis], sl1[axis] = slice(None, -1), slice(1, None)
    mid = np.add(arr[tuple(sl0)], arr[tuple(sl1)], dtype=np.float64)
    mid *= 0.5
    return _to_f32_array(mid.astype(np.float32))


def _crop_destagger(
    buf: array[float], dims: tuple[int, int, int], trim: int, k_exp: int, axis: int
) -> PubMass3D:
    nz, ny, nx = dims
    ny_m, nx_m, kz = ny - int(axis == 1), nx - int(axis == 2), k_exp + int(axis == 0)
    if k_exp < 1 or nz < kz or trim < 1 or ny_m <= 2 * trim or nx_m <= 2 * trim:
        raise ValueError("Invalid sponge trim or exported_k for staggered 3D volume")
    sub = np.frombuffer(buf, np.float32).reshape(nz, ny, nx)[:kz, trim:-trim, trim:-trim]
    return PubMass3D(Slab3DF32(k_exp, ny_m - 2 * trim, nx_m - 2 * trim, _avg_axis(sub, axis)))


def crop_destagger_x_3d(stag_u: CompStagX3D, trim: int, exported_k: int) -> PubMass3D:
    """Crop lateral sponge and upper levels first, then average X-staggered U faces."""
    return _crop_destagger(stag_u.buf, (stag_u.nz, stag_u.ny, stag_u.nx), trim, exported_k, 2)


def crop_destagger_y_3d(stag_v: CompStagY3D, trim: int, exported_k: int) -> PubMass3D:
    """Crop lateral sponge and upper levels first, then average Y-staggered V faces."""
    return _crop_destagger(stag_v.buf, (stag_v.nz, stag_v.ny, stag_v.nx), trim, exported_k, 1)


def crop_destagger_z_3d(stag_z: CompStagZ3D, trim: int, exported_k: int) -> PubMass3D:
    """Crop lateral sponge and upper levels first, then average Z-staggered faces."""
    return _crop_destagger(stag_z.buf, (stag_z.nz, stag_z.ny, stag_z.nx), trim, exported_k, 0)


def crop_sponge_2d(slab: CompMass2D, trim: int) -> PubMass2D:
    """Crop lateral relaxation cells on all four edges of a contiguous 2D float32 mass slab."""
    sub = _crop_sponge_2d_view(slab, trim)
    ny, nx = sub.shape
    return PubMass2D(Slab2DF32(ny, nx, _to_f32_array(sub)))


def _crop_sponge_2d_view(slab: CompMass2D, trim: int) -> npt.NDArray[np.float32]:
    if trim < 1 or slab.ny <= 2 * trim or slab.nx <= 2 * trim:
        raise ValueError("Invalid sponge trim for 2D computational slab")
    arr = np.frombuffer(slab.buf, np.float32).reshape(slab.ny, slab.nx)
    return arr[trim:-trim, trim:-trim]


def crop_bucket_2d(slab: CompBucket2D, trim: int) -> PubBucket2D:
    """Crop lateral relaxation cells on all four edges of a 2D int32 bucket counter slab."""
    if trim < 1 or slab.ny <= 2 * trim or slab.nx <= 2 * trim:
        raise ValueError("Invalid sponge trim for 2D computational int32 bucket slab")
    arr = np.frombuffer(slab.buf, np.int32).reshape(slab.ny, slab.nx)
    out = array("i")
    out.frombytes(memoryview(np.ascontiguousarray(arr[trim:-trim, trim:-trim])).cast("B"))
    return PubBucket2D(Slab2DI32(slab.ny - 2 * trim, slab.nx - 2 * trim, out))
