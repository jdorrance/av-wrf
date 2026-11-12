"""ZEP-0002 sharding_indexed binary encoder/decoder, tile-cap guard, and coordinate chunks.

Uses exact configured `inner_chunk_yx` (canonical `[64, 64]`) on all grids by padding outer shard
shapes `(ceil(ny / iy) * iy, ceil(nx / ix) * ix)` and filling partial edge pixels with `NaN`,
matching Zarr v3 / ZEP-0002 `zarr.open_group` semantics across composite and prime grid sizes.

References:
- ZEP-0002 sharding binary layout: https://zarr.dev/zeps/accepted/ZEP0002.html
- Zarr v3 codecs (`bytes`, `zstd`, `crc32c`): https://zarr.readthedocs.io/en/stable/api/zarr/codecs/
"""

from __future__ import annotations

import struct

import numpy as np
import numpy.typing as npt
from numcodecs import zstd as _zstd

from av_wrf.contracts import (
    WRF_ZARR_V3_COORDS,
    DomainProfile,
    OutputProfile,
    PubMass2D,
    PubMass3D,
    validate_zarr_output,
)
from av_wrf.core import published_grid_origin_xy_m, published_grid_shape

EMPTY_CHUNK_SENTINEL = 0xFFFFFFFFFFFFFFFF
# 256 tiles = 16x16 chunks of 64x64 = 1024x1024 published cells per step shard. The cap keeps
# a single shard's Zstd tile buffer bounded while covering Alps-/Western-US-scale regions.
MAX_SPATIAL_TILES_PER_FRAME = 256


def _make_crc32c_table() -> tuple[int, ...]:
    table: list[int] = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if (crc & 1) else (crc >> 1)
        table.append(crc)
    return tuple(table)


_CRC32C_TABLE: tuple[int, ...] = _make_crc32c_table()


def crc32c_u32(payload: bytes) -> int:
    """Compute Castagnoli CRC-32C unsigned 32-bit digest required by ZEP-0002."""
    crc = 0xFFFFFFFF
    for byte in payload:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def resolve_inner_chunk_yx(ny: int, nx: int, output: OutputProfile) -> tuple[int, int]:
    """Return exact `(iy, ix)` inner chunk dimensions and enforce `MAX_SPATIAL_TILES_PER_FRAME`."""
    validate_zarr_output(output)
    if ny < 1 or nx < 1:
        raise ValueError(f"Published grid dimensions must be >= 1, got ({ny}, {nx})")
    iy, ix = output.inner_chunk_yx
    tiles_y = (ny + iy - 1) // iy
    tiles_x = (nx + ix - 1) // ix
    spatial_tiles = tiles_y * tiles_x
    if spatial_tiles > MAX_SPATIAL_TILES_PER_FRAME:
        raise ValueError(
            f"Published grid ({ny}x{nx}) with {iy}x{ix} inner chunks requires "
            f"{spatial_tiles} spatial tiles (> {MAX_SPATIAL_TILES_PER_FRAME} cap)"
        )
    return iy, ix


def outer_shard_yx(ny: int, nx: int, output: OutputProfile) -> tuple[int, int]:
    """Return ZEP-0002 outer shard `(outer_ny, outer_nx)` padded to multiples of `(iy, ix)`."""
    iy, ix = resolve_inner_chunk_yx(ny, nx, output)
    return ((ny + iy - 1) // iy) * iy, ((nx + ix - 1) // ix) * ix


def _pack_volume_shard(
    frame: npt.NDArray[np.float32],
    output: OutputProfile,
) -> bytes:
    nz, ny, nx = frame.shape
    iy, ix = resolve_inner_chunk_yx(ny, nx, output)
    ys, xs = range(0, ny, iy), range(0, nx, ix)
    body = bytearray()
    index = bytearray()
    tile_buf = np.empty((nz, iy, ix), dtype="<f4")
    if not bool(np.all(np.isfinite(frame))):
        raise ValueError("Non-finite float32 values in ZEP-0002 shard")
    for ty in ys:
        vy = min(iy, ny - ty)
        for tx in xs:
            vx = min(ix, nx - tx)
            if vy < iy or vx < ix:
                tile_buf.fill(np.nan)
            tile_buf[:, :vy, :vx] = frame[:, ty : ty + vy, tx : tx + vx]
            comp = _zstd.compress(tile_buf, output.zstd_level)
            index.extend(struct.pack("<QQ", len(body), len(comp)))
            body.extend(comp)
    footer_crc = struct.pack("<I", crc32c_u32(bytes(index)))
    body.extend(index)
    body.extend(footer_crc)
    return bytes(body)


def encode_2d_shard_bytes(
    slab: PubMass2D,
    output: OutputProfile,
) -> bytes:
    """Encode one published-grid 2D slab into a ZEP-0002 shard."""
    frame = np.frombuffer(slab.buf, dtype=np.float32).reshape(1, slab.ny, slab.nx)
    return _pack_volume_shard(frame, output)


def encode_3d_shard_bytes(
    slab: PubMass3D,
    output: OutputProfile,
) -> bytes:
    """Encode one published-grid 3D volume into a ZEP-0002 shard."""
    frame = np.frombuffer(slab.buf, dtype=np.float32).reshape(slab.nz, slab.ny, slab.nx)
    return _pack_volume_shard(frame, output)


def _verify_shard_index(
    shard_bytes: bytes,
    *,
    ny: int,
    nx: int,
    output: OutputProfile,
) -> tuple[bytes, int, int]:
    """Verify CRC-32C and complete contiguous tile bounds for one time step."""
    iy, ix = resolve_inner_chunk_yx(ny, nx, output)
    n_tiles = len(range(0, ny, iy)) * len(range(0, nx, ix))
    idx_len = n_tiles * 16
    if len(shard_bytes) < idx_len + 4:
        raise ValueError("Truncated ZEP-0002 shard payload")
    body_len = len(shard_bytes) - idx_len - 4
    idx_raw = shard_bytes[body_len:-4]
    (expected_crc,) = struct.unpack("<I", shard_bytes[-4:])
    if crc32c_u32(idx_raw) != expected_crc:
        raise ValueError("ZEP-0002 shard index CRC-32C mismatch")

    cursor = 0
    for tile_i in range(n_tiles):
        off, nbytes = struct.unpack_from("<QQ", idx_raw, tile_i * 16)
        if off == EMPTY_CHUNK_SENTINEL or nbytes == EMPTY_CHUNK_SENTINEL or nbytes == 0:
            raise ValueError(f"Unexpected empty inner chunk in populated tile {tile_i}")
        if off != cursor or off + nbytes > body_len:
            raise ValueError(f"Invalid ZEP-0002 offset/length ({off}, {nbytes}) at {tile_i}")
        cursor += nbytes
    if cursor != body_len:
        raise ValueError("ZEP-0002 shard body contains unaccounted trailing bytes")
    return idx_raw, iy, ix


def decode_shard_f32_array(
    shard_bytes: bytes,
    *,
    nz: int,
    ny: int,
    nx: int,
    output: OutputProfile,
) -> npt.NDArray[np.float32]:
    """Verify index + CRC-32C and decompress one time step into `(1, nz, ny, nx)`."""
    idx_raw, iy, ix = _verify_shard_index(shard_bytes, ny=ny, nx=nx, output=output)
    out_arr = np.empty((1, nz, ny, nx), dtype=np.float32)
    tile_bytes = nz * iy * ix * 4
    shard_view = memoryview(shard_bytes)
    sp_idx = 0
    for ty in range(0, ny, iy):
        vy = min(iy, ny - ty)
        for tx in range(0, nx, ix):
            vx = min(ix, nx - tx)
            off, nbytes = struct.unpack_from("<QQ", idx_raw, sp_idx * 16)
            sp_idx += 1
            try:
                raw_tile = _zstd.decompress(shard_view[off : off + nbytes])
            except Exception as exc:
                raise ValueError(f"Corrupt Zstd payload in tile {sp_idx - 1}") from exc
            if len(raw_tile) != tile_bytes:
                raise ValueError("Corrupt Zstd inner chunk byte length")
            tile_vals = np.frombuffer(raw_tile, dtype="<f4").reshape(nz, iy, ix)
            out_arr[0, :, ty : ty + vy, tx : tx + vx] = tile_vals[:, :vy, :vx]
    return out_arr


def encode_coord_chunks(
    domain: DomainProfile,
    lead_hours: tuple[int, ...],
    valid_unix_s: tuple[int, ...],
    window_starts_s: tuple[int, ...],
    window_ends_s: tuple[int, ...],
) -> dict[str, bytes]:
    """Encode all 7 1D little-endian coordinate chunks derived from `WRF_ZARR_V3_COORDS`."""
    nj, ni = published_grid_shape(domain)
    x0_m, y0_m = published_grid_origin_xy_m(domain)
    values_by_name: dict[str, tuple[int | float, ...]] = {
        "time": lead_hours,
        "valid_time_unix_s": valid_unix_s,
        "window_start_unix_s": window_starts_s,
        "window_end_unix_s": window_ends_s,
        "level": tuple(range(domain.exported_levels_k)),
        "y": tuple(y0_m + j * domain.dy_m for j in range(nj)),
        "x": tuple(x0_m + i * domain.dx_m for i in range(ni)),
    }
    return {
        f"{spec.name}/c/0": struct.pack(
            f"<{len(values_by_name[spec.name])}{spec.pack_code}",
            *values_by_name[spec.name],
        )
        for spec in WRF_ZARR_V3_COORDS
    }
