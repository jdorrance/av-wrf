"""Pure lead-hour selection, UTC/partition parsing, local deadline SLA, and cycle-window math.

References:
- Consumer horizon & lead rules: windy-av-layer/packages/contracts/src/products.ts
- Consumer cycle-age admission: windy-av-layer/apps/batch/src/publication.ts
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from av_wrf.contracts import (
    CycleWindow,
    ForcingProfile,
    OutputProfile,
    ProductSpec,
    format_iso_utc,
    parse_iso_utc,
)

MIN_SPINUP_HOURS = 6
MIN_CRON_ARRIVAL_BUFFER_HOURS = 0.25
MIN_SOLVER_DEADLINE_MARGIN_HOURS = 1.0
MAX_FUTURE_CLOCK_SKEW_MINUTES = 15


def selected_lead_hours(output: OutputProfile) -> tuple[int, ...]:
    """Return strictly increasing consumer lead hours (>= 1) for an OutputProfile."""
    if output.hourly_through_hour < 1 or output.hourly_through_hour > output.map_end_hour:
        raise ValueError("hourly_through_hour must lie in 1..map_end_hour")
    if output.tail_step_hours < 1:
        raise ValueError("tail_step_hours must be >= 1")
    tail_span = output.map_end_hour - output.hourly_through_hour
    if tail_span % output.tail_step_hours != 0:
        raise ValueError("Tail horizon must be evenly divisible by tail_step_hours")
    hourly = tuple(range(1, output.hourly_through_hour + 1))
    tail = tuple(
        range(
            output.hourly_through_hour + output.tail_step_hours,
            output.map_end_hour + 1,
            output.tail_step_hours,
        )
    )
    return hourly + tail


def _parse_cron_hour_minute(cron_expr: str) -> tuple[int, int]:
    parts = cron_expr.strip().split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        raise ValueError(f"Expected a daily UTC cron expression, got '{cron_expr}'")
    minute = int(parts[0])
    hour = int(parts[1])
    if not (0 <= minute < 60 and 0 <= hour < 24):
        raise ValueError(f"Invalid cron minute/hour in '{cron_expr}'")
    return hour, minute


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    token = hhmm.strip()
    hh_s, sep, mm_s = token.partition(":")
    if sep != ":" or len(hh_s) != 2 or len(mm_s) != 2 or not (hh_s.isdigit() and mm_s.isdigit()):
        raise ValueError(f"Expected HH:MM local_deadline_hhmm, got '{hhmm}'")
    hh, mm = int(hh_s), int(mm_s)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"Out-of-range HH:MM in local_deadline_hhmm '{hhmm}'")
    return hh, mm


def _briefing_clock(product: ProductSpec) -> tuple[ZoneInfo, int, int]:
    try:
        tz = ZoneInfo(product.region_tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA region_tz '{product.region_tz}'") from exc
    dl_h, dl_m = _parse_hhmm(product.local_deadline_hhmm)
    return tz, dl_h, dl_m


def _briefing_deadline_utc(origin_utc: datetime, clock: tuple[ZoneInfo, int, int]) -> datetime:
    tz, hour, minute = clock
    origin_local = origin_utc.astimezone(tz)
    deadline_local = origin_local.replace(hour=hour, minute=minute, second=0, fold=0)
    if deadline_local.astimezone(UTC) <= origin_utc:
        deadline_local += timedelta(days=1)
    return deadline_local.astimezone(UTC)


def _latest_live_age_hours(product: ProductSpec, output: OutputProfile) -> int:
    return min(product.max_cycle_age_hours, output.map_end_hour - product.min_horizon_hours)


def validate_horizon_policy(
    product: ProductSpec,
    source: ForcingProfile,
    output: OutputProfile,
    *,
    publish_origin_utc: str | None = None,
) -> int:
    """Verify spin-up, cron arrival margin, local briefing deadline, and remaining horizon."""
    if product.spinup_hours < MIN_SPINUP_HOURS:
        raise ValueError(f"spinup_hours ({product.spinup_hours}) must be >= {MIN_SPINUP_HOURS}")
    if not 0 < product.min_horizon_hours < output.map_end_hour or product.max_cycle_age_hours < 0:
        raise ValueError("Expected a positive remaining horizon and non-negative cycle age")
    origin_gap = (product.publish_origin_hour_utc - product.forcing_cycle_hour_utc) % 24
    if origin_gap < product.spinup_hours:
        raise ValueError(
            f"Gap from forcing_cycle_hour_utc to publish_origin_hour_utc ({origin_gap}h) "
            f"is shorter than spinup_hours ({product.spinup_hours}h)"
        )
    cron_h, cron_m = _parse_cron_hour_minute(product.schedule_cron_utc)
    cron_offset_min = (60 * (cron_h - product.forcing_cycle_hour_utc) + cron_m) % (24 * 60)
    cron_offset_h = cron_offset_min / 60
    required_ready_h = source.source_ready_lag_p95_hours + MIN_CRON_ARRIVAL_BUFFER_HOURS
    if cron_offset_h < required_ready_h:
        raise ValueError(
            f"Schedule '{product.schedule_cron_utc}' triggers {cron_offset_h:.2f}h after "
            f"{product.forcing_cycle_hour_utc:02d}Z, before ready window ({required_ready_h:.2f}h)"
        )
    cron_after_origin = timedelta(minutes=cron_offset_min - 60 * origin_gap)
    margin = timedelta(hours=MIN_SOLVER_DEADLINE_MARGIN_HOURS)
    latest_live_age = _latest_live_age_hours(product, output)
    if timedelta(hours=latest_live_age) - cron_after_origin < margin:
        raise ValueError(
            f"Daily UTC cron leaves less than {MIN_SOLVER_DEADLINE_MARGIN_HOURS:g}h "
            "before the live cycle expires"
        )
    clock = _briefing_clock(product)
    if publish_origin_utc is not None:
        origin = parse_iso_utc(publish_origin_utc)
        until_briefing = _briefing_deadline_utc(origin, clock) - (origin + cron_after_origin)
        if until_briefing < margin:
            hours = until_briefing.total_seconds() / 3600
            raise ValueError(f"Daily UTC cron leaves only {hours:.2f}h before briefing deadline")
    if not source.fail_closed:
        raise ValueError(f"ForcingProfile '{source.profile_id}' must enforce fail_closed=True")
    return output.map_end_hour - latest_live_age


def is_cycle_live_fresh(
    product: ProductSpec,
    output: OutputProfile,
    publish_origin_utc: str,
    now_utc_iso: str,
) -> bool:
    """Return True iff cycle satisfies live clock, max_cycle_age, and min_horizon bounds."""
    origin_dt = parse_iso_utc(publish_origin_utc)
    now_dt = parse_iso_utc(now_utc_iso)
    earliest_live_dt = origin_dt - timedelta(
        hours=product.spinup_hours, minutes=MAX_FUTURE_CLOCK_SKEW_MINUTES
    )
    latest_live_age = _latest_live_age_hours(product, output)
    return (
        latest_live_age >= 0
        and earliest_live_dt <= now_dt <= origin_dt + timedelta(hours=latest_live_age)
        and now_dt <= _briefing_deadline_utc(origin_dt, _briefing_clock(product))
    )


def parse_cycle_arg(raw_cycle: str, product: ProductSpec) -> tuple[str, str]:
    """Normalize a strict ISO UTC timestamp or YYYYMMDD/HH partition into (iso_utc, partition)."""
    token = raw_cycle.strip()
    if "/" in token and "T" not in token:
        date_part, sep, hour_part = token.partition("/")
        if not (
            sep == "/"
            and len(date_part) == 8
            and date_part.isdigit()
            and len(hour_part) == 2
            and hour_part.isdigit()
        ):
            raise ValueError(f"Invalid cycle partition '{raw_cycle}'; expected YYYYMMDD/HH")
        dt = datetime(
            int(date_part[:4]),
            int(date_part[4:6]),
            int(date_part[6:8]),
            int(hour_part),
            0,
            0,
            tzinfo=UTC,
        )
    else:
        dt = parse_iso_utc(token)
        if dt.minute != 0 or dt.second != 0 or dt.microsecond != 0:
            raise ValueError("Cycle origin must align to an exact UTC hour")
    if dt.hour != product.publish_origin_hour_utc:
        raise ValueError(
            f"Origin hour {dt.hour:02d}Z != configured {product.publish_origin_hour_utc:02d}Z"
        )
    return format_iso_utc(dt), dt.strftime("%Y%m%d/%H")


def resolve_cycle_window(
    product: ProductSpec,
    output: OutputProfile,
    publish_origin_iso_utc: str,
) -> CycleWindow:
    """Resolve forcing, WRF spin-up init, publish origin, and step bounds in UTC."""
    origin_iso, partition = parse_cycle_arg(publish_origin_iso_utc, product)
    origin_dt = parse_iso_utc(origin_iso)
    origin_gap_h = (origin_dt.hour - product.forcing_cycle_hour_utc) % 24
    forcing_dt = origin_dt - timedelta(hours=origin_gap_h)
    wrf_init_dt = origin_dt - timedelta(hours=product.spinup_hours)
    leads = selected_lead_hours(output)
    valid_times: list[str] = []
    window_starts: list[str] = []
    prev_h = 0
    for h in leads:
        valid_times.append(format_iso_utc(origin_dt + timedelta(hours=h)))
        window_starts.append(format_iso_utc(origin_dt + timedelta(hours=prev_h)))
        prev_h = h
    return CycleWindow(
        forcing_cycle_utc=format_iso_utc(forcing_dt),
        wrf_init_utc=format_iso_utc(wrf_init_dt),
        publish_origin_utc=origin_iso,
        cycle_partition=partition,
        lead_hours=leads,
        valid_times_utc=tuple(valid_times),
        window_starts_utc=tuple(window_starts),
    )
