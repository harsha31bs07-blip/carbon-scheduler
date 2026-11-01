# Development and verification

Dashboard improvement work adds `python -m workflow.readiness` for local-only setup
diagnostics; the explicit `--probe` option performs read-only destination inventory.
The authenticated landing page is now Overview, with direct access to the validated
offline simulation. See [dashboard operation](dashboard.md) for input selection,
command quoting, exit codes and evidence limits.

The published baseline is commit `2c09887` on `main`. The A2 foundations are being
implemented on `feat/a2-foundations`; the milestone checklist lives in
`IMPLEMENTATION_PLAN.md`.

## Isolated Windows setup

From the project root in PowerShell, with CPython 3.13 installed:

```powershell
python -m venv .venv-a2
.\.venv-a2\Scripts\python.exe -m pip install -r requirements-dev.txt -c constraints/windows-py313.txt
.\.venv-a2\Scripts\python.exe -m pip check
$env:PYTHON_DOTENV_DISABLED = '1'
.\.venv-a2\Scripts\python.exe -m pytest -q
```

`constraints/windows-py313.txt` captures packages installed in a fresh Windows
x64 CPython 3.13.14 environment. The November 1 rehearsal corrected stale
Streamlit/Pillow pins that conflicted with runtime requirements. The complete
constrained installation then succeeded in `.venv-release-20261101`.
It is a platform-specific snapshot, not a portable
lock. Other platforms install `requirements-dev.txt` directly. CI checks both
Windows (with the snapshot) and Linux (with the supported dependency ranges).
Neither path needs provider credentials; model calls are mocked in offline tests.

Latest local verification on 2026-11-01: the clean release environment passed
**1,917 tests in 233.58 seconds**, with dotenv and the pytest cache disabled.
The added scripted-demo test also passed (10.62 seconds on its final targeted
run); together these cover all 1,918 collected tests. Dashboard interactions
were checked with AppTest, and loopback HTTP startup returned 200/ok. See
[release readiness](release-readiness.md) for exact scope and open live gates.
`pip check` reported no broken requirements; `git diff --check` passed with
existing line-ending notices. Coverage includes review gates, staged planning,
atomic planned queue/worker integration, raw-first accounting, failure and retry
coverage, strict bundle-3 replay, matched evaluation protocols/worker execution,
dimension ablations and portable offline comparisons. The queue dashboard tests use a bounded
20-second AppTest timeout, consistent with the full dashboard tests. A prior
full-suite run exceeded the default three-second budget while populating the
first dataframe; its exact stall was not captured. Stores close in `finally`
and fixed dashboard submission starts no worker thread. Focused and subsequent
full-suite runs passed. The deprecated dataframe width argument was updated.

Historical M2 verification on 2026-10-19: 772 tests passed in 27.40 seconds.
M0/M1 verification was 547 passing tests. The expanded Windows/Linux CI matrix
is configured; no remote run was performed in this implementation session.

The P2 source checkpoint is preserved at `.runtime/implementation-source-20261026.json`.
The P3 checkpoint is `.runtime/p3-implementation-source-20261026.json`;
the P4 checkpoint is `.runtime/p4-implementation-source-20261026.json`;
the current P6 tooling checkpoint is `.runtime/p6-implementation-source-20261101.json`.
The workflow quality integration checkpoint is `.runtime/p6-quality-source-20261101.json`.
The final local dashboard/rehearsal checkpoint is `.runtime/p7-implementation-source-20261101.json`.
These identify source and
fixture content without staging or committing pre-existing work.

Restore dotenv loading for a live session by removing the temporary shell flag:

```powershell
Remove-Item Env:PYTHON_DOTENV_DISABLED -ErrorAction SilentlyContinue
```

## Evidence directories

Use `results/runs/` for new local runs, `results/scratch/` for experiments,
`results/demo/` for curated strict replay exports, and `.runtime/` for future queue
state. Historical recordings stay in place. See `results/README.md` for the
versioned evaluation convention.

## Deployment configuration

Copy `config/deployments.example.json` to `config/deployments.local.json`, or pass
another file explicitly. The local file is ignored by Git. Each deployment maps
canonical model IDs to exact provider IDs and declares its endpoint independently
of geography. Unknown region/grid values stay `null`; configured geography needs
a `location_source` explaining its evidence. A generic cloud endpoint does not
prove a selectable physical region.

Credentials belong in environment variables. A deployment's `credential_env`
stores only the variable name. Configuration loading does not contact endpoints
or establish their availability. The default inventory has one local Ollama
deployment; a second machine or region-pinned destination must be configured
explicitly before demonstrating geographic shifting.
