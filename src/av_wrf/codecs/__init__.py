"""Public L2 codecs surface: NetCDF4 WRF readers and Zarr v3 store/shard builders."""

from __future__ import annotations

from av_wrf.codecs.synthetic import (
    build_synthetic_wrf_baseline,
    build_synthetic_wrf_step,
    synthetic_thm_from_dry_theta,
)
from av_wrf.codecs.wrf_nc import (
    RawWrfBaselineInput,
    RawWrfStepInput,
    read_wrfout_baseline_nc,
    read_wrfout_step_nc,
    select_cycle_wrfout_slices,
    select_cycle_wrfout_windows,
    verify_local_wrf_nc,
    verify_wrfout_grid_against_domain,
)
from av_wrf.codecs.zarr_shard import (
    MAX_SPATIAL_TILES_PER_FRAME,
    crc32c_u32,
    decode_shard_f32,
    decode_shard_f32_array,
    divisor_chunk,
    encode_2d_shard_bytes,
    encode_3d_shard_bytes,
    encode_coord_chunks,
    resolve_inner_chunk_yx,
    verify_shard_index,
)
from av_wrf.codecs.zarr_v3 import (
    build_zarr_v3_store_metadata,
    expected_shard_keys,
    zarr_shard_relpath,
)

__all__ = [
    "MAX_SPATIAL_TILES_PER_FRAME",
    "RawWrfBaselineInput",
    "RawWrfStepInput",
    "build_synthetic_wrf_baseline",
    "build_synthetic_wrf_step",
    "build_zarr_v3_store_metadata",
    "crc32c_u32",
    "decode_shard_f32",
    "decode_shard_f32_array",
    "divisor_chunk",
    "encode_2d_shard_bytes",
    "encode_3d_shard_bytes",
    "encode_coord_chunks",
    "expected_shard_keys",
    "read_wrfout_baseline_nc",
    "read_wrfout_step_nc",
    "resolve_inner_chunk_yx",
    "select_cycle_wrfout_slices",
    "select_cycle_wrfout_windows",
    "synthetic_thm_from_dry_theta",
    "verify_local_wrf_nc",
    "verify_shard_index",
    "verify_wrfout_grid_against_domain",
    "zarr_shard_relpath",
]
