"""Unit tests for av_wrf.core.grid: offset central meridian bbox, dateline, and crop-destagger."""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import (  # noqa: E402
    CompBucket2D,
    CompMass2D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    Slab2DF32,
    Slab2DI32,
    Slab3DF32,
    parse_domain_profile,
)
from av_wrf.core import (  # noqa: E402
    computational_latlon_bounds,
    crop_bucket_2d,
    crop_destagger_x_3d,
    crop_destagger_y_3d,
    crop_destagger_z_3d,
    crop_sponge_2d,
    lambert_grid_to_earth_trig,
    lambert_xy_m_to_latlon,
    latlon_to_lambert_xy_m,
    published_crs_wkt,
    published_grid_shape,
    published_lambert_grid_metadata,
    validate_domain_profile,
)
from av_wrf.core.thermo import rotate_grid_to_earth_vec  # noqa: E402
from av_wrf.runner import parse_declarative_yaml  # noqa: E402
from tests.oracle.reference_cases import NH_MIDLAT_LCC, SH_MIDLAT_LCC  # noqa: E402


def _load_configured_domains() -> tuple[DomainProfile, ...]:
    paths = sorted((ROOT / "config" / "profiles" / "domains").glob("*.yaml"))
    return tuple(parse_domain_profile(parse_declarative_yaml(p)) for p in paths)


class TestGrid(unittest.TestCase):
    def test_lambert_rejects_unsupported_spacing_and_radius(self) -> None:
        d = _load_configured_domains()[0]
        with self.assertRaisesRegex(ValueError, "grid spacing must be equal"):
            validate_domain_profile(dataclasses.replace(d, dy_m=d.dx_m * 2.0))
        with self.assertRaisesRegex(ValueError, "Earth radius"):
            validate_domain_profile(dataclasses.replace(d, earth_radius_m=d.earth_radius_m + 1.0))

    def test_lambert_rejects_invalid_meridian_and_degenerate_cone(self) -> None:
        d = _load_configured_domains()[0]
        with self.assertRaisesRegex(ValueError, "longitudes"):
            validate_domain_profile(dataclasses.replace(d, central_meridian_deg=181.0))
        with self.assertRaisesRegex(ValueError, "share a hemisphere"):
            validate_domain_profile(
                dataclasses.replace(d, std_parallel_2_deg=-d.std_parallel_1_deg)
            )
        with self.assertRaisesRegex(ValueError, "zero cone"):
            latlon_to_lambert_xy_m(
                d.center_lat_deg,
                d.center_lon_deg,
                origin_lat_deg=d.center_lat_deg,
                central_meridian_deg=d.central_meridian_deg,
                std_parallel_1_deg=35.0,
                std_parallel_2_deg=-35.0,
                radius_m=d.earth_radius_m,
            )

    def test_wps_tangent_cutoff_matches_published_geometry_and_rotation(self) -> None:
        for sign in (1.0, -1.0):
            tangent = dataclasses.replace(
                _load_configured_domains()[0],
                center_lat_deg=40.0 * sign,
                center_lon_deg=40.0,
                central_meridian_deg=0.0,
                std_parallel_1_deg=40.0 * sign,
                std_parallel_2_deg=40.0 * sign,
            )
            near = dataclasses.replace(tangent, std_parallel_2_deg=40.1 * sign)
            secant = dataclasses.replace(tangent, std_parallel_2_deg=40.11 * sign)
            meta = published_lambert_grid_metadata(tangent)
            self.assertEqual(published_lambert_grid_metadata(near), meta)
            self.assertNotEqual(published_lambert_grid_metadata(secant)["x0M"], meta["x0M"])
            self.assertEqual(published_crs_wkt(near), published_crs_wkt(tangent))
            self.assertEqual(
                computational_latlon_bounds(near), computational_latlon_bounds(tangent)
            )
            cosine, sine = lambert_grid_to_earth_trig(near, np.array([40.0], dtype=np.float32))
            angle = math.sin(math.radians(40.0 * sign)) * math.radians(-40.0)
            east, north = rotate_grid_to_earth_vec(np.ones(1), np.zeros(1), cosine, sine)
            self.assertAlmostEqual(float(east[0]), math.cos(angle), places=12)
            self.assertAlmostEqual(float(north[0]), math.sin(angle), places=12)

    def test_nh_and_sh_lambert_roundtrip_and_optional_pyproj_oracle(self) -> None:
        r_m = _load_configured_domains()[0].earth_radius_m
        for lat0, lon0, sp1, sp2 in (NH_MIDLAT_LCC, SH_MIDLAT_LCC):
            x0, y0 = latlon_to_lambert_xy_m(
                lat0,
                lon0,
                origin_lat_deg=lat0,
                central_meridian_deg=lon0,
                std_parallel_1_deg=sp1,
                std_parallel_2_deg=sp2,
                radius_m=r_m,
            )
            self.assertAlmostEqual(x0, 0.0, places=7)
            self.assertAlmostEqual(y0, 0.0, places=7)

            t_lat, t_lon = lat0 + 1.35, lon0 + 1.85
            x_m, y_m = latlon_to_lambert_xy_m(
                t_lat,
                t_lon,
                origin_lat_deg=lat0,
                central_meridian_deg=lon0,
                std_parallel_1_deg=sp1,
                std_parallel_2_deg=sp2,
                radius_m=r_m,
            )
            rec_lat, rec_lon = lambert_xy_m_to_latlon(
                x_m,
                y_m,
                origin_lat_deg=lat0,
                central_meridian_deg=lon0,
                std_parallel_1_deg=sp1,
                std_parallel_2_deg=sp2,
                radius_m=r_m,
            )
            self.assertAlmostEqual(rec_lat, t_lat, places=9)
            self.assertAlmostEqual(rec_lon, t_lon, places=9)

            try:
                from pyproj import CRS, Transformer
            except ImportError:
                continue
            proj_str = (
                f"+proj=lcc +lat_1={sp1} +lat_2={sp2} +lat_0={lat0} "
                f"+lon_0={lon0} +R={r_m} +units=m +no_defs"
            )
            fwd = Transformer.from_crs(
                CRS.from_epsg(4326), CRS.from_proj4(proj_str), always_xy=True
            )
            px, py = fwd.transform(t_lon, t_lat)
            self.assertLess(abs(x_m - px), 0.05)
            self.assertLess(abs(y_m - py), 0.05)

    def test_published_lambert_metadata_offset_meridian_and_antimeridian_bounds(self) -> None:
        for base_dom in _load_configured_domains():
            validate_domain_profile(base_dom)
            dom = dataclasses.replace(base_dom, center_lon_deg=base_dom.central_meridian_deg + 4.0)
            w, s, e, n = computational_latlon_bounds(dom)
            self.assertLess(w, dom.center_lon_deg)
            self.assertGreater(e, dom.center_lon_deg)
            self.assertLess(s, dom.center_lat_deg)
            self.assertGreater(n, dom.center_lat_deg)

            # Antimeridian-crossing domain (centered at 179E) yields bounded span < 20 deg
            anti_dom = dataclasses.replace(
                base_dom, center_lon_deg=179.0, central_meridian_deg=179.0
            )
            aw, _, ae, _ = computational_latlon_bounds(anti_dom)
            self.assertLess(aw, 180.0)
            self.assertGreater(ae, 180.0)
            self.assertTrue(0.0 < (ae - aw) < 20.0)

            meta = published_lambert_grid_metadata(dom)
            nj, ni = published_grid_shape(dom)
            self.assertEqual((meta["height"], meta["width"]), (nj, ni))
            self.assertIn("Lambert_Conformal_Conic_2SP", published_crs_wkt(dom))

            cx_m = float(meta["x0M"]) + 0.5 * (int(meta["width"]) - 1) * float(meta["dxM"])
            cy_m = float(meta["y0M"]) + 0.5 * (int(meta["height"]) - 1) * float(meta["dyM"])
            clat, clon = lambert_xy_m_to_latlon(
                cx_m,
                cy_m,
                origin_lat_deg=dom.center_lat_deg,
                central_meridian_deg=dom.central_meridian_deg,
                std_parallel_1_deg=dom.std_parallel_1_deg,
                radius_m=dom.earth_radius_m,
                std_parallel_2_deg=dom.std_parallel_2_deg,
            )
            self.assertAlmostEqual(clat, dom.center_lat_deg, places=9)
            self.assertAlmostEqual(clon, dom.center_lon_deg, places=9)

        with self.assertRaises(ValueError):
            validate_domain_profile(dataclasses.replace(_load_configured_domains()[0], e_we=100))

    def test_crop_and_destagger_use_faces_adjacent_to_exported_mass_cells(self) -> None:
        slab = CompMass2D(
            Slab2DF32.from_values(5, 6, [float(r * 10 + c) for r in range(5) for c in range(6)])
        )
        cropped = crop_sponge_2d(slab, trim=1)
        self.assertEqual((cropped.ny, cropped.nx), (3, 4))
        bkt = CompBucket2D(
            Slab2DI32.from_values(5, 6, [r * 10 + c for r in range(5) for c in range(6)])
        )
        self.assertEqual(crop_bucket_2d(bkt, trim=1).at(0, 0), 11)

        u_stag = CompStagX3D(Slab3DF32.from_values(3, 4, 6, [float(i) for i in range(72)]))
        v_stag = CompStagY3D(Slab3DF32.from_values(3, 5, 5, [float(i) for i in range(75)]))
        w_stag = CompStagZ3D(Slab3DF32.from_values(4, 4, 5, [float(i) for i in range(80)]))
        self.assertEqual(
            list(crop_destagger_x_3d(u_stag, 1, 2).buf),
            [7.5, 8.5, 9.5, 13.5, 14.5, 15.5, 31.5, 32.5, 33.5, 37.5, 38.5, 39.5],
        )
        self.assertEqual(
            list(crop_destagger_y_3d(v_stag, 1, 2).buf),
            [8.5, 9.5, 10.5, 13.5, 14.5, 15.5, 33.5, 34.5, 35.5, 38.5, 39.5, 40.5],
        )
        self.assertEqual(
            list(crop_destagger_z_3d(w_stag, 1, 2).buf),
            [16.0, 17.0, 18.0, 21.0, 22.0, 23.0, 36.0, 37.0, 38.0, 41.0, 42.0, 43.0],
        )


if __name__ == "__main__":
    unittest.main()
