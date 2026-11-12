"""Postprocessing releases full volumes between reads and enforces the locked horizon."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from weakref import ReferenceType, ref

import pytest

from av_wrf.codecs import build_synthetic_wrf_baseline, build_synthetic_wrf_step
from av_wrf.contracts import (
    AccumBaseline,
    CycleWindow,
    DomainProfile,
    NormalizedWrfFrame,
    RawWrfBaselineInput,
    RawWrfStepInput,
    ScienceProfile,
    parse_iso_utc,
)
from av_wrf.core import post_frame, resolve_cycle_window
from av_wrf.runner import LocalFileStore, load_bundle
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.post import _stream_staged_frames
from tests.integration import stage_sparse_config

type _Case = tuple[LoadedBundle, CycleWindow, RawWrfBaselineInput, LocalFileStore]


@pytest.fixture
def stream_case(tmp_path: Path) -> _Case:
    config = tmp_path / "config"
    source = Path(__file__).resolve().parents[2] / "config"
    bundle = load_bundle(config, stage_sparse_config(source, config))
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    window = resolve_cycle_window(bundle.product, bundle.output, origin)
    baseline = build_synthetic_wrf_baseline(
        bundle.domain, origin, int(parse_iso_utc(origin).timestamp())
    )
    return bundle, window, baseline, LocalFileStore(tmp_path / "store")


def test_releases_raw_and_normalized_volumes_before_loading_next_step(stream_case: _Case) -> None:
    bundle, window, baseline, store = stream_case
    previous: list[ReferenceType[object]] = []

    def record_frame(
        base: AccumBaseline, raw: RawWrfStepInput, domain: DomainProfile, science: ScienceProfile
    ) -> tuple[NormalizedWrfFrame, AccumBaseline]:
        frame, result = post_frame(base, raw, domain, science)
        previous.extend((ref(frame), ref(frame.fields_3d["pressure_pa"].buf)))
        return frame, result

    def leads() -> Iterator[RawWrfStepInput]:
        for hour, stamp in zip(window.lead_hours, window.valid_times_utc, strict=True):
            assert all(item() is None for item in previous)
            step = build_synthetic_wrf_step(
                bundle.domain, stamp, baseline.valid_time_unix_s + hour * 3600, hour
            )
            previous.extend((ref(step), ref(step.p_pert_pa.buf)))
            yield step
            del step  # The producer drops its own reference when the consumer resumes it.
        assert all(item() is None for item in previous)

    with patch("av_wrf.runner.post.post_frame", record_frame):
        _stream_staged_frames(store, "staged", baseline, leads(), window, bundle)
    assert len(previous) == 4 * len(window.lead_hours)


@pytest.mark.parametrize(
    ("fault", "message"),
    [("short", "Too few WRF steps"), ("long", "Too many WRF steps"), ("time", "timestamp")],
)
def test_rejects_step_count_and_timestamp_drift(
    stream_case: _Case, fault: str, message: str
) -> None:
    bundle, window, baseline, store = stream_case
    scheduled = list(zip(window.lead_hours, window.valid_times_utc, strict=True))
    if fault == "short":
        scheduled.pop()
    elif fault == "long":
        scheduled.append(scheduled[-1])

    def leads() -> Iterator[RawWrfStepInput]:
        for hour, stamp in scheduled:
            step = build_synthetic_wrf_step(
                bundle.domain, stamp, baseline.valid_time_unix_s + hour * 3600, hour
            )
            yield (
                replace(step, valid_time_unix_s=step.valid_time_unix_s + 1)
                if fault == "time"
                else step
            )

    with pytest.raises(ValueError, match=message):
        _stream_staged_frames(store, "staged", baseline, leads(), window, bundle)
