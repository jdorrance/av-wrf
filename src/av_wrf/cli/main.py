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
    check_all_products,
    check_product_config,
    load_build_manifest,
    load_bundle,
    load_product_ready_receipt,
    open_store,
    plan_product_cycle,
    post_cycle_from_inputs,
    post_webhook_json,
    prep_cycle_namelists,
    publish_cycle_receipt,
    reconcile_dispatch,
    resolve_cli_cycle,
    run_geogrid,
    run_wrf_cycle,
)

CLI_COMMANDS: tuple[tuple[str, str], ...] = (
    ("check", "Validate configured product(s), vertical stretch, namelist, and Zarr v3."),
    ("plan", "Resolve cycle timestamps and persist an immutable run.lock.json."),
    ("geog", "Preflight static DEM/land-use tiles and build geo_em.d01.nc."),
    ("prep", "Write Fortran namelist.wps + namelist.input and forcing_subset.json recipe."),
    ("solve", "Execute WPS ungrib/metgrid + MPI real/wrf and bind solver_receipt.json."),
    ("post", "Stage wrfout frames into attempt-scoped sharded wrf-zarr-v3 arrays."),
    ("publish", "Verify full RunLock horizon, promote Zarr store, and CAS latest.json."),
    ("reconcile", "Dispatch pending _ready.json webhook idempotently (fail closed)."),
)

_CYCLE_CMDS = {"plan", "prep", "solve", "post", "publish", "reconcile"}


def _add_subcommand_flags(cmd_name: str, cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument(
        "--product",
        required=(cmd_name != "check"),
        default=None,
        help="Configured product ID under <config-dir>/products/ (omit on check for all)",
    )
    cmd.add_argument("--config-dir", default="config", help="Declarative config directory")
    if cmd_name in _CYCLE_CMDS:
        cmd.add_argument("--cycle", required=True, help="Publish origin ISO UTC or YYYYMMDD/HH")
        cmd.add_argument("--attempt", default="att-01", help="Unique attempt identifier")
    if cmd_name == "plan":
        cmd.add_argument(
            "--allow-unpinned-dev",
            action="store_true",
            help="Permit local host dry-run when solver build-manifest.json is absent",
        )
    if cmd_name == "geog":
        cmd.add_argument("--work-dir", default="work/geog", help="Target geogrid directory")
    if cmd_name == "prep":
        cmd.add_argument("--work-dir", default="work/prep", help="Target namelist directory")
    if cmd_name == "solve":
        cmd.add_argument("--work-dir", default="work/solve", help="Attempt scratch root")
        cmd.add_argument("--grib-dir", type=Path, required=True, help="Staged GRIB2 input dir")
        cmd.add_argument("--geog-file", type=Path, default=None, help="Prebuilt geo_em.d01.nc")
    if cmd_name == "post":
        cmd.add_argument("--wrfout-dir", type=Path, help="Directory of wrfout_d01_* files")
        cmd.add_argument("--synthetic", action="store_true", help="Stage full synthetic horizon")
    if cmd_name == "publish":
        cmd.add_argument(
            "--allow-synthetic-fixture",
            action="store_true",
            help="Publish synthetic run into isolated fixtures/synthetic/ namespace",
        )
    if cmd_name == "reconcile":
        cmd.add_argument("--webhook-url", default="", help="Consumer webhook URL override")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="av-wrf")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd_name, help_text in CLI_COMMANDS:
        _add_subcommand_flags(cmd_name, sub.add_parser(cmd_name, help=help_text))
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
        return {"status": "prepped", "namelist_wps": str(wps_p), "namelist_input": str(wrf_p)}
    s_rec = run_wrf_cycle(
        cfg_dir,
        stage_store,
        product_id=prod_id,
        cycle_partition=part,
        attempt_id=att,
        work_dir=Path(args.work_dir),
        grib_dir=Path(args.grib_dir),
        geog_file=args.geog_file,
        solver_prefix=Path(env.solver_prefix),
        bundle=bundle,
    )
    return {
        "status": "solved",
        "cycle": s_rec.cycle_partition,
        "wrfout_count": len(s_rec.wrfout_sha256s),
    }


def _dispatch_cycle_cmd(args: argparse.Namespace, env: RuntimeEnv, cfg_dir: Path) -> int:
    dst_store = open_store(env.store_uri)
    stg_store = (
        dst_store if env.stage_store_uri == env.store_uri else open_store(env.stage_store_uri)
    )
    prod_id, att = str(args.product), str(args.attempt)
    bundle = load_bundle(cfg_dir, prod_id)
    orig_utc, part = resolve_cli_cycle(cfg_dir, prod_id, str(args.cycle), bundle=bundle)
    if args.command in {"plan", "prep", "solve"}:
        out = _run_plan_prep_or_solve(args, env, cfg_dir, stg_store, bundle, orig_utc, part)
    elif args.command == "post":
        w_dir = Path(args.wrfout_dir) if args.wrfout_dir is not None else None
        prefix, count = post_cycle_from_inputs(
            cfg_dir,
            stg_store,
            product_id=prod_id,
            cycle_partition=part,
            attempt_id=att,
            wrfout_dir=w_dir,
            synthetic=bool(args.synthetic),
            bundle=bundle,
        )
        out = {"status": "posted", "zarr_prefix": prefix, "leads": count}
    elif args.command == "publish":
        rec = publish_cycle_receipt(
            cfg_dir,
            dst_store,
            product_id=prod_id,
            cycle_partition=part,
            attempt_id=att,
            bundle=bundle,
            stage_store=stg_store,
            allow_synthetic=bool(args.allow_synthetic_fixture),
        )
        out = {
            "status": "published",
            "cycle": rec.cycle_partition,
            "shards": len(rec.shard_sha256s),
            "forcing_profile_id": rec.forcing_profile_id,
        }
    else:
        rec = load_product_ready_receipt(
            cfg_dir, dst_store, product_id=prod_id, cycle_partition=part, bundle=bundle
        )
        target_url = str(args.webhook_url).strip() or env.webhook_url
        sent = reconcile_dispatch(
            dst_store,
            rec,
            lambda r: post_webhook_json(target_url, r),
            destination_id=target_url or "unset",
        )
        out = {"status": "reconciled", "dispatched_now": sent, "cycle": part}
    print(json.dumps(out))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = load_runtime_env()
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
        )
        print(json.dumps({"status": "geog_ready", "geo_em": str(geo_em), "sha256": geo_sha}))
        return 0
    return _dispatch_cycle_cmd(args, env, cfg_dir)


if __name__ == "__main__":
    raise SystemExit(main())
