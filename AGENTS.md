# Subtractive simplicity

Keep the reachable surface small. Nest detail behind it. Shake off anything no
current caller, reader, or check reaches. YAGNI, KISS, and DRY are that cut:
build only the present need, keep it plain, and give each fact one owner. Every
coding action must leave the touched neighborhood simpler—inline single-caller
wrappers, collapse duplicate state, delete dead code, and tighten types before
adding anything new. Fast execution and low compute cost share that surface:
move fewer bytes, allocate fewer arrays, and burn fewer core-hours.

- Read the nearest `README.md` and `contracts/` types before implementation
  files. [Architecture](docs/architecture.md) owns the package map and layer DAG.
- Keep entry points small. State purpose, inputs, units, outputs, and errors at
  the boundary. Keep helpers private (`_name`) and export only `__all__`.
- Give each fact one owner:
  - [vendor/producer-contract](vendor/producer-contract/README.md) holds the
    `wrf-zarr-v3` wire schema (provisional until promoted into `windy-av-layer`;
    sync via `scripts/sync_contract.py --from-consumer`).
  - [zarr_schema.py](src/av_wrf/contracts/zarr_schema.py) mirrors the 22-array
    template in Python.
  - [config/](config/README.md) is the sole owner of region, product, domain,
    source, science, and output profiles; never hardcode region or product IDs
    in `src/`, `scripts/`, `tests/`, `docker/`, or Markdown docs.
  - `scripts/generate_reference.py` generates [docs/reference.md](docs/reference.md).
- Respect the strict producer boundary. `av-wrf` produces native model-level
  `forecast.zarr`, `_ready.json`, and `_diagnostics.json` (extensions follow
  [plan.md](docs/plan.md)). Soaring parcel physics, subgrid DEM columns,
  `samples.bin`, `profiles.bin`, and `.pmtiles` belong exclusively to
  `windy-av-layer`.
- Obey the 5-layer one-way import DAG (`L0 contracts <- L1 core <- L2 codecs <-
  L3 runner <- L4 cli`):
  - `contracts` and `core` are pure deterministic functions: zero file I/O,
    network, subprocesses, `netCDF4`/`zarr` handles, RNG, or wall-clock calls.
  - `cli/env.py` is the sole module permitted to read `os.environ`.
  - Relative imports and dynamic `importlib`/`__import__` calls are forbidden.
  - `core` foundations (`grid`, `thermo`, `vertical`, `nesting`, `namelist`,
    `physics_namelist`, `post_3d`) are the only peer modules other `core`
    modules may import.
- Enforce subtractive budgets (`scripts/check_repo.py`):
  - At most `350` lines per `.py` file, `60` lines per function, and `100`
    columns per line (`E501`).
  - Zero `typing.Any`, zero `# type: ignore`, zero bare `zip()` (`B905`), zero
    `abc.ABC` inheritance, and `@dataclass(frozen=True)` on every record.
  - Generic dumping-ground files (`utils.py`, `helpers.py`, `common.py`,
    `misc.py`, `base.py`) and undeclared directories are forbidden.
  - Delete unreached stubs, exports, config fields, and comments in the change
    that leaves them unreached. Cite upstream WRF/weather docs in comments where
    physical conventions or solver workarounds matter.
  - Annotate every non-obvious physical convention, upstream Fortran quirk,
    IEEE-754 edge case, and concurrency invariant with one or two inline lines
    stating *why*, so a future reviewer does not have to re-derive it.
- Validate every change with `uv run verify` and `git diff --check`. Update
  `vendor/producer-contract/` only through
  `scripts/sync_contract.py --allow-lock-rewrite` while the schema remains
  provisional, and never loosen `tests/oracle/` tolerances to silence a check.

## Documentation

Write terse, plain prose for an experienced engineer. Every sentence must earn
its space.

- **Be terse.** Open each `README.md` with one sentence on what the package
  does and a short command or import example. Caps: `AGENTS.md <= 90` lines,
  `docs/architecture.md <= 200` lines, package `README.md <= 40` lines.
- **State what code does positively; ban "X, not Y" contrasts.** Write
  "`core.grid` crops `bdy_trim_cells` from each edge"—never "from `core.grid`,
  not in the Zarr writer." State a negative only for deliberate architectural
  absences at a layer boundary.
- **Cut defensive disclaimers.** Encode units, staggering, and reference frames
  in field names and types such as `PubMass3D`.
- **Leave schemas and standard specs in code.** Link to the owning module or
  upstream specification rather than restating field bounds in Markdown.
