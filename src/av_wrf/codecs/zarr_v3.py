"""Complete Zarr v3 store hierarchy, sharding_indexed array metadata, and shard inventory.

References:
- Zarr v3 core & ZEP-0002 sharding: https://zarr.dev/zeps/accepted/ZEP0002.html
- GeoZarr CRS conventions: https://github.com/zarr-developers/geozarr-spec
"""

from __future__ import annotations

import math

from av_wrf.codecs.zarr_shard import outer_shard_yx, resolve_inner_chunk_yx
from av_wrf.contracts import (
    LOW_CLOUD_DEFINITION,
    MOISTURE_SOURCE_OF_TRUTH,
    TEMPERATURE_DEFINITION,
    WRF_ZARR_V3_COORDS,
    WRF_ZARR_V3_FIELD_NAMES,
    WRF_ZARR_V3_FIELDS,
    ZARR_FORMAT_VERSION,
    ZARR_SCHEMA_ID,
    ZARR_SCHEMA_STATUS,
    ZARR_SCHEMA_VERSION,
    DomainProfile,
    OutputProfile,
    ZarrCoordSpec,
    ZarrFieldSpec,
    zarr_index_codecs,
    zarr_inner_codecs,
)
from av_wrf.core import (
    published_crs_wkt,
    published_grid_shape,
    published_lambert_grid_metadata,
)


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


def expected_shard_keys(output: OutputProfile, n_leads: int) -> set[str]:
    """Return complete canonical set of coord + 22-field shard keys for `n_leads`."""
    expected: set[str] = {f"{c.name}/c/0" for c in WRF_ZARR_V3_COORDS}
    expected.add(zarr_shard_relpath("terrain_m", output))
    step = output.shard_time_steps
    for spec in WRF_ZARR_V3_FIELDS:
        if spec.name == "terrain_m":
            continue
        for t_idx in range(0, n_leads, step):
            expected.add(zarr_shard_relpath(spec.name, output, time_index=t_idx))
    return expected


def _coord_array_meta(spec: ZarrCoordSpec, length: int) -> dict[str, object]:
    return {
        "zarr_format": ZARR_FORMAT_VERSION,
        "node_type": "array",
        "shape": [length],
        "data_type": spec.data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [length]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": spec.fill_value,
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "dimension_names": [spec.dim],
        "attributes": {"variable": spec.name, "units": spec.units},
    }


def _field_array_meta(
    spec: ZarrFieldSpec,
    t_len: int,
    k_len: int,
    nj: int,
    ni: int,
    output: OutputProfile,
) -> dict[str, object]:
    iy, ix = resolve_inner_chunk_yx(nj, ni, output)
    outer_ny, outer_nx = outer_shard_yx(nj, ni, output)
    t_shard = min(t_len, output.shard_time_steps)
    if spec.dims == ("y", "x"):
        shape, outer, inner = [nj, ni], [outer_ny, outer_nx], [iy, ix]
    elif len(spec.dims) == 3:
        shape, outer, inner = [t_len, nj, ni], [t_shard, outer_ny, outer_nx], [1, iy, ix]
    else:
        shape = [t_len, k_len, nj, ni]
        outer = [t_shard, k_len, outer_ny, outer_nx]
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
    if output.schema_id != ZARR_SCHEMA_ID:
        raise ValueError(
            f"OutputProfile.schema_id '{output.schema_id}' != locked '{ZARR_SCHEMA_ID}'"
        )
    nj, ni = published_grid_shape(domain)
    t_len = len(lead_hours)
    k_len = domain.exported_levels_k
    if t_len < 1 or math.isnan(domain.dx_m):
        raise ValueError("lead_hours must be non-empty")

    dim_lengths = {"time": t_len, "level": k_len, "y": nj, "x": ni}
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
                # WRF v4.8 Buoyancy selects max theta-e where pressure exceeds 500 hPa.
                "cape_parcel_definition": "afwa-mucape-max-theta-e-pressure-gt-500hpa",
                "low_cloud_definition": LOW_CLOUD_DEFINITION,
                "lead_hours": list(lead_hours),
                "crs_wkt": published_crs_wkt(domain),
                "grid": published_lambert_grid_metadata(domain),
            },
        }
    }
    for c_spec in WRF_ZARR_V3_COORDS:
        docs[f"{c_spec.name}/zarr.json"] = _coord_array_meta(c_spec, dim_lengths[c_spec.dim])
    for spec in WRF_ZARR_V3_FIELDS:
        docs[f"{spec.name}/zarr.json"] = _field_array_meta(spec, t_len, k_len, nj, ni, output)
    return docs
