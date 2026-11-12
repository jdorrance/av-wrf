"""Spin-up pressure tendency diagnostics."""

from av_wrf.core import pressure_tendency_abs_hpa_hr


def test_pressure_tendency_does_not_cancel_opposing_changes() -> None:
    assert pressure_tendency_abs_hpa_hr((90_000, 91_000), (90_150, 90_850), 1.0) == 1.5
