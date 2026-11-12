"""Thin av-wrf CLI entrypoint exposing only implemented runner subcommands."""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path

from av_wrf.cli.env import RuntimeEnv, load_runtime_env
from av_wrf.runner import (
    LoadedBundle,
    ObjectStore,
    acquire_forcing,
    check_all_products,
    check_product_config,
    load_build_manifest,
    load_bundle,
    load_ready_receipt,
    load_run_lock,
    open_store,
    parse_cycle_arg,
    plan_product_cycle,
    post_cycle_from_inputs,
    post_webhook_json,
    prep_cycle_namelists,
    publish_cycle_receipt,
    reconcile_dispatch,
    run_geogrid,
    run_wrf_cycle,
    sha256_file,
    write_forcing_manifest,
)

CLI_COMMANDS: tuple[tuple[str, str], ...] = (
    ("check", "Validate configured product(s), vertical stretch, namelist, and Zarr v3."),
    ("plan", "Resolve cycle timestamps and persist an immutable run.lock.json."),
    ("geog", "Build and validate static geography for the active domains."),
    ("prep", "Write Fortran namelist.wps + namelist.input and forcing_subset.json recipe."),
    ("acquire", "Acquire and seal the locked forcing inventory."),
    ("forcing-manifest", "Verify local GRIB2 inputs and record the forcing inventory."),
    ("solve", "Execute WPS ungrib/metgrid + MPI real/wrf and bind solver_receipt.json."),
    ("post", "Stage wrfout frames into attempt-scoped sharded wrf-zarr-v3 arrays."),
    ("publish", "Verify full RunLock horizon, promote Zarr store, and CAS latest.json."),
    ("reconcile", "Dispatch pending _ready.json webhook idempotently (fail closed)."),
)

_CYCLE_CMDS = {name for name, _ in CLI_COMMANDS if name not in {"check", "geog"}}


def _add_subcommand_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument(
        "--product",
        required=(cmd_name != "check"),
        help="Configured product ID under <config-dir>/products/ (omit on check for all)",
    )
    cmd.add_argument("--config-dir", default="config", help="Declarative config directory")
    if cmd_name in _CYCLE_CMDS:
        cmd.add_argument("--cycle", required=True, help="Publish origin ISO UTC or YYYYMMDD/HH")
        attempt = None if cmd_name == "reconcile" else "att-01"
        cmd.add_argument("--attempt", default=attempt, help="Attempt identifier")
    if cmd_name == "plan":
        cmd.add_argument(
            "--allow-unpinned-dev",
            action="store_true",
            help="Permit local host dry-run when solver build-manifest.json is absent",
        )
        cmd.add_argument("--geog-file", type=Path, help="Bind the geography file or bundle SHA-256")
    if cmd_name in {"geog", "prep"}:
        default_dir = "work/geog" if cmd_name == "geog" else "work/prep"
        dir_help = "New geogrid directory" if cmd_name == "geog" else "Target directory"
        cmd.add_argument("--work-dir", default=default_dir, help=dir_help)
    if cmd_name in {"acquire", "forcing-manifest", "solve"}:
        cmd.add_argument("--grib-dir", type=Path, required=True, help="Staged forcing directory")
    if cmd_name == "forcing-manifest":
        cmd.add_argument("--acquisition-inventory", type=Path, help="Source hash inventory path")
        cmd.add_argument(
            "--file",
            action="append",
            required=True,
            metavar="HOUR=FILENAME",
            help="Forcing forecast hour and filename within --grib-dir (repeatable)",
        )
    if cmd_name == "solve":
        cmd.add_argument("--work-dir", default="work/solve", help="Attempt scratch root")
        cmd.add_argument("--geog-file", type=Path, required=True, help="Locked geography path")
    if cmd_name == "post":
        source = cmd.add_mutually_exclusive_group(required=True)
        source.add_argument("--wrfout-dir", type=Path, help="Active-domain WRF output directory")
        source.add_argument("--synthetic", action="store_true", help="Stage full synthetic horizon")
    if cmd_name == "publish":
        cmd.add_argument(
            "--allow-synthetic-fixture",
            action="store_true",
            help="Publish synthetic run into isolated fixtures/synthetic/ namespace",
        )
        cmd.add_argument(
            "--prune-stage",
            action="store_true",
            help="Delete staged attempt prefix after promotion succeeds",
        )
        cmd.add_argument(
            "--store-uri",
            action="append",
            help="Target destination store URI(s) (repeatable; defaults to AV_WRF_STORE_URI)",
        )
    if cmd_name == "reconcile":
        cmd.add_argument("--store-uri", default=None, help="Destination store URI")
        cmd.add_argument("--webhook-url", default="", help="Consumer webhook URL override")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="av-wrf")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd_name, help_text in CLI_COMMANDS:
        cmd = sub.add_parser(cmd_name, help=help_text)
        _add_subcommand_flags(cmd_name, cmd)
        if cmd_name == "acquire":
            cmd.add_argument("--plan", type=Path, help="Verify a prep forcing_subset.json")
            cmd.add_argument("--cache", type=Path, help="ICON source object cache")
            cmd.add_argument("--workers", type=int, default=8, help="Concurrent downloads")
            cmd.add_argument("--gfs-regional-filter", action="store_true", help="Use NOAA cutout")
    return parser


def _run_check(cfg_dir: Path, product_id: str | None, mpi_ranks: int) -> int:
    if product_id is not None:
        summary = check_product_config(cfg_dir, product_id)
        print(json.dumps({"status": "ok", "mpi_ranks": mpi_ranks, **dataclasses.asdict(summary)}))
        return 0
    summaries = check_all_products(cfg_dir)
    out = {
        "status": "ok",
        "mpi_ranks": mpi_ranks,
        "products": [dataclasses.asdict(s) for s in summaries],
    }
    print(json.dumps(out))
    return 0


def _run_plan_prep_or_solve(
    args: argparse.Namespace,
    env: RuntimeEnv,
    cfg_dir: Path,
    stage_store: ObjectStore,
    bundle: LoadedBundle,
    orig_utc: str,
    part: str,
) -> dict[str, object]:
    prod_id, att = bundle.product.product_id, str(args.attempt)
    if args.command == "plan":
        manifest = load_build_manifest(
            Path(env.build_manifest_path),
            env.oci_digest,
            allow_unpinned_dev=bool(args.allow_unpinned_dev),
        )
        key, lock = plan_product_cycle(
            cfg_dir,
            stage_store,
            product_id=prod_id,
            publish_origin_utc=orig_utc,
            attempt_id=att,
            mpi_ranks=env.mpi_ranks,
            build_manifest=manifest,
            geog_bundle_sha256=sha256_file(args.geog_file) if args.geog_file else None,
            bundle=bundle,
        )
        return {"status": "locked", "lock_key": key, "cycle_partition": lock.cycle_partition}
    if args.command == "prep":
        wps_p, wrf_p = prep_cycle_namelists(
            cfg_dir,
            stage_store,
            product_id=prod_id,
            cycle_partition=part,
            attempt_id=att,
            work_dir=Path(args.work_dir),
            bundle=bundle,
        )
        return {
            "status": "prepped",
            "namelist_wps": str(wps_p),
            "namelist_input_real": str(wrf_p.with_suffix(".input.real")),
            "namelist_input": str(wrf_p),
        }
    s_rec = run_wrf_cycle(
        cfg_dir,
        stage_store,
        product_id=prod_id,
        cycle_partition=part,
        attempt_id=att,
        work_dir=Path(args.work_dir),
        grib_dir=Path(args.grib_dir),
        geog_file=args.geog_file,
        mpi_launcher=env.mpi_launcher,
        solver_prefix=Path(env.solver_prefix),
        runtime_manifest=load_build_manifest(Path(env.build_manifest_path), env.oci_digest),
        timeout_s=env.native_timeout_s,
        bundle=bundle,
    )
    return {"status": "solved", "cycle": part, "wrfout_count": len(s_rec.wrfout_sha256s)}


def _run_forcing_manifest_cmd(
    args: argparse.Namespace, stage_store: ObjectStore, bundle: LoadedBundle, part: str
) -> dict[str, object]:
    lock = load_run_lock(
        stage_store, bundle.product.product_id, part, str(args.attempt), bundle=bundle
    )
    files: dict[int, Path] = {}
    grib_dir: Path = args.grib_dir
    for entry in args.file:
        hour, sep, name = entry.partition("=")
        if not sep or not hour.isascii() or not hour.isdecimal() or Path(name).name != name:
            raise ValueError(f"Invalid --file {entry!r}; expected HOUR=FILENAME")
        forecast_hour = int(hour)
        if not name or forecast_hour in files:
            raise ValueError(f"Missing filename or repeated forecast hour in --file {entry!r}")
        files[forecast_hour] = grib_dir / name
    manifest = write_forcing_manifest(
        grib_dir,
        lock,
        bundle.primary_source,
        files,
        domain=bundle.domain,
        acquisition_inventory=args.acquisition_inventory,
    )
    return {"status": "forcing_verified", "forecast_hours": manifest.forecast_hours}


def _run_publish_cmd(
    args: argparse.Namespace,
    env: RuntimeEnv,
    cfg_dir: Path,
    stg_store: ObjectStore,
    bundle: LoadedBundle,
    part: str,
) -> dict[str, object]:
    raw_uris: list[str] = list(dict.fromkeys(args.store_uri or [env.store_uri]))
    destinations = [
        stg_store if uri == env.stage_store_uri else open_store(uri, s3=env.s3) for uri in raw_uris
    ]
    prune = bool(args.prune_stage)
    last_rec = None
    latest: list[str] = []
    for idx, dst_store in enumerate(destinations):
        last_rec = publish_cycle_receipt(
            cfg_dir,
            dst_store,
            product_id=bundle.product.product_id,
            cycle_partition=part,
            attempt_id=str(args.attempt),
            bundle=bundle,
            stage_store=stg_store,
            allow_synthetic=bool(args.allow_synthetic_fixture),
            prune_stage=(prune and idx == len(raw_uris) - 1),
            on_latest=latest.append,
        )
    if last_rec is None:
        raise ValueError("No destination store URI provided to publish")
    return {
        "status": "published",
        "cycle": last_rec.cycle_partition,
        "shards": len(last_rec.shard_sha256s),
        "destinations": len(raw_uris),
        "forcing_profile_id": last_rec.forcing_profile_id,
        "latest": latest,
    }


def _dispatch_cycle_cmd(args: argparse.Namespace, env: RuntimeEnv, cfg_dir: Path) -> int:
    prod_id, att = str(args.product), str(args.attempt)
    bundle = load_bundle(cfg_dir, prod_id)
    orig_utc, part = parse_cycle_arg(str(args.cycle), bundle.product)
    if args.command == "reconcile":
        dst_store = open_store(args.store_uri or env.store_uri, s3=env.s3)
        rec = load_ready_receipt(dst_store, bundle.product.model_id, part)
        if args.attempt is not None and rec.attempt_id != args.attempt:
            raise ValueError(f"Expected published attempt {args.attempt!r}, got {rec.attempt_id!r}")
        if rec.publish_origin_utc != orig_utc:
            raise ValueError("Published receipt origin does not match the requested cycle")
        target_url = str(args.webhook_url).strip() or env.webhook_url
        sent = reconcile_dispatch(
            dst_store,
            rec,
            lambda r: post_webhook_json(target_url, r, bearer_token=env.webhook_token),
            destination_id=target_url,
        )
        out = {"status": "reconciled", "dispatched_now": sent, "cycle": part}
    else:
        stg_store = open_store(env.stage_store_uri, s3=env.s3)
        if args.command in {"plan", "prep", "solve"}:
            out = _run_plan_prep_or_solve(args, env, cfg_dir, stg_store, bundle, orig_utc, part)
        elif args.command == "forcing-manifest":
            out = _run_forcing_manifest_cmd(args, stg_store, bundle, part)
        elif args.command == "acquire":
            lock = load_run_lock(stg_store, prod_id, part, att, bundle=bundle)
            manifest = acquire_forcing(
                bundle,
                lock,
                args.grib_dir,
                plan_path=args.plan,
                cache=args.cache,
                workers=args.workers,
                gfs_regional_filter=args.gfs_regional_filter,
            )
            out = {"status": "forcing_verified", "forecast_hours": manifest.forecast_hours}
        elif args.command == "post":
            prefix, count = post_cycle_from_inputs(
                cfg_dir,
                stg_store,
                product_id=prod_id,
                cycle_partition=part,
                attempt_id=att,
                wrfout_dir=args.wrfout_dir,
                synthetic=bool(args.synthetic),
                bundle=bundle,
            )
            out = {"status": "posted", "zarr_prefix": prefix, "leads": count}
        else:
            out = _run_publish_cmd(args, env, cfg_dir, stg_store, bundle, part)
    print(json.dumps(out))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = load_runtime_env()
    if args.command in _CYCLE_CMDS and args.command != "reconcile" and not env.stage_store_uri:
        parser.error("Set AV_WRF_STAGE_STORE_URI or AV_WRF_STORE_URI for cycle staging")
    if args.command in {"publish", "reconcile"} and not (args.store_uri or env.store_uri):
        parser.error("Set AV_WRF_STORE_URI or pass --store-uri for the destination")
    if args.command == "reconcile" and not (args.webhook_url.strip() or env.webhook_url):
        parser.error("Set AV_WRF_WEBHOOK_URL or pass --webhook-url for dispatch")
    cfg_dir = Path(args.config_dir)
    if args.command == "check":
        prod_opt = str(args.product) if args.product is not None else None
        return _run_check(cfg_dir, prod_opt, env.mpi_ranks)
    if args.command == "geog":
        geo_em, geo_sha = run_geogrid(
            cfg_dir,
            product_id=str(args.product),
            work_dir=Path(args.work_dir),
            solver_prefix=Path(env.solver_prefix),
            timeout_s=env.native_timeout_s,
        )
        print(json.dumps({"status": "geog_ready", "geo_em": str(geo_em), "sha256": geo_sha}))
        return 0
    return _dispatch_cycle_cmd(args, env, cfg_dir)
