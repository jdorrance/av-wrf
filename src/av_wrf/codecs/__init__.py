"""Public L2 codecs surface: NetCDF4 WRF readers and Zarr v3 store/shard builders."""

from __future__ import annotations

from av_wrf.codecs.synthetic import build_synthetic_wrf_step, synthetic_thm_from_dry_theta
from av_wrf.codecs.wrf_nc import (
    RawWrfStepInput,
    read_wrfout_step_nc,
    select_cycle_wrfout_slices,
    verify_local_wrf_nc,
)
from av_wrf.codecs.zarr_shard import (
    crc32c_u32,
    decode_shard_f32,
    divisor_chunk,
    encode_2d_shard_bytes,
    encode_3d_shard_bytes,
    encode_coord_chunks,
    verify_shard_index,
)
from av_wrf.codecs.zarr_v3 import (
    build_zarr_v3_store_metadata,
    expected_shard_keys,
    zarr_shard_relpath,
)

__all__ = [
    "RawWrfStepInput",
    "build_synthetic_wrf_step",
    "build_zarr_v3_store_metadata",
    "crc32c_u32",
    "decode_shard_f32",
    "divisor_chunk",
    "encode_2d_shard_bytes",
    "encode_3d_shard_bytes",
    "encode_coord_chunks",
    "expected_shard_keys",
    "read_wrfout_step_nc",
    "select_cycle_wrfout_slices",
    "synthetic_thm_from_dry_theta",
    "verify_local_wrf_nc",
    "verify_shard_index",
    "zarr_shard_relpath",
]
