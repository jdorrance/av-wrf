"""ZEP-0002 sharding_indexed binary encoder/decoder, tile-cap guard, and coordinate chunks.

References:
- ZEP-0002 sharding binary layout: https://zarr.dev/zeps/accepted/ZEP0002.html
- Zarr v3 codecs (`bytes`, `zstd`, `crc32c`): https://zarr.readthedocs.io/en/stable/api/zarr/codecs/
"""

from __future__ import annotations

import struct
from array import array
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
from numcodecs import zstd as _zstd

from av_wrf.contracts import (
    WRF_ZARR_V3_COORDS,
    DomainProfile,
    OutputProfile,
    PubMass2D,
    PubMass3D,
)
from av_wrf.core import published_grid_origin_xy_m, published_grid_shape

EMPTY_CHUNK_SENTINEL = 0xFFFFFFFFFFFFFFFF
MAX_SPATIAL_TILES_PER_FRAME = 64
MIN_LARGE_GRID_AXIS = 24
MIN_LARGE_GRID_CHUNK = 8


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


def divisor_chunk(full_len: int, target: int) -> int:
    """Pick largest chunk size <= min(full_len, target) that evenly divides full_len."""
    cap = min(full_len, max(1, target))
    for candidate in range(cap, 0, -1):
        if full_len % candidate == 0:
            return candidate
    return 1


def resolve_inner_chunk_yx(ny: int, nx: int, output: OutputProfile) -> tuple[int, int]:
    """Resolve `(iy, ix)` inner chunk dimensions and fail closed on pathological 1x1 tiling."""
    iy = divisor_chunk(ny, output.inner_chunk_yx[0])
    ix = divisor_chunk(nx, output.inner_chunk_yx[1])
    spatial_tiles = (ny // iy) * (nx // ix)
    bad_y = ny >= MIN_LARGE_GRID_AXIS and iy < MIN_LARGE_GRID_CHUNK
    bad_x = nx >= MIN_LARGE_GRID_AXIS and ix < MIN_LARGE_GRID_CHUNK
    if bad_y or bad_x or spatial_tiles > MAX_SPATIAL_TILES_PER_FRAME:
        raise ValueError(
            f"Published grid ({ny}x{nx}) yields degenerate {iy}x{ix} inner chunks "
            f"({spatial_tiles} spatial tiles > {MAX_SPATIAL_TILES_PER_FRAME} cap); "
            "choose composite published grid dimensions or matching inner_chunk_yx"
        )
    return iy, ix


def _pack_volume_shard(
    vols_4d: npt.NDArray[np.float32],
    output: OutputProfile,
    shard_time_steps: int,
) -> bytes:
    n_frames, _, ny, nx = vols_4d.shape
    if n_frames < 1 or n_frames > shard_time_steps:
        raise ValueError(f"Invalid frame count {n_frames} for shard_time_steps={shard_time_steps}")
    if not bool(np.all(np.isfinite(vols_4d))):
        raise ValueError("Refusing to encode non-finite float32 values into ZEP-0002 shard")
    iy, ix = resolve_inner_chunk_yx(ny, nx, output)
    spatial_tiles = (ny // iy) * (nx // ix)
    body = bytearray()
    index = bytearray()
    level = output.zstd_level
    for s in range(shard_time_steps):
        if s >= n_frames:
            index.extend(
                struct.pack("<QQ", EMPTY_CHUNK_SENTINEL, EMPTY_CHUNK_SENTINEL) * spatial_tiles
            )
            continue
        frame_vol = vols_4d[s]
        for ty in range(0, ny, iy):
            for tx in range(0, nx, ix):
                raw_le = np.ascontiguousarray(
                    frame_vol[:, ty : ty + iy, tx : tx + ix], dtype="<f4"
                ).tobytes()
                comp = bytes(_zstd.compress(raw_le, level))
                index.extend(struct.pack("<QQ", len(body), len(comp)))
                body.extend(comp)
    footer_crc = struct.pack("<I", crc32c_u32(bytes(index)))
    return bytes(body) + bytes(index) + footer_crc


def encode_2d_shard_bytes(
    frames: Sequence[PubMass2D],
    output: OutputProfile,
    *,
    shard_time_steps: int = 1,
) -> bytes:
    """Encode 1..shard_time_steps Published-Grid 2D slabs into a ZEP-0002 shard."""
    if not frames:
        raise ValueError("encode_2d_shard_bytes requires >= 1 frame")
    ny, nx = frames[0].ny, frames[0].nx
    stacked = np.stack(
        [np.frombuffer(f.buf, dtype=np.float32).reshape(1, ny, nx) for f in frames], axis=0
    )
    return _pack_volume_shard(stacked, output, shard_time_steps)


def encode_3d_shard_bytes(
    frames: Sequence[PubMass3D],
    output: OutputProfile,
    *,
    shard_time_steps: int = 1,
) -> bytes:
    """Encode 1..shard_time_steps Published-Grid 3D volumes into a ZEP-0002 shard."""
    if not frames:
        raise ValueError("encode_3d_shard_bytes requires >= 1 frame")
    nz, ny, nx = frames[0].nz, frames[0].ny, frames[0].nx
    stacked = np.stack(
        [np.frombuffer(v.buf, dtype=np.float32).reshape(nz, ny, nx) for v in frames], axis=0
    )
    return _pack_volume_shard(stacked, output, shard_time_steps)


def verify_shard_index(
    shard_bytes: bytes,
    *,
    shard_time_steps: int,
    n_frames: int,
    ny: int,
    nx: int,
    output: OutputProfile,
) -> tuple[bytes, int, int, int]:
    """Verify ZEP-0002 CRC-32C, contiguous populated tile bounds, and sentinel tail slots."""
    if n_frames < 1 or n_frames > shard_time_steps:
        raise ValueError("Invalid n_frames / shard_time_steps for shard index verification")
    iy, ix = resolve_inner_chunk_yx(ny, nx, output)
    spatial_tiles = (ny // iy) * (nx // ix)
    n_tiles = shard_time_steps * spatial_tiles
    idx_len = n_tiles * 16
    if len(shard_bytes) < idx_len + 4:
        raise ValueError("Truncated ZEP-0002 shard payload")
    body_len = len(shard_bytes) - idx_len - 4
    idx_raw = shard_bytes[body_len:-4]
    (expected_crc,) = struct.unpack("<I", shard_bytes[-4:])
    if crc32c_u32(idx_raw) != expected_crc:
        raise ValueError("ZEP-0002 shard index CRC-32C mismatch")

    populated_tiles = n_frames * spatial_tiles
    cursor = 0
    for tile_i in range(n_tiles):
        off, nbytes = struct.unpack_from("<QQ", idx_raw, tile_i * 16)
        if tile_i < populated_tiles:
            if off == EMPTY_CHUNK_SENTINEL or nbytes == EMPTY_CHUNK_SENTINEL or nbytes == 0:
                raise ValueError(f"Unexpected empty inner chunk in populated tile {tile_i}")
            if off != cursor or off + nbytes > body_len:
                raise ValueError(f"Invalid ZEP-0002 offset/length ({off}, {nbytes}) at {tile_i}")
            cursor += nbytes
        elif off != EMPTY_CHUNK_SENTINEL or nbytes != EMPTY_CHUNK_SENTINEL:
            raise ValueError(f"Expected sentinel in unpopulated tail tile {tile_i}")
    if cursor != body_len:
        raise ValueError("ZEP-0002 shard body contains unaccounted trailing bytes")
    return idx_raw, iy, ix, spatial_tiles


def decode_shard_f32_array(
    shard_bytes: bytes,
    *,
    shard_time_steps: int = 1,
    n_frames: int | None = None,
    nz: int,
    ny: int,
    nx: int,
    output: OutputProfile,
) -> npt.NDArray[np.float32]:
    """Verify ZEP-0002 index + CRC-32C and decompress all populated tiles to a NumPy array."""
    populated = shard_time_steps if n_frames is None else n_frames
    idx_raw, iy, ix, spatial_tiles = verify_shard_index(
        shard_bytes,
        shard_time_steps=shard_time_steps,
        n_frames=populated,
        ny=ny,
        nx=nx,
        output=output,
    )
    out_arr = np.full((shard_time_steps, nz, ny, nx), np.nan, dtype=np.float32)
    tile_bytes = nz * iy * ix * 4
    for s in range(populated):
        sp_idx = 0
        for ty in range(0, ny, iy):
            for tx in range(0, nx, ix):
                off, nbytes = struct.unpack_from("<QQ", idx_raw, (s * spatial_tiles + sp_idx) * 16)
                sp_idx += 1
                try:
                    raw_tile = bytes(_zstd.decompress(shard_bytes[off : off + nbytes]))
                except Exception as exc:
                    raise ValueError(f"Corrupt Zstd payload in tile {sp_idx - 1}") from exc
                if len(raw_tile) != tile_bytes:
                    raise ValueError("Corrupt Zstd inner chunk byte length")
                tile_vals = np.frombuffer(raw_tile, dtype="<f4").reshape(nz, iy, ix)
                out_arr[s, :, ty : ty + iy, tx : tx + ix] = tile_vals.astype(np.float32, copy=False)
    return out_arr


def decode_shard_f32(
    shard_bytes: bytes,
    *,
    shard_time_steps: int = 1,
    n_frames: int | None = None,
    nz: int,
    ny: int,
    nx: int,
    output: OutputProfile,
) -> array[float]:
    """Verify ZEP-0002 index + CRC-32C and decode `shard_time_steps` slots to float32 array."""
    out_arr = decode_shard_f32_array(
        shard_bytes,
        shard_time_steps=shard_time_steps,
        n_frames=n_frames,
        nz=nz,
        ny=ny,
        nx=nx,
        output=output,
    )
    out = array("f")
    out.frombytes(np.ascontiguousarray(out_arr, dtype=np.float32).tobytes())
    return out


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
