"""Nested static-geography domain profiles and deterministic geo_em tar bundles."""

from __future__ import annotations

import re
import shutil
import tarfile
from pathlib import Path

from av_wrf.codecs import check_geogrid_rotation, verify_grid_against_domain, verify_wps_fields
from av_wrf.contracts import DomainProfile
from av_wrf.core import forcing_domain
from av_wrf.runner.bundle import LoadedBundle


def geog_domain_profiles(bundle: LoadedBundle) -> tuple[tuple[str, DomainProfile], ...]:
    """Return `(tag, profile)` for every active geogrid domain, coarse parent first."""
    dom = bundle.domain
    outer = forcing_domain(dom, bundle.primary_source.grid_spacing_deg)
    return (("d01", dom),) if outer is dom else (("d01", outer), ("d02", dom))


def validate_geo_em(
    path: Path, domain: DomainProfile, *, lake_depth: bool, repair: bool = False
) -> None:
    """Admit static WPS fields and normalize southern wind rotation before sealing."""
    import netCDF4

    with netCDF4.Dataset(
        str(path), "r+" if repair and domain.std_parallel_1_deg < 0 else "r"
    ) as ds:
        xlong = verify_grid_against_domain(ds, domain, path, wps=True)
        fields = ("HGT_M", "LU_INDEX", "LANDMASK")
        verify_wps_fields(ds, path, (*fields, "LAKE_DEPTH") if lake_depth else fields)
        check_geogrid_rotation(ds, path, domain, xlong, repair=repair)


def pack_geog_files(paths: tuple[Path, ...], dest: Path) -> Path:
    """Pack geo_em files into a deterministic POSIX tar (sorted, zeroed metadata)."""
    with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(paths, key=lambda p: p.name):
            info = tar.gettarinfo(str(path), arcname=path.name)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            with path.open("rb") as stream:
                tar.addfile(info, stream)
    return dest


def extract_geog_bundle(bundle_path: Path, dest_dir: Path) -> tuple[Path, ...]:
    """Extract a nested geo_em tar and return the member paths in dNN order."""
    with tarfile.open(bundle_path, "r") as tar:
        members = tar.getmembers()
        names = {m.name for m in members}
        if (
            len(members) != 2
            or names != {"geo_em.d01.nc", "geo_em.d02.nc"}
            or any(not member.isfile() for member in members)
        ):
            raise ValueError(f"Unexpected nested geography bundle members: {sorted(names)}")
        for member in members:
            # Exact regular-file names and exclusive creation prevent archive aliases/overwrites.
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError(f"Missing geography payload: {member.name}")
            with stream, (dest_dir / member.name).open("xb") as target:
                shutil.copyfileobj(stream, target)
    return tuple(dest_dir / name for name in sorted(names))


def verify_geog_sources(log: str, domain: DomainProfile) -> None:
    """Admit configured field-specific geography sources from the WPS log."""
    selected = set(re.findall(r"Using (\S+) data source for (\S+)\.", log))
    required = {(domain.lai_source, "LAI12M"), (domain.varsso_source, "VAR_SSO")}
    for configured, name in ((domain.dem_source, "HGT_M"), (domain.landuse_source, "LANDUSEF")):
        # GEOGRID tries the global token list independently for each field; 30s may resolve
        # a `default` field before the fallback token (WPS/source_data_module.F).
        allowed = {configured} if configured != "default" else {"30s", "default"}
        if not any((source, name) in selected for source in allowed):
            raise ValueError(f"Geogrid selected {sorted(selected)}, expected {configured}/{name}")
    if not required <= selected:
        raise ValueError(f"Geogrid selected {sorted(selected)}, expected {sorted(required)}")


__all__ = [
    "extract_geog_bundle",
    "geog_domain_profiles",
    "pack_geog_files",
    "validate_geo_em",
    "verify_geog_sources",
]
