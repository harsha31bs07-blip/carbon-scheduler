# Carbon Scheduler — Implementation Plan

_Started 2026-10-06. The old status reports this replaces are in git history._

## Where the project stands

The system schedules a 4-step support-ticket pipeline (`classify → retrieve → draft → summarize`). For each step it picks a model, location and time offset by scoring candidates on latency, cost, energy, carbon and accuracy.

| Layer | Path | State |
|---|---|---|
| Contract | `shared/schema.py`, `shared/availability.py` | `Candidate`, `STEP_DEFINITIONS`, and offline/hybrid detection |
| Data | `data/` | Estimates and the mode-aware candidate pool (`candidate_pool()`) |
| Scheduler | `scheduler/` | Filter, normalize, weighted-score. All candidates come from `build_candidate()` |
| Workflow | `workflow/executor.py`, `model_clients.py` | Runs the pipeline on Ollama (local) and Gemini/NVIDIA (cloud), with replay and fallback |
| Dashboard | `workflow/dashboard/app.py` | Streamlit with OTP login, about 1,150 lines in one file |

**Tests:** 83 passing. They run offline by default (root `conftest.py`) and don't depend on `.env` or the time of day. CI is in `.github/workflows/tests.yml`.

---

## ✅ Phase 0 — Hygiene (done)

- Removed `.env` from git history. The first commit was rewritten before any push. The old commit is still referenced locally by a Cline checkpoint ref, which a normal `git push origin main` does not send. **Rotate the keys anyway.**
- Fixed `.gitignore` (it was UTF-16, so git ignored nothing). Removed the stale nested project copy, the bytecode, and the one-off scripts and reports.
- Added `requirements.txt`, `requirements-dev.txt`, `pytest.ini` and `README.md`. Completed `.env.example`.

## ✅ Phase 1 — Test suite green (done)

- `workflow/tests/test_workflow.py` was a corrupted splice of `tests/test_scheduler.py`. I rewrote it with real executor tests: dispatch, JSONL logging, replay, fallback, auth fail-fast and the end-to-end pipeline, all with faked model calls.
- Constraint tests now pin their own thresholds instead of relying on the production SLAs. The test of whether policies differ no longer depends on the wall clock.
- Bug fixed: `local-gpu` carried a phantom 195 ms network latency (missing table entry → cross-continent default).
- Added `Candidate.to_dict()` and `from_dict()`.

## ✅ Phase 2 — Hybrid mode (done)

- `shared/availability.py` detects providers lazily from env vars. `data/` no longer imports `workflow/`, and a mode switch takes effect without re-importing.
- Single source for the pool: `data.candidate_builder.candidate_pool()` gives the valid (model, location) pairs. Local models run only on `local-gpu` and cloud tiers only in cloud regions. The scheduler uses this pool.
- One factory, `scheduler.data_adapter.build_candidate()`, is used by the scheduler, both baselines and the executor fallback.
  - **Bug fixed:** the executor baseline priced every step with "generation" tokens (1500/500), which inflated the reported savings for short steps.
  - This also covers the performance item: one estimate per candidate instead of five.
- The baseline is mode-aware: the most accurate available model at a fixed location (offline `local-qwen3.5 @ local-gpu`; with Gemini `opus @ us-west`).
- Fallback is correct: it retries with the right local model, and the log messages no longer say "gemini-3-flash".
- The dashboard shows a mode badge and a fallback badge (your in-progress work, now committed).

### ✅ Item 4 — model naming (done, option b)

- The tiers `haiku`/`sonnet`/`opus` were priced at Claude rates but ran on Gemini 3 Flash (haiku *and* opus) and Nemotron (sonnet). The cloud models are now `gemini-3-flash` and `nemotron-ultra-3`, priced from Google's and NVIDIA-hosting pricing (checked 2026-10-06).
- Cloud time-to-first-token is calibrated from real calls in `results/`.
- The old names still resolve as aliases, so old results load, and replay matches the existing `gemini-3-flash` recordings again.
- `data/eval/run_eval.py` now calls the pipeline's own clients (Ollama, Gemini, NIM) instead of the Anthropic API.
- `gemini-3-pro` has a client but no free tier, so it isn't in the pool. To enable it, add it to `GEMINI_MODELS` in `data/candidate_builder.py` with a `model_table` row ($2 / $12 per MTok).
- Consequence: with Gemini only, the baseline and the scheduler both pick `gemini-3-flash @ us-west`, so the comparison differs only by the time offset. This makes Phase 3 item 1 urgent.

## Phase 3 — Scheduler quality (next, ~1 day)

1. **Deferral is free.** `est_latency_ms` ignores `time_offset_hr`, so even the **speed** policy picks a 2 h delay (observed: every policy picks `haiku @ us-west +2h` in hybrid mode). Add the deferral to the latency used for scoring (or a separate `w_delay` term), and keep it out of the SLA check if the SLA means compute time only.
2. **Policies collapse.** In hybrid mode, speed, balanced and eco pick the same candidate for all 4 steps. After fixing item 1, re-check, then consider fixed-range normalization instead of min-max over tiny pools (min-max turns small differences into 0/1 swings).
3. **Defaults.** In `scorer.py`, `min_accuracy` defaults to 0.7 while every step defines 0.60, and the fallback treats `max_latency_ms == 0` as "no SLA". Make both explicit.
4. **Explainability.** Return the per-metric score breakdown with the winner so the dashboard can show why it was chosen.

## Phase 4 — Workflow and dashboard (~1–2 days)

1. **Executor duplication.** `execute_step` and `_execute_baseline_step` duplicate replay, dispatch, fallback and logging. Extract one `_run_candidate()` helper.
2. **Dashboard availability.** The dashboard reads `model_clients.GEMINI_AVAILABLE`, a snapshot taken at import. Switch it to `shared.availability.mode_label()`.
3. **Split `dashboard/app.py`** into `auth.py`, `data_loading.py`, `charts.py` and `app.py`.
4. **OTP login.** Keep it behind `OTP_DEV_MODE` or drop it; it adds SMTP setup to a local demo.
5. **Replay data.** `results/` has about 80 runs and replay picks the newest match. Curate `results/demo/`, point replay at it, and gitignore the rest.
6. **Label every metric** in the UI as MEASURED, ESTIMATED or REPLAYED.

## Phase 5 — Credibility of the numbers (ongoing)

1. Run `data/eval/run_eval.py` against the real models so accuracy is measured (`is_measured: true`).
2. Re-run `full_benchmark.py` and `workflow/benchmark_local_models.py` (GPU power through `codecarbon`), then update `data/model_table.py` with dated sources.
3. Write the carbon methodology down in one place: intensity source, PUE, and what counts as a projection.

## Order

Phase 3 → Phase 4 → Phase 5. Commit at the end of each phase.
