"""Complete Zarr v3 store hierarchy, sharding_indexed array metadata, and shard paths.

References:
- Zarr v3 core & ZEP-0002 sharding: https://zarr.dev/zeps/accepted/ZEP0002.html
- GeoZarr CRS conventions: https://github.com/zarr-developers/geozarr-spec
"""

from __future__ import annotations

import math

from av_wrf.contracts import (
    MOISTURE_SOURCE_OF_TRUTH,
    TEMPERATURE_DEFINITION,
    WRF_ZARR_V3_FIELD_NAMES,
    WRF_ZARR_V3_FIELDS,
    ZARR_FORMAT_VERSION,
    ZARR_SCHEMA_STATUS,
    ZARR_SCHEMA_VERSION,
    DomainProfile,
    OutputProfile,
    PubMass2D,
    PubMass3D,
    ZarrFieldSpec,
    zarr_index_codecs,
    zarr_inner_codecs,
)
from av_wrf.core import (
    published_crs_wkt,
    published_grid_shape,
    published_lambert_grid_metadata,
)


def _divisor_chunk(full_len: int, target: int) -> int:
    """Pick largest chunk size <= min(full_len, target) that evenly divides full_len."""
    cap = min(full_len, max(1, target))
    for candidate in range(cap, 0, -1):
        if full_len % candidate == 0:
            return candidate
    return 1


def zarr_shard_relpath(
    field_name: str,
    output: OutputProfile,
    *,
    time_index: int | None = None,
    y_shard: int = 0,
    x_shard: int = 0,
) -> str:
    """Return canonical Zarr v3 c/... shard object key using OutputProfile sharding."""
    if field_name not in WRF_ZARR_V3_FIELD_NAMES:
        raise ValueError(f"Undeclared wrf-zarr-v3 field: {field_name}")
    if y_shard < 0 or x_shard < 0:
        raise ValueError("Spatial shard indices must be non-negative")
    spec = next(f for f in WRF_ZARR_V3_FIELDS if f.name == field_name)
    if spec.dims == ("y", "x"):
        return f"{field_name}/c/{y_shard}/{x_shard}"
    if time_index is None or time_index < 0:
        raise ValueError(f"Time-dependent field {field_name} requires time_index >= 0")
    t_shard = time_index // output.shard_time_steps
    if len(spec.dims) == 3:
        return f"{field_name}/c/{t_shard}/{y_shard}/{x_shard}"
    return f"{field_name}/c/{t_shard}/0/{y_shard}/{x_shard}"


def _coord_array_meta(
    name: str,
    dim_name: str,
    length: int,
    dtype: str,
    fill_value: int | float,
    units: str,
) -> dict[str, object]:
    return {
        "zarr_format": ZARR_FORMAT_VERSION,
        "node_type": "array",
        "shape": [length],
        "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [length]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": fill_value,
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "dimension_names": [dim_name],
        "attributes": {"variable": name, "units": units},
    }


def _field_array_meta(
    spec: ZarrFieldSpec,
    t_len: int,
    k_len: int,
    nj: int,
    ni: int,
    output: OutputProfile,
) -> dict[str, object]:
    iy = _divisor_chunk(nj, output.inner_chunk_yx[0])
    ix = _divisor_chunk(ni, output.inner_chunk_yx[1])
    t_shard = min(t_len, output.shard_time_steps)
    if spec.dims == ("y", "x"):
        shape = [nj, ni]
        outer = [nj, ni]
        inner = [iy, ix]
    elif len(spec.dims) == 3:
        shape = [t_len, nj, ni]
        outer = [t_shard, nj, ni]
        inner = [1, iy, ix]
    else:
        shape = [t_len, k_len, nj, ni]
        outer = [t_shard, k_len, nj, ni]
        inner = [1, k_len, iy, ix]
    return {
        "zarr_format": ZARR_FORMAT_VERSION,
        "node_type": "array",
        "shape": shape,
        "data_type": "float32",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": outer}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": "NaN",
        "dimension_names": list(spec.dims),
        "codecs": [
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": inner,
                    "codecs": zarr_inner_codecs(output.zstd_level),
                    "index_codecs": zarr_index_codecs(),
                    "index_location": "end",
                },
            }
        ],
        "attributes": {
            "units": spec.units,
            "consumer_target": spec.target,
            "time_support": spec.time_support,
            "mantissa_keepbits": spec.keepbits,
        },
    }


def build_zarr_v3_store_metadata(
    domain: DomainProfile,
    output: OutputProfile,
    lead_hours: tuple[int, ...],
) -> dict[str, dict[str, object]]:
    """Build complete Zarr v3 metadata map (root group, 7 coords, and 22 sharded arrays)."""
    nj, ni = published_grid_shape(domain)
    t_len = len(lead_hours)
    k_len = domain.exported_levels_k
    if t_len < 1 or math.isnan(domain.dx_m):
        raise ValueError("lead_hours must be non-empty")

    docs: dict[str, dict[str, object]] = {
        "zarr.json": {
            "zarr_format": ZARR_FORMAT_VERSION,
            "node_type": "group",
            "attributes": {
                "schema_id": output.schema_id,
                "schema_version": ZARR_SCHEMA_VERSION,
                "status": ZARR_SCHEMA_STATUS,
                "vertical_coordinate": "model-levels",
                "moisture_source_of_truth": MOISTURE_SOURCE_OF_TRUTH,
                "temperature_definition": TEMPERATURE_DEFINITION,
                "lead_hours": list(lead_hours),
                "crs_wkt": published_crs_wkt(domain),
                "grid": published_lambert_grid_metadata(domain),
            },
        },
        "time/zarr.json": _coord_array_meta("time", "time", t_len, "int32", -1, "hours"),
        "valid_time_unix_s/zarr.json": _coord_array_meta(
            "valid_time_unix_s", "time", t_len, "int64", -1, "s"
        ),
        "window_start_unix_s/zarr.json": _coord_array_meta(
            "window_start_unix_s", "time", t_len, "int64", -1, "s"
        ),
        "window_end_unix_s/zarr.json": _coord_array_meta(
            "window_end_unix_s", "time", t_len, "int64", -1, "s"
        ),
        "level/zarr.json": _coord_array_meta("level", "level", k_len, "int32", -1, "index"),
        "y/zarr.json": _coord_array_meta("y", "y", nj, "float64", 0.0, "m"),
        "x/zarr.json": _coord_array_meta("x", "x", ni, "float64", 0.0, "m"),
    }
    for spec in WRF_ZARR_V3_FIELDS:
        docs[f"{spec.name}/zarr.json"] = _field_array_meta(
            spec, t_len, k_len, nj, ni, output
        )
    return docs


def verify_published_2d_shape(slab: PubMass2D, domain: DomainProfile) -> None:
    """Verify 2D array matches Published Grid (nj, ni)."""
    nj, ni = published_grid_shape(domain)
    if (slab.ny, slab.nx) != (nj, ni):
        raise ValueError(f"2D slab shape ({slab.ny}, {slab.nx}) != ({nj}, {ni})")


def verify_published_3d_shape(volume: PubMass3D, domain: DomainProfile) -> None:
    """Verify 3D array matches Published Grid (exported_levels_k, nj, ni)."""
    nj, ni = published_grid_shape(domain)
    if (volume.nz, volume.ny, volume.nx) != (domain.exported_levels_k, nj, ni):
        raise ValueError("3D volume shape does not match Published Grid (K, nj, ni)")
