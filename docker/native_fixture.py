"""Synthetic, spatially uniform inputs for native WPS qualification only."""

import math
from pathlib import Path

import numpy as np

from av_wrf.codecs import WpsIntermediateField, write_wps_intermediate_field
from av_wrf.contracts import DomainProfile
from av_wrf.runner.forcing_ifs import REQUIRED, UNITS


def geography(root: Path) -> None:
    """Write tiny global geogrid tiles and a purpose-built static-field table."""
    fields = (
        ("HGT_M", 100, 1, 0),
        ("LANDUSEF", 7, 1, 21),
        ("SOILCTOP", 4, 1, 16),
        ("SOILCBOT", 4, 1, 16),
        ("SOILTEMP", 280, 1, 0),
        ("ALBEDO12M", 20, 12, 0),
        ("GREENFRAC", 50, 12, 0),
        ("LAI12M", 2, 12, 0),
        ("SNOALB", 50, 1, 0),
        ("LAKE_DEPTH", 50, 1, 0),
    )
    lines = []
    for name, value, nz, categories in fields:
        path = root / name
        path.mkdir(parents=True)
        kind = "categorical" if categories else "continuous"
        index = (
            f"type={kind}\nprojection=regular_ll\ndx=1.0\ndy=1.0\n"
            "known_x=1.0\nknown_y=1.0\nknown_lat=-89.5\nknown_lon=-179.5\n"
            f"wordsize=2\ntile_x=360\ntile_y=180\ntile_z={nz}\n"
            "tile_bdr=0\nrow_order=bottom_top\nendian=big\n"
            'units="fixture"\ndescription="uniform fixture"\n'
        )
        if categories:
            index += f"category_min=1\ncategory_max={categories}\n"
        if name == "LANDUSEF":
            index += (
                'mminlu="MODIFIED_IGBP_MODIS_NOAH"\niswater=17\nislake=21\nisice=15\nisurban=13\n'
            )
        elif name == "GREENFRAC":
            index += "scale_factor=0.01\n"
        (path / "index").write_text(index, encoding="ascii")
        (path / "00001-00360.00001-00180").write_bytes(
            np.full((nz, 180, 360), value, dtype=">i2").tobytes()
        )
        lines.extend(("===============================", f"name={name}", "priority=1"))
        lines.extend((f"dest_type={kind}", "interp_option=default:nearest_neighbor"))
        lines.append(f"rel_path=default:{name}/")
        if categories:
            dimension = "land_cat" if name == "LANDUSEF" else "soil_cat"
            dominant = {"LANDUSEF": "LU_INDEX", "SOILCTOP": "SCT_DOM", "SOILCBOT": "SCB_DOM"}
            lines.extend((f"z_dim_name={dimension}", f"dominant={dominant[name]}"))
            if name == "LANDUSEF":
                lines.append("landmask_water=default:17,21")
        elif nz == 12:
            lines.append("z_dim_name=month")
        if name in ("LAKE_DEPTH", "LAI12M"):
            lines.append(f"flag_in_output=FLAG_{name}")
    (root / "GEOGRID.TBL").write_text("\n".join(lines) + "\n", encoding="ascii")


def forcing(work: Path, domain: DomainProfile, dates: tuple[str, ...]) -> None:
    """Encode a hydrostatic, low-wind atmosphere with four soil layers."""
    values = {
        "PSFC": 100000.0,
        "PMSL": 101200.0,
        "SKINTEMP": 288.0,
        "LANDSEA": 1.0,
        "SNOW": 0.0,
        "SNOWH": 0.0,
        "SOILHGT": 100.0,
    }
    for date in dates:
        with (work / f"FILE:{date[:13]}").open("wb") as stream:
            for name, level in sorted(REQUIRED):
                if name == "TT":
                    value = (
                        288.0 if level == 200100 else max(210.0, 288.0 * (level / 100000.0) ** 0.19)
                    )
                elif name == "GHT":
                    value = 8000.0 * math.log(100000.0 / level)
                elif name in ("UU", "VV"):
                    value = 2.0 if name == "UU" else 0.0
                elif name == "SPECHUMD":
                    value = 0.006 if level == 200100 else 0.006 * (level / 100000.0) ** 2
                elif name.startswith("ST"):
                    value = 285.0
                elif name.startswith("SM"):
                    value = 0.25
                else:
                    value = values[name]
                field = WpsIntermediateField(
                    date,
                    0.0,
                    "fixture",
                    name,
                    UNITS[name],
                    "synthetic fixture",
                    level,
                    domain.center_lat_deg - 10,
                    domain.center_lon_deg - 10,
                    1.0,
                    1.0,
                    domain.earth_radius_m / 1000,
                    False,
                )
                write_wps_intermediate_field(stream, field, np.full((21, 21), value))
