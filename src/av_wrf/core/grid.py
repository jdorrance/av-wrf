"""Pure float64 spherical Lambert Conformal projection, destaggering, and sponge cropping.

Assumptions & Grid Geometry Alignment:
1. Spherical datum: WPS/WRF uses a sphere of radius `R = 6,370,000 m` (`reradius` in
   `module_model_constants.F`), distinct from GFS/RRFS GRIB2 shape 6 (`6,371,229 m`). Both
   `published_crs_wkt` and `LambertConformalGrid.earthRadiusM` bind `6,370,000 m` explicitly.
2. Arakawa-C destaggering: `wrfout` `U`, `V`, `W`, and `PH+PHB` are un-coupled physical fields.
   Horizontal `0.5 * (f[i] + f[i+1])` is 2nd-order in uniform projected spacing `dx, dy`, and
   vertical `0.5 * (f[k] + f[k+1])` is exact in `eta` because WRF defines mass levels as the
   arithmetic midpoint of full `eta_w` faces (Skamarock et al. 2019 NCAR/TN-556+STR Sec. 3.2.1).
3. Map scale `m(lat) = |n * rho| / (R * cos(lat))` matches `windy-av-layer` `geometry.ts` and
   determines the minimum physical ground spacing `dx / m_max` for RK3 CFL checks (Sec. 3.3.4).

References:
- WPS spherical Lambert:
  https://github.com/wrf-model/WPS/blob/master/geogrid/src/module_map_utils.F
- WRF v4 Technical Note (Sec. 2.3 & 3.2):
  https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf
- Consumer LambertConformalGrid: windy-av-layer/packages/contracts/src/geometry.ts
"""

from __future__ import annotations

import math
from array import array

from av_wrf.contracts import (
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    PubMass2D,
    PubMass3D,
    Slab2DF32,
    Slab3DF32,
)


def _wrap_lon_rad(delta_rad: float) -> float:
    return (delta_rad + math.pi) % (2.0 * math.pi) - math.pi


def _cone_constants(
    std_lat1_deg: float,
    std_lat2_deg: float,
    origin_lat_deg: float,
    radius_m: float,
) -> tuple[float, float, float]:
    phi1 = math.radians(std_lat1_deg)
    phi2 = math.radians(std_lat2_deg)
    phi0 = math.radians(origin_lat_deg)
    t1 = math.tan(0.25 * math.pi + 0.5 * phi1)
    t2 = math.tan(0.25 * math.pi + 0.5 * phi2)
    t0 = math.tan(0.25 * math.pi + 0.5 * phi0)
    if abs(phi1 - phi2) < 1e-12:
        n = math.sin(phi1)
    else:
        n = math.log(math.cos(phi1) / math.cos(phi2)) / math.log(t2 / t1)
    f_const = (math.cos(phi1) * (t1**n)) / n
    rho0 = radius_m * f_const / (t0**n)
    return n, f_const, rho0


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
    dlon = _wrap_lon_rad(math.radians(lon_deg - central_meridian_deg))
    rho = radius_m * f_const / (math.tan(0.25 * math.pi + 0.5 * phi) ** n)
    theta = n * dlon
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
    sign_n = 1.0 if n >= 0.0 else -1.0
    dy = rho0 - y_m
    rho = sign_n * math.hypot(x_m, dy)
    theta = math.atan2(sign_n * x_m, sign_n * dy)
    lat_rad = 2.0 * math.atan((radius_m * f_const / rho) ** (1.0 / n)) - 0.5 * math.pi
    lon_rad = _wrap_lon_rad(math.radians(central_meridian_deg) + theta / n)
    return math.degrees(lat_rad), math.degrees(lon_rad)


def lambert_convergence_trig(
    lon_deg: float,
    *,
    central_meridian_deg: float,
    std_parallel_1_deg: float,
    std_parallel_2_deg: float,
    origin_lat_deg: float,
    radius_m: float,
) -> tuple[float, float]:
    """Return (cos(theta), sin(theta)) for theta = n*(lon - stand_lon)."""
    n, _, _ = _cone_constants(
        std_parallel_1_deg, std_parallel_2_deg, origin_lat_deg, radius_m
    )
    theta = n * _wrap_lon_rad(math.radians(lon_deg - central_meridian_deg))
    return math.cos(theta), math.sin(theta)


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
    rho = radius_m * f_const / (math.tan(0.25 * math.pi + 0.5 * phi) ** n)
    return abs(n * rho) / (radius_m * math.cos(phi))


def published_grid_shape(domain: DomainProfile) -> tuple[int, int]:
    """Return (nj, ni) mass-point dimensions after cropping bdy_trim_cells on each edge."""
    comp_nj = domain.e_sn - 1
    comp_ni = domain.e_we - 1
    trim = 2 * domain.bdy_trim_cells
    if domain.bdy_trim_cells < 1 or comp_nj <= trim or comp_ni <= trim:
        raise ValueError("Published grid dimensions must remain positive after sponge trim")
    return comp_nj - trim, comp_ni - trim


def published_grid_origin_xy_m(domain: DomainProfile) -> tuple[float, float]:
    """Return (x0M, y0M) of Published Grid cell (j=0, i=0) relative to projection origin."""
    published_grid_shape(domain)
    comp_nj = domain.e_sn - 1
    comp_ni = domain.e_we - 1
    cx_m, cy_m = latlon_to_lambert_xy_m(
        domain.center_lat_deg,
        domain.center_lon_deg,
        origin_lat_deg=domain.center_lat_deg,
        central_meridian_deg=domain.central_meridian_deg,
        std_parallel_1_deg=domain.std_parallel_1_deg,
        std_parallel_2_deg=domain.std_parallel_2_deg,
        radius_m=domain.earth_radius_m,
    )
    x0_comp = cx_m - 0.5 * (comp_ni - 1) * domain.dx_m
    y0_comp = cy_m - 0.5 * (comp_nj - 1) * domain.dy_m
    return (
        x0_comp + domain.bdy_trim_cells * domain.dx_m,
        y0_comp + domain.bdy_trim_cells * domain.dy_m,
    )


def published_crs_wkt(domain: DomainProfile) -> str:
    """Return OGC WKT spherical Lambert Conformal CRS string for GeoZarr readers."""
    r = domain.earth_radius_m
    return (
        'PROJCS["WRF_Lambert_Conformal_Spherical",'
        f'GEOGCS["GCS_WRF_Sphere",DATUM["D_Sphere",SPHEROID["Sphere",{r:.1f},0.0]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Lambert_Conformal_Conic_2SP"],'
        f'PARAMETER["Central_Meridian",{domain.central_meridian_deg}],'
        f'PARAMETER["Latitude_Of_Origin",{domain.center_lat_deg}],'
        f'PARAMETER["Standard_Parallel_1",{domain.std_parallel_1_deg}],'
        f'PARAMETER["Standard_Parallel_2",{domain.std_parallel_2_deg}],'
        'UNIT["Meter",1.0]]'
    )


def published_lambert_grid_metadata(
    domain: DomainProfile,
) -> dict[str, int | float | str | bool]:
    """Build exact windy-av-layer LambertConformalGrid descriptor for Published Grid."""
    nj, ni = published_grid_shape(domain)
    x0_m, y0_m = published_grid_origin_xy_m(domain)
    return {
        "kind": "lambert-conformal",
        "width": ni,
        "height": nj,
        "earthRadiusM": domain.earth_radius_m,
        "centralMeridianDeg": domain.central_meridian_deg,
        "latitudeOfOriginDeg": domain.center_lat_deg,
        "standardParallel1Deg": domain.std_parallel_1_deg,
        "standardParallel2Deg": domain.std_parallel_2_deg,
        "x0M": x0_m,
        "y0M": y0_m,
        "dxM": domain.dx_m,
        "dyM": domain.dy_m,
        "wrapLongitude": False,
    }


def destagger_x_3d(stag_u: CompStagX3D) -> CompMass3D:
    """Destagger (nz, ny, nx+1) Arakawa-C U faces onto (nz, ny, nx) mass points."""
    nz, ny, nx1 = stag_u.nz, stag_u.ny, stag_u.nx
    if nx1 < 2:
        raise ValueError("Staggered X axis requires >= 2 columns")
    nx = nx1 - 1
    out = array("f")
    src = stag_u.buf
    for k in range(nz):
        for j in range(ny):
            row_off = (k * ny + j) * nx1
            for i in range(nx):
                out.append(0.5 * (src[row_off + i] + src[row_off + i + 1]))
    return CompMass3D(Slab3DF32(nz=nz, ny=ny, nx=nx, buf=out))


def destagger_y_3d(stag_v: CompStagY3D) -> CompMass3D:
    """Destagger (nz, ny+1, nx) Arakawa-C V faces onto (nz, ny, nx) mass points."""
    nz, ny1, nx = stag_v.nz, stag_v.ny, stag_v.nx
    if ny1 < 2:
        raise ValueError("Staggered Y axis requires >= 2 rows")
    ny = ny1 - 1
    out = array("f")
    src = stag_v.buf
    for k in range(nz):
        plane_off = k * ny1 * nx
        for j in range(ny):
            r0 = plane_off + j * nx
            r1 = r0 + nx
            for i in range(nx):
                out.append(0.5 * (src[r0 + i] + src[r1 + i]))
    return CompMass3D(Slab3DF32(nz=nz, ny=ny, nx=nx, buf=out))


def destagger_z_3d(stag_z: CompStagZ3D) -> CompMass3D:
    """Destagger (nz+1, ny, nx) Arakawa-C vertical faces onto (nz, ny, nx) mass levels."""
    nz1, ny, nx = stag_z.nz, stag_z.ny, stag_z.nx
    if nz1 < 2:
        raise ValueError("Staggered Z axis requires >= 2 levels")
    nz = nz1 - 1
    plane = ny * nx
    out = array("f")
    src = stag_z.buf
    for k in range(nz):
        p0 = k * plane
        p1 = p0 + plane
        for idx in range(plane):
            out.append(0.5 * (src[p0 + idx] + src[p1 + idx]))
    return CompMass3D(Slab3DF32(nz=nz, ny=ny, nx=nx, buf=out))


def crop_sponge_2d(slab: CompMass2D, trim: int) -> PubMass2D:
    """Crop lateral relaxation cells on all four edges of a contiguous 2D mass slab."""
    if trim < 1 or slab.ny <= 2 * trim or slab.nx <= 2 * trim:
        raise ValueError("Invalid sponge trim for 2D computational slab")
    pub_ny = slab.ny - 2 * trim
    pub_nx = slab.nx - 2 * trim
    out = array("f")
    src = slab.buf
    for j in range(trim, slab.ny - trim):
        start = j * slab.nx + trim
        out.extend(src[start : start + pub_nx])
    return PubMass2D(Slab2DF32(ny=pub_ny, nx=pub_nx, buf=out))


def crop_sponge_3d(volume: CompMass3D, trim: int, exported_k: int) -> PubMass3D:
    """Select lowest exported_k mass levels and crop lateral sponge cells."""
    if exported_k < 1 or volume.nz < exported_k:
        raise ValueError("Volume has fewer vertical levels than exported_k")
    if trim < 1 or volume.ny <= 2 * trim or volume.nx <= 2 * trim:
        raise ValueError("Invalid sponge trim for 3D computational volume")
    pub_ny = volume.ny - 2 * trim
    pub_nx = volume.nx - 2 * trim
    out = array("f")
    src = volume.buf
    for k in range(exported_k):
        plane_off = k * volume.ny * volume.nx
        for j in range(trim, volume.ny - trim):
            start = plane_off + j * volume.nx + trim
            out.extend(src[start : start + pub_nx])
    return PubMass3D(Slab3DF32(nz=exported_k, ny=pub_ny, nx=pub_nx, buf=out))
