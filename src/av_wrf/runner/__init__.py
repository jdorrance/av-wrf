"""Public L3a runner surface: stores, mirrors, solver cold-start, staging, and dispatch."""

from __future__ import annotations

from av_wrf.core import parse_cycle_arg
from av_wrf.runner.acquire import acquire_forcing
from av_wrf.runner.bundle import (
    LoadedBundle,
    load_bundle,
    parse_declarative_yaml,
    validate_bundle,
)
from av_wrf.runner.dispatch import (
    DEFAULT_DISPATCH_LEASE_S,
    dispatch_idempotency_key,
    dispatch_receipt_key,
    post_webhook_json,
    reconcile_dispatch,
)
from av_wrf.runner.mirrors import (
    FORCING_MANIFEST_FILENAME,
    verify_forcing_manifest,
    write_forcing_manifest,
)
from av_wrf.runner.post import post_cycle_from_inputs
from av_wrf.runner.publish import load_ready_receipt, publish_cycle_receipt
from av_wrf.runner.s3 import S3Config
from av_wrf.runner.solver import (
    load_build_manifest,
    parse_build_manifest,
    verify_solver_receipt,
)
from av_wrf.runner.solver_exec import run_geogrid, run_wrf_cycle
from av_wrf.runner.stages import (
    check_all_products,
    check_product_config,
    load_run_lock,
    plan_product_cycle,
    prep_cycle_namelists,
)
from av_wrf.runner.store import (
    LocalFileStore,
    ObjectStore,
    attempt_stage_prefix,
    json_bytes,
    open_store,
    post_manifest_key,
    published_cycle_prefix,
    put_idempotent,
    run_lock_key,
    sha256_file,
    sha256_hex,
    solver_receipt_key,
    synthetic_cycle_prefix,
)

__all__ = [
    "DEFAULT_DISPATCH_LEASE_S",
    "FORCING_MANIFEST_FILENAME",
    "LoadedBundle",
    "LocalFileStore",
    "ObjectStore",
    "S3Config",
    "acquire_forcing",
    "attempt_stage_prefix",
    "check_all_products",
    "check_product_config",
    "dispatch_idempotency_key",
    "dispatch_receipt_key",
    "json_bytes",
    "load_build_manifest",
    "load_bundle",
    "load_ready_receipt",
    "load_run_lock",
    "open_store",
    "parse_build_manifest",
    "parse_cycle_arg",
    "parse_declarative_yaml",
    "plan_product_cycle",
    "post_cycle_from_inputs",
    "post_manifest_key",
    "post_webhook_json",
    "prep_cycle_namelists",
    "publish_cycle_receipt",
    "published_cycle_prefix",
    "put_idempotent",
    "reconcile_dispatch",
    "run_geogrid",
    "run_lock_key",
    "run_wrf_cycle",
    "sha256_file",
    "sha256_hex",
    "solver_receipt_key",
    "synthetic_cycle_prefix",
    "validate_bundle",
    "verify_forcing_manifest",
    "verify_solver_receipt",
    "write_forcing_manifest",
]
