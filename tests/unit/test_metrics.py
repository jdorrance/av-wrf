"""Forecast diagnostics preserve measured values and legacy receipt readability."""

from av_wrf.contracts import DiagnosticsReceipt
from av_wrf.core import pressure_tendency_abs_hpa_hr


def test_pressure_tendency_does_not_cancel_opposing_changes() -> None:
    assert pressure_tendency_abs_hpa_hr((90_000, 91_000), (90_150, 90_850), 1.0) == 1.5


def test_diagnostics_omit_unmeasured_timesteps_and_read_legacy_receipts() -> None:
    receipt = DiagnosticsReceipt(
        model_id="model",
        cycle_partition="20260101/00",
        attempt_id="attempt",
        stage_seconds=(("post", 1.0),),
        max_cfl=0.3,
        initial_dps_dt_hpa_hr=(0.1,),
        mean_sensible_flux_wm2=100.0,
        mean_latent_flux_wm2=50.0,
        total_precip_mm=2.0,
    )
    doc = receipt.to_doc()
    legacy = {**doc, "adaptive_dt_min_s": 0.0, "adaptive_dt_max_s": 0.0}
    assert "adaptive_dt_min_s" not in doc and "adaptive_dt_max_s" not in doc
    assert DiagnosticsReceipt.from_doc(legacy) == receipt
    assert DiagnosticsReceipt.from_doc(legacy).to_doc() == doc
    assert legacy["adaptive_dt_min_s"] == legacy["adaptive_dt_max_s"] == 0.0
