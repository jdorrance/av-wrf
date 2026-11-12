"""NetCDF boundary tests for masked fields, geometry and endpoint gusts."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

import netCDF4
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.codecs import (  # noqa: E402
    check_geogrid_rotation,
    read_wrfout_baseline_nc,
    read_wrfout_step_nc,
    select_cycle_wrfout_slices,
    synthetic_thm_from_dry_theta,
    validate_wrfinput_nc,
)
from av_wrf.contracts import DomainProfile, ScienceProfile  # noqa: E402
from av_wrf.core import (  # noqa: E402
    auto_eta_faces,
    build_accum_baseline,
    computational_grid_origin_xy_m,
    estimate_auto_levels_agl_m,
    lambert_xy_m_to_latlon,
    post_frame,
)
from av_wrf.runner import load_bundle  # noqa: E402


def _populate_3d_nc_vars(
    ds: netCDF4.Dataset, domain: DomainProfile, ny: int, nx: int, nz: int
) -> None:
    mids = estimate_auto_levels_agl_m(domain, terrain_m=1200.0, etac=0.2)
    faces_agl = [0.0]
    for m in mids:
        faces_agl.append(faces_agl[-1] + 2.0 * (m - faces_agl[-1]))
    pb = np.empty((1, nz, ny, nx), dtype=np.float32)
    phb = np.empty((1, nz + 1, ny, nx), dtype=np.float32)
    for k in range(nz):
        pb[0, k] = np.float32(87_800.0 * (0.97**k))
    for kw in range(nz + 1):
        phb[0, kw] = np.float32((1200.0 + faces_agl[kw]) * 9.81)
    thm_v = synthetic_thm_from_dry_theta(8.0, 0.005)
    for nm, val in (("P", 0.0), ("T", 8.0), ("QVAPOR", 0.005), ("THM", thm_v), ("CLDFRA", 0.1)):
        ds.createVariable(nm, "f4", ("Time", "bottom_top", "south_north", "west_east"))[:] = val
    ds.createVariable("PB", "f4", ("Time", "bottom_top", "south_north", "west_east"))[:] = pb
    ds.createVariable("PH", "f4", ("Time", "bottom_top_stag", "south_north", "west_east"))[:] = 0.0
    ds.createVariable("PHB", "f4", ("Time", "bottom_top_stag", "south_north", "west_east"))[:] = phb
    ds.createVariable("W", "f4", ("Time", "bottom_top_stag", "south_north", "west_east"))[:] = 0.1
    ds.createVariable("U", "f4", ("Time", "bottom_top", "south_north", "west_east_stag"))[:] = 5.0
    ds.createVariable("V", "f4", ("Time", "bottom_top", "south_north_stag", "west_east"))[:] = 2.0


def _write_real_wrfout(
    nc_path: Path,
    domain: DomainProfile,
    stamp_wrf: str,
    *,
    achfx: float = 0.0,
    mask_t2: bool = False,
    mask_bucket: bool = False,
    omit_cape_mu: bool = False,
    omit_znw: bool = False,
    shift_lat_deg: float = 0.0,
) -> None:
    ny, nx, nz = domain.e_sn - 1, domain.e_we - 1, domain.e_vert - 1
    x0_c, y0_c = computational_grid_origin_xy_m(domain)
    xlat = np.zeros((1, ny, nx), dtype=np.float32)
    xlong = np.zeros((1, ny, nx), dtype=np.float32)
    for j in range(ny):
        for i in range(nx):
            lat, lon = lambert_xy_m_to_latlon(
                x0_c + i * domain.dx_m,
                y0_c + j * domain.dy_m,
                origin_lat_deg=domain.center_lat_deg,
                central_meridian_deg=domain.central_meridian_deg,
                std_parallel_1_deg=domain.std_parallel_1_deg,
                std_parallel_2_deg=domain.std_parallel_2_deg,
                radius_m=domain.earth_radius_m,
            )
            xlat[0, j, i], xlong[0, j, i] = np.float32(lat + shift_lat_deg), np.float32(lon)
    with netCDF4.Dataset(str(nc_path), mode="w") as ds:
        ds.MAP_PROJ, ds.DX, ds.DY = 1, domain.dx_m, domain.dy_m
        ds.TRUELAT1, ds.TRUELAT2 = domain.std_parallel_1_deg, domain.std_parallel_2_deg
        ds.STAND_LON = domain.central_meridian_deg
        ds.CEN_LAT, ds.CEN_LON = domain.center_lat_deg, domain.center_lon_deg
        ds.HYBRID_OPT, ds.ETAC = 2, 0.2
        for dn, dl in (
            ("Time", 1),
            ("DateStrLen", 19),
            ("south_north", ny),
            ("west_east", nx),
            ("south_north_stag", ny + 1),
            ("west_east_stag", nx + 1),
            ("bottom_top", nz),
            ("bottom_top_stag", nz + 1),
        ):
            ds.createDimension(dn, dl)
        ds.createVariable("Times", "S1", ("Time", "DateStrLen"))[0, :] = np.frombuffer(
            stamp_wrf.encode("ascii"), dtype="S1"
        )
        if not omit_znw:
            ds.createVariable("ZNW", "f4", ("Time", "bottom_top_stag"))[0, :] = auto_eta_faces(
                domain
            )
        ds.createVariable("XLAT", "f4", ("Time", "south_north", "west_east"))[:] = xlat
        ds.createVariable("XLONG", "f4", ("Time", "south_north", "west_east"))[:] = xlong
        vals_2d = {
            "HGT": 1200.0,
            "COSALPHA": 1.0,
            "SINALPHA": 0.0,
            "PSFC": 88_000.0,
            "T2": 295.0,
            "Q2": 0.007,
            "U10": 4.0,
            "V10": 1.5,
            "HFX": -20.0,
            "LH": 5.0,
            "UST": 0.4,
            "PBLH": 1800.0,
            "ACHFX": achfx,
            "ACLHF": achfx * 0.25,
            "RAINC": 0.0,
            "RAINNC": 1.2,
            "AFWA_CAPE": 90.0,
        }
        if not omit_cape_mu:
            vals_2d["AFWA_CAPE_MU"] = 340.0
        for nm, val in vals_2d.items():
            fv = np.float32(9.969209968386869e36) if (nm == "T2" and mask_t2) else None
            var = ds.createVariable(nm, "f4", ("Time", "south_north", "west_east"), fill_value=fv)
            slab = np.full((1, ny, nx), val, dtype=np.float32)
            if nm == "T2" and mask_t2:
                slab[0, ny // 2, nx // 2] = np.float32(9.969209968386869e36)
            var[:] = slab
        for inm in ("I_RAINC", "I_RAINNC"):
            ifv = np.int32(-9999) if (inm == "I_RAINNC" and mask_bucket) else None
            ivar = ds.createVariable(
                inm, "i4", ("Time", "south_north", "west_east"), fill_value=ifv
            )
            islab = np.zeros((1, ny, nx), dtype=np.int32)
            if inm == "I_RAINNC" and mask_bucket:
                islab[0, 1, 1] = -9999
            ivar[:] = islab
        _populate_3d_nc_vars(ds, domain, ny, nx, nz)


def _small_domain_and_science() -> tuple[DomainProfile, ScienceProfile]:
    prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
    b = load_bundle(ROOT / "config", prod_id)
    return dataclasses.replace(b.domain, e_we=21, e_sn=21, bdy_trim_cells=6), b.science


class TestRealNetcdfBoundary(unittest.TestCase):
    def test_post_rejects_negative_cape_and_boundary_layer_height(self) -> None:
        dom, sci = _small_domain_and_science()
        with tempfile.TemporaryDirectory() as tmp:
            origin, lead = (Path(tmp) / f"f{h}.nc" for h in (0, 1))
            _write_real_wrfout(origin, dom, "2026-11-12_00:00:00")
            _write_real_wrfout(lead, dom, "2026-11-12_01:00:00")
            base = build_accum_baseline(read_wrfout_baseline_nc(origin, dom), dom.bdy_trim_cells)
            raw = read_wrfout_step_nc(lead, dom, sci)
            idx = dom.bdy_trim_cells * dom.e_we
            for field, slab in (("AFWA_CAPE_MU", raw.afwa_cape_jkg), ("PBLH", raw.pblh_m)):
                valid = slab.buf[idx]
                slab.buf[idx] = -1.0
                with self.assertRaisesRegex(ValueError, f"Negative value.*{field}"):
                    post_frame(base, raw, dom, sci)
                slab.buf[idx] = valid

    def test_southern_lambert_rotation_sign_across_antimeridian(self) -> None:
        dom, sci = _small_domain_and_science()
        dom = dataclasses.replace(
            dom,
            center_lat_deg=-dom.center_lat_deg,
            center_lon_deg=179.9,
            central_meridian_deg=179.9,
            std_parallel_1_deg=-dom.std_parallel_1_deg,
            std_parallel_2_deg=-dom.std_parallel_2_deg,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "southern.nc"
            _write_real_wrfout(path, dom, "2026-11-12_01:00:00")
            with netCDF4.Dataset(str(path), mode="a") as ds:
                lon = np.asarray(ds.variables["XLONG"][:], np.float64)
                offset = (lon - dom.central_meridian_deg + 180.0) % 360.0 - 180.0
                theta = np.deg2rad(0.64 * offset)
                expected = np.sin(theta).astype(np.float32)
                self.assertLess(lon[0, 0, -1], 0.0)
                self.assertGreater(expected[0, 0, -1], 0.0)
                ds.variables["COSALPHA"][:] = np.cos(theta)
                ds.variables["SINALPHA"][:] = -expected
            legacy = read_wrfout_step_nc(path, dom, sci)
            np.testing.assert_array_equal(np.asarray(legacy.sin_alpha.buf), expected.ravel())
            with netCDF4.Dataset(str(path), mode="a") as ds:
                ds.variables["SINALPHA"][:] = expected
            signed = read_wrfout_step_nc(path, dom, sci)
            self.assertEqual(legacy.sin_alpha, signed.sin_alpha)

            with netCDF4.Dataset(str(path), mode="a") as ds:
                ds.variables["SINALPHA"][:] = -expected
                for suffix, y, x, pad in (
                    ("_U", "south_north", "west_east_stag", ((0, 0), (0, 0), (0, 1))),
                    ("_V", "south_north_stag", "west_east", ((0, 0), (0, 1), (0, 0))),
                ):
                    dims = ("Time", y, x)
                    ds.createVariable(f"XLONG{suffix}", "f4", dims)[:] = np.pad(
                        lon, pad, mode="edge"
                    )
                    ds.createVariable(f"SINALPHA{suffix}", "f4", dims)[:] = np.pad(
                        -expected, pad, mode="edge"
                    )
                longitude = lon[0].astype(np.float32)
                with self.assertRaisesRegex(ValueError, "incorrect southern Lambert SINALPHA"):
                    check_geogrid_rotation(ds, path, dom, longitude)
                check_geogrid_rotation(ds, path, dom, longitude, repair=True)
            with netCDF4.Dataset(str(path)) as ds:
                check_geogrid_rotation(ds, path, dom, longitude)

    def test_low_cloud_uses_native_levels_above_exported_column(self) -> None:
        dom, sci = _small_domain_and_science()
        dom = dataclasses.replace(dom, exported_levels_k=3)
        with tempfile.TemporaryDirectory() as tmp:
            origin, lead = (Path(tmp) / f"f{h}.nc" for h in (0, 1))
            _write_real_wrfout(origin, dom, "2026-11-12_00:00:00")
            _write_real_wrfout(lead, dom, "2026-11-12_01:00:00")
            with netCDF4.Dataset(str(lead), mode="a") as ds:
                cld = ds.variables["CLDFRA"]
                cld[:] = 0.0
                cld[0, 3, :, :] = 0.5
                cld[0, 10, :, :] = 0.5
            base = build_accum_baseline(read_wrfout_baseline_nc(origin, dom), dom.bdy_trim_cells)
            frame, _ = post_frame(base, read_wrfout_step_nc(lead, dom, sci), dom, sci)
            self.assertEqual(frame.fields_2d["cloud_total_pct"].at(0, 0), 75.0)
            self.assertEqual(frame.fields_2d["cloud_low_pct"].at(0, 0), 50.0)

    def test_netcdf_boundary_rejects_masks_missing_cape_mu_znw_and_grid_shift(self) -> None:
        dom, sci = _small_domain_and_science()
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _write_real_wrfout(td / "mask_t2.nc", dom, "2026-11-12_01:00:00", mask_t2=True)
            with self.assertRaisesRegex(ValueError, "T2.*masked"):
                read_wrfout_step_nc(td / "mask_t2.nc", dom, sci)
            _write_real_wrfout(td / "mask_bkt.nc", dom, "2026-11-12_01:00:00", mask_bucket=True)
            with self.assertRaisesRegex(ValueError, "I_RAINNC.*masked"):
                read_wrfout_step_nc(td / "mask_bkt.nc", dom, sci)
            _write_real_wrfout(td / "nocape.nc", dom, "2026-11-12_01:00:00", omit_cape_mu=True)
            with self.assertRaisesRegex(ValueError, "AFWA_CAPE_MU"):
                read_wrfout_step_nc(td / "nocape.nc", dom, sci)
            _write_real_wrfout(td / "wrfinput_d01", dom, "2026-11-12_00:00:00", omit_znw=True)
            with self.assertRaisesRegex(ValueError, "ZNW"):
                validate_wrfinput_nc(td / "wrfinput_d01", dom, sci)
            _write_real_wrfout(td / "shift.nc", dom, "2026-11-12_01:00:00", shift_lat_deg=0.0135)
            with self.assertRaisesRegex(ValueError, "XLAT/XLONG"):
                read_wrfout_step_nc(td / "shift.nc", dom, sci)

    def test_sparse_tail_uses_full_accumulation_and_endpoint_gust(self) -> None:
        dom, sci = _small_domain_and_science()
        with tempfile.TemporaryDirectory() as tmp:
            wdir = Path(tmp) / "wrfout"
            wdir.mkdir()
            _write_real_wrfout(wdir / "wrfout_d01_00", dom, "2026-11-12_00:00:00", achfx=0.0)
            _write_real_wrfout(
                wdir / "wrfout_d01_03", dom, "2026-11-12_03:00:00", achfx=1_496_000.0
            )
            (f0_p, f0_i), ((f3_p, f3_i),) = select_cycle_wrfout_slices(
                wdir, "2026-11-12T00:00:00Z", ("2026-11-12T03:00:00Z",)
            )
            validate_wrfinput_nc(f0_p, dom, sci)
            base0 = build_accum_baseline(
                read_wrfout_baseline_nc(f0_p, dom, time_index=f0_i, science=sci), dom.bdy_trim_cells
            )
            step3 = read_wrfout_step_nc(f3_p, dom, sci, time_index=f3_i)
            frame3, _ = post_frame(base0, step3, dom, sci)
            self.assertAlmostEqual(
                frame3.fields_2d["sensible_heat_flux_wm2"].at(0, 0),
                1_496_000.0 / 10_800.0,
                delta=0.005,
            )
            self.assertEqual(frame3.window_end_unix_s - frame3.window_start_unix_s, 10_800)
            self.assertAlmostEqual(
                frame3.fields_2d["gust_10m_mps"].at(0, 0),
                float(np.hypot(4.0, 1.5)) + 7.71 * 0.4,
                delta=0.001,
            )
            with netCDF4.Dataset(str(f3_p), mode="a") as ds:
                mslp = ds.createVariable("AFWA_MSLP", "f4", ("Time", "south_north", "west_east"))
                mslp[:] = 100_500.0
                trim = dom.bdy_trim_cells
                mslp[f3_i, trim, trim] = 0.0
            mixed, _ = post_frame(
                base0, read_wrfout_step_nc(f3_p, dom, sci, time_index=f3_i), dom, sci
            )
            self.assertEqual(
                mixed.fields_2d["mslp_pa"].at(0, 0), frame3.fields_2d["mslp_pa"].at(0, 0)
            )
            self.assertEqual(mixed.fields_2d["mslp_pa"].at(0, 1), 100_500.0)


if __name__ == "__main__":
    unittest.main()
