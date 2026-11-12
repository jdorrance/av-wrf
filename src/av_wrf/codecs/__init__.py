"""Public L2 codecs surface: NetCDF4 WRF readers and Zarr v3 store/shard builders."""

from __future__ import annotations

from av_wrf.codecs.icon_grib import IconGribCutout, verify_icon_object
from av_wrf.codecs.synthetic import (
    build_synthetic_wrf_baseline,
    build_synthetic_wrf_step,
    synthetic_thm_from_dry_theta,
)
from av_wrf.codecs.wps_intermediate import WpsIntermediateField, write_wps_intermediate_field
from av_wrf.codecs.wps_intermediate_read import WpsIntermediateSlab, read_wps_intermediate_fields
from av_wrf.codecs.wrf_nc import (
    RawWrfBaselineInput,
    RawWrfStepInput,
    read_wrfout_baseline_nc,
    read_wrfout_step_nc,
    verify_local_wrf_nc,
    verify_wrfout_grid_against_domain,
)
from av_wrf.codecs.wrf_nc_grid import (
    check_geogrid_rotation,
    domain_tag,
    select_cycle_wrfout_slices,
    validate_wrfinput_nc,
    verify_grid_against_domain,
    verify_wps_fields,
)
from av_wrf.codecs.zarr_shard import (
    MAX_SPATIAL_TILES_PER_FRAME,
    crc32c_u32,
    decode_shard_f32_array,
    encode_2d_shard_bytes,
    encode_3d_shard_bytes,
    encode_coord_chunks,
    outer_shard_yx,
    resolve_inner_chunk_yx,
)
from av_wrf.codecs.zarr_v3 import (
    build_zarr_v3_store_metadata,
    expected_shard_keys,
    zarr_shard_relpath,
)

__all__ = [
    "MAX_SPATIAL_TILES_PER_FRAME",
    "IconGribCutout",
    "RawWrfBaselineInput",
    "RawWrfStepInput",
    "WpsIntermediateField",
    "WpsIntermediateSlab",
    "build_synthetic_wrf_baseline",
    "build_synthetic_wrf_step",
    "build_zarr_v3_store_metadata",
    "check_geogrid_rotation",
    "crc32c_u32",
    "decode_shard_f32_array",
    "domain_tag",
    "encode_2d_shard_bytes",
    "encode_3d_shard_bytes",
    "encode_coord_chunks",
    "expected_shard_keys",
    "outer_shard_yx",
    "read_wps_intermediate_fields",
    "read_wrfout_baseline_nc",
    "read_wrfout_step_nc",
    "resolve_inner_chunk_yx",
    "select_cycle_wrfout_slices",
    "synthetic_thm_from_dry_theta",
    "validate_wrfinput_nc",
    "verify_grid_against_domain",
    "verify_icon_object",
    "verify_local_wrf_nc",
    "verify_wps_fields",
    "verify_wrfout_grid_against_domain",
    "write_wps_intermediate_field",
    "zarr_shard_relpath",
]
