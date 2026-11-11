"""Thin av-wrf CLI entrypoint routing subcommands to runner or verify."""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path

from av_wrf.cli.env import load_runtime_env
from av_wrf.contracts import BuildManifest
from av_wrf.runner import check_product_config, open_store, plan_product_cycle

CLI_COMMANDS: tuple[tuple[str, str], ...] = (
    ("check", "Validate product, domain, vertical stretch, science, horizon, and Zarr v3."),
    ("plan", "Resolve cycle timestamps and persist an immutable run.lock.json."),
    ("geog", "Preflight static DEM/land-use tiles and build geo_em.d01.nc."),
    ("prep", "Fetch fail-closed forcing slabs, interpolate soil, and run real."),
    ("solve", "Execute MPI WRF-ARW with synchronized wrfrst checkpoints."),
    ("post", "Convert wrfout frames into sharded wrf-zarr-v3 arrays."),
    ("publish", "Verify cycle completeness, write _ready.json, and CAS latest.json."),
    ("reconcile", "Dispatch pending _ready.json webhooks idempotently."),
    ("archive", "Capture rolling as-issued forcing and consumer baselines."),
    ("score", "Score matured forecast leads on NWP and soaring column proxies."),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="av-wrf")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd_name, help_text in CLI_COMMANDS:
        cmd = sub.add_parser(cmd_name, help=help_text)
        cmd.add_argument("--product", default="wrf-armenia-3km", help="Configured product ID")
        cmd.add_argument("--config-dir", default="config", help="Declarative config directory")
        if cmd_name == "plan":
            cmd.add_argument(
                "--cycle",
                required=True,
                help="Publish origin UTC ISO-8601 (e.g. 2026-11-12T00:00:00Z)",
            )
            cmd.add_argument("--attempt", default="att-01", help="Unique attempt identifier")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = load_runtime_env()
    if args.command == "check":
        summary = check_product_config(Path(args.config_dir), str(args.product))
        payload = {"status": "ok", "mpi_ranks": env.mpi_ranks, **dataclasses.asdict(summary)}
        print(json.dumps(payload))
        return 0
    if args.command == "plan":
        store = open_store(env.store_uri)
        manifest = BuildManifest("local-dev", "wrf-4.8.0", "wps-4.7.0", (), "host", "openmpi")
        key, lock = plan_product_cycle(
            Path(args.config_dir),
            store,
            product_id=str(args.product),
            publish_origin_utc=str(args.cycle),
            attempt_id=str(args.attempt),
            mpi_ranks=env.mpi_ranks,
            build_manifest=manifest,
        )
        print(
            json.dumps(
                {"status": "locked", "lock_key": key, "cycle_partition": lock.cycle_partition}
            )
        )
        return 0
    raise NotImplementedError(f"Subcommand '{args.command}' requires Phase 2 solver container")


if __name__ == "__main__":
    raise SystemExit(main())
