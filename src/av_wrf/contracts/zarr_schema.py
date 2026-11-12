"""Canonical 22-field wrf-zarr-v3 specification, coordinates, and admission bounds.

Assumptions & Provenance Declarations (required by windy-av-layer methods.ts):
- `MOISTURE_SOURCE_OF_TRUTH = "spfh_kgkg"`: model-level RH is omitted because consumer
  `native-fields.ts` derives dewpoint directly from `(spfhKgKg, pressureHpa)`.
- `LOW_CLOUD_DEFINITION`: terrain-following sigma `p >= 0.8 * PSFC` (ECMWF LCC convention) so
  high-elevation plateaus (`PSFC < 800 hPa`) do not spuriously report 0% low cloud.
"""

from __future__ import annotations

from dataclasses import dataclass

ZARR_SCHEMA_ID = "wrf-zarr-v3"
ZARR_SCHEMA_VERSION = 1
ZARR_SCHEMA_STATUS = "provisional-phase-1"
ZARR_FORMAT_VERSION = 3
MOISTURE_SOURCE_OF_TRUTH = "spfh_kgkg"
TEMPERATURE_DEFINITION = "sensible-air-temperature-k"
# Locked provisional catalog label. The Zarr group records the native WRF parcel method.
CAPE_PARCEL_DEFINITION = "afwa-mucape-lowest-300hpa-virtual-temp-undiluted"
LOW_CLOUD_DEFINITION = "ecmwf-sigma-ge-0p8-max-random-overlap"
WRF_SPHERE_RADIUS_M = 6_370_000.0
MAX_EXPORTED_LEVELS = 40
CONSUMER_MAX_PROFILE_GAP_M = 1_000.0
BL_CEILING_AGL_M = 2_000.0
DEFAULT_SHARD_TIME_STEP = 1
DEFAULT_INNER_SPATIAL_CHUNK: tuple[int, int] = (64, 64)
DEFAULT_ZSTD_LEVEL = 3


def zarr_inner_codecs(zstd_level: int = DEFAULT_ZSTD_LEVEL) -> list[dict[str, object]]:
    """Return Zarr v3 inner-chunk codec pipeline (bytes little-endian + zstd)."""
    return [
        {"name": "bytes", "configuration": {"endian": "little"}},
        {"name": "zstd", "configuration": {"level": zstd_level}},
    ]


def zarr_index_codecs() -> list[dict[str, object]]:
    """Return ZEP-0002 sharding_indexed index codec pipeline (bytes + crc32c)."""
    return [
        {"name": "bytes", "configuration": {"endian": "little"}},
        {"name": "crc32c"},
    ]


@dataclass(frozen=True)
class ZarrCoordSpec:
    name: str
    dim: str
    data_type: str
    pack_code: str
    fill_value: int | float
    units: str


@dataclass(frozen=True)
class ZarrFieldSpec:
    name: str
    dims: tuple[str, ...]
    units: str
    target: str
    time_support: str
    keepbits: int


WRF_ZARR_V3_COORDS: tuple[ZarrCoordSpec, ...] = (
    ZarrCoordSpec("time", "time", "int32", "i", -1, "hours"),
    ZarrCoordSpec("valid_time_unix_s", "time", "int64", "q", -1, "s"),
    ZarrCoordSpec("window_start_unix_s", "time", "int64", "q", -1, "s"),
    ZarrCoordSpec("window_end_unix_s", "time", "int64", "q", -1, "s"),
    ZarrCoordSpec("level", "level", "int32", "i", -1, "index"),
    ZarrCoordSpec("y", "y", "float64", "d", 0.0, "m"),
    ZarrCoordSpec("x", "x", "float64", "d", 0.0, "m"),
)

_T2D = ("time", "y", "x")
_T3D = ("time", "level", "y", "x")

WRF_ZARR_V3_FIELDS: tuple[ZarrFieldSpec, ...] = (
    ZarrFieldSpec("terrain_m", ("y", "x"), "m", "terrainM", "static", 17),
    ZarrFieldSpec("surface_pressure_pa", _T2D, "Pa", "surfacePressurePa", "instant", 17),
    ZarrFieldSpec("temp_2m_k", _T2D, "K", "temp2mK", "instant", 15),
    ZarrFieldSpec("spfh_2m_kgkg", _T2D, "kg/kg", "specificHumidity2mKgKg", "instant", 15),
    ZarrFieldSpec("sensible_heat_flux_wm2", _T2D, "W/m2", "sensibleHeatFluxWm2", "window-mean", 15),
    ZarrFieldSpec("latent_heat_flux_wm2", _T2D, "W/m2", "latentHeatFluxWm2", "window-mean", 15),
    ZarrFieldSpec("precip_mm_hr", _T2D, "mm/h", "precipitationMmPerHour", "window-rate", 15),
    ZarrFieldSpec("mslp_pa", _T2D, "Pa", "auxSurface.mslpPa", "instant", 17),
    ZarrFieldSpec("u_10m_earth_mps", _T2D, "m/s", "auxSurface.u10mMps", "instant", 15),
    ZarrFieldSpec("v_10m_earth_mps", _T2D, "m/s", "auxSurface.v10mMps", "instant", 15),
    ZarrFieldSpec(
        "gust_10m_mps", _T2D, "m/s", "auxSurface.gust10mMps", "instant-parameterized", 15
    ),
    ZarrFieldSpec("pbl_height_m", _T2D, "m", "auxSurface.pblHeightM", "instant", 15),
    ZarrFieldSpec("cape_jkg", _T2D, "J/kg", "auxSurface.capeJkg", "instant", 15),
    ZarrFieldSpec("cloud_total_pct", _T2D, "%", "auxSurface.cloudTotalPct", "instant", 15),
    ZarrFieldSpec("cloud_low_pct", _T2D, "%", "auxSurface.cloudLowPct", "instant", 15),
    ZarrFieldSpec("pressure_pa", _T3D, "Pa", "pressurePa", "instant", 17),
    ZarrFieldSpec("height_msl_m", _T3D, "m", "hgtM", "instant", 17),
    ZarrFieldSpec("temp_k", _T3D, "K", "tempK", "instant", 15),
    ZarrFieldSpec("spfh_kgkg", _T3D, "kg/kg", "auxLevels.spfhKgKg", "instant", 15),
    ZarrFieldSpec("u_earth_mps", _T3D, "m/s", "auxLevels.uWindMps", "instant", 15),
    ZarrFieldSpec("v_earth_mps", _T3D, "m/s", "auxLevels.vWindMps", "instant", 15),
    ZarrFieldSpec("omega_pas", _T3D, "Pa/s", "auxLevels.omegaPaS", "instant", 15),
)

WRF_ZARR_V3_FIELD_NAMES: frozenset[str] = frozenset(f.name for f in WRF_ZARR_V3_FIELDS)
WRF_ZARR_V3_2D_NAMES: tuple[str, ...] = tuple(f.name for f in WRF_ZARR_V3_FIELDS if len(f.dims) < 4)
WRF_ZARR_V3_3D_NAMES: tuple[str, ...] = tuple(
    f.name for f in WRF_ZARR_V3_FIELDS if len(f.dims) == 4
)

__all__ = [
    "BL_CEILING_AGL_M",
    "CAPE_PARCEL_DEFINITION",
    "CONSUMER_MAX_PROFILE_GAP_M",
    "DEFAULT_INNER_SPATIAL_CHUNK",
    "DEFAULT_SHARD_TIME_STEP",
    "DEFAULT_ZSTD_LEVEL",
    "LOW_CLOUD_DEFINITION",
    "MAX_EXPORTED_LEVELS",
    "MOISTURE_SOURCE_OF_TRUTH",
    "TEMPERATURE_DEFINITION",
    "WRF_SPHERE_RADIUS_M",
    "WRF_ZARR_V3_2D_NAMES",
    "WRF_ZARR_V3_3D_NAMES",
    "WRF_ZARR_V3_COORDS",
    "WRF_ZARR_V3_FIELDS",
    "WRF_ZARR_V3_FIELD_NAMES",
    "ZARR_FORMAT_VERSION",
    "ZARR_SCHEMA_ID",
    "ZARR_SCHEMA_STATUS",
    "ZARR_SCHEMA_VERSION",
    "ZarrCoordSpec",
    "ZarrFieldSpec",
    "zarr_index_codecs",
    "zarr_inner_codecs",
]
