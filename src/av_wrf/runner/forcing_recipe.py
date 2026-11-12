"""Source-specific acquisition and validation for normalized WPS forcing."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from av_wrf.contracts import DomainProfile, ForcingProfile


@dataclass(frozen=True)
class ForcingRecipe:
    """Plan acquisition and admit one assembled GRIB2 file per WPS boundary time."""

    identity: tuple[
        str, str, int, float
    ]  # Provider, Vtable, boundary interval s, grid spacing deg.
    check_window: Callable[[ForcingProfile, DomainProfile, datetime, tuple[int, ...]], None]
    file_keys: Callable[[datetime, tuple[int, ...]], Mapping[str, str | tuple[str, ...]]]
    validate_file: Callable[[Path, datetime, ForcingProfile, DomainProfile, int], None]
    metgrid_fields: tuple[str, ...]
    metgrid_soil_dimensions: tuple[str, *tuple[str, ...]]
    vtable_text: str | None = None


__all__ = ["ForcingRecipe"]
