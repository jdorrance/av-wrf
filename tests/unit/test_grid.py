"""Unit tests for av_wrf.core.grid loading real YAML and optional pyproj oracle."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import (  # noqa: E402
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    Slab2DF32,
    Slab3DF32,
    parse_domain_profile,
)
from av_wrf.core import (  # noqa: E402
    crop_sponge_2d,
    crop_sponge_3d,
    destagger_x_3d,
    destagger_y_3d,
    destagger_z_3d,
    lambert_xy_m_to_latlon,
    latlon_to_lambert_xy_m,
    published_crs_wkt,
    published_grid_shape,
    published_lambert_grid_metadata,
)
from av_wrf.runner import parse_declarative_yaml  # noqa: E402
from tests.oracle.reference_cases import (  # noqa: E402
    ARMENIA_CENTER,
    NZ_SOUTH_ISLAND_CENTER,
)


def load_armenia_domain() -> DomainProfile:
    raw = parse_declarative_yaml(
        ROOT / "config" / "profiles" / "domains" / "armenia-3km-v1.yaml"
    )
    return parse_domain_profile(raw)


class TestGrid(unittest.TestCase):
    def test_nh_and_sh_lambert_roundtrip_and_optional_pyproj_oracle(self) -> None:
        r_m = load_armenia_domain().earth_radius_m
        for lat0, lon0, sp1, sp2 in (ARMENIA_CENTER, NZ_SOUTH_ISLAND_CENTER):
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
            self.assertGreater(x_m, 0.0)
            self.assertGreater(y_m, 0.0)
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
                from pyproj import CRS, Transformer  # noqa: PLC0415
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

    def test_published_lambert_metadata_and_wkt_off_meridian(self) -> None:
        dom = dataclasses.replace(
            load_armenia_domain(), center_lon_deg=45.75, central_meridian_deg=44.50
        )
        meta = published_lambert_grid_metadata(dom)
        self.assertEqual(meta["kind"], "lambert-conformal")
        self.assertEqual((meta["height"], meta["width"]), (148, 180))
        self.assertIn("Lambert_Conformal_Conic_2SP", published_crs_wkt(dom))

        cx_m = float(meta["x0M"]) + 0.5 * (int(meta["width"]) - 1) * float(meta["dxM"])
        cy_m = float(meta["y0M"]) + 0.5 * (int(meta["height"]) - 1) * float(meta["dyM"])
        clat, clon = lambert_xy_m_to_latlon(
            cx_m,
            cy_m,
            origin_lat_deg=dom.center_lat_deg,
            central_meridian_deg=dom.central_meridian_deg,
            std_parallel_1_deg=dom.std_parallel_1_deg,
            std_parallel_2_deg=dom.std_parallel_2_deg,
            radius_m=dom.earth_radius_m,
        )
        self.assertAlmostEqual(clat, dom.center_lat_deg, places=9)
        self.assertAlmostEqual(clon, dom.center_lon_deg, places=9)

    def test_destagger_3d_and_sponge_crop_float32_slabs(self) -> None:
        dom = load_armenia_domain()
        self.assertEqual(published_grid_shape(dom), (148, 180))
        slab = CompMass2D(
            Slab2DF32.from_values(5, 6, [float(r * 10 + c) for r in range(5) for c in range(6)])
        )
        cropped = crop_sponge_2d(slab, trim=1)
        self.assertEqual((cropped.ny, cropped.nx), (3, 4))
        self.assertAlmostEqual(cropped.at(0, 0), 11.0, places=5)

        u_stag = CompStagX3D(Slab3DF32.from_values(2, 3, 4, [float(i) for i in range(24)]))
        v_stag = CompStagY3D(Slab3DF32.from_values(2, 4, 3, [float(i) for i in range(24)]))
        w_stag = CompStagZ3D(Slab3DF32.from_values(3, 3, 3, [float(i) for i in range(27)]))
        self.assertEqual((destagger_x_3d(u_stag).nz, destagger_x_3d(u_stag).nx), (2, 3))
        self.assertEqual((destagger_y_3d(v_stag).ny, destagger_y_3d(v_stag).nx), (3, 3))
        w_mass = destagger_z_3d(w_stag)
        self.assertEqual((w_mass.nz, w_mass.ny, w_mass.nx), (2, 3, 3))
        vol_cropped = crop_sponge_3d(w_mass, trim=1, exported_k=1)
        self.assertEqual((vol_cropped.nz, vol_cropped.ny, vol_cropped.nx), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
