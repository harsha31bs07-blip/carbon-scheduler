# Carbon Scheduler — Implementation Plan

_Written 2026-10-06, after the repo cleanup. This file replaces the old status reports (they are still in git history at `c6fd6af`)._

## Where the project stands

The system schedules a 4-step support-ticket pipeline (`classify → retrieve → draft → summarize`). For each step, it picks a model, location and time offset by scoring candidates on latency, cost, energy, carbon and accuracy.

| Layer | Path | State |
|---|---|---|
| Contract | `shared/schema.py` | `Candidate` dataclass and `STEP_DEFINITIONS`. Stable. |
| Data | `data/` | Estimates for cost, energy, latency, carbon and accuracy. Currently being moved to "hybrid" (uncommitted). |
| Scheduler | `scheduler/` | Filter, normalize and weighted-score. Hardcoded to local-only. |
| Workflow | `workflow/executor.py`, `model_clients.py` | Runs the pipeline against Ollama (local) and Gemini/NVIDIA (cloud). Has a replay mode. |
| Dashboard | `workflow/dashboard/app.py` | Streamlit app with OTP email login, about 1,080 lines in one file. |

**Tests:** 87 total, **40 failing**. The failures were already there before the cleanup.

## Phase 0 — Hygiene (do first, ~1 hour)

1. **Remove `.env` from git history before the first push.** Commit `c6fd6af` contains real `GOOGLE_API_KEY`, `NVIDIA_API_KEY` and SMTP credentials. The GitHub remote is still empty, so nothing has leaked yet. Since it's the only commit and it's unpushed, `git commit --amend` after staging the cleanup is enough. To be safe, rotate the keys anyway.
2. **Delete the leftover untracked files** (see "Cleanup done" below).
3. **Add `requirements.txt`.** The third-party imports are `streamlit`, `pandas`, `plotly`, `python-dotenv`, `requests`, `google-genai`, `codecarbon` (optional) and `pytest`. Pin the versions.
4. **Replace `PROJECT_STRUCTURE.md` with a short `README.md`.** The current file describes the old cloud-only design and lists deleted files.

## Phase 1 — Get the test suite green (~half a day)

A green suite is needed before any other change can be trusted.

1. **`workflow/tests/test_workflow.py` NameErrors (25 tests).** The file uses `get_policy_weights`, `get_step`, `STEPS`, `generate_candidates` and `get_carbon` without importing them. Add the imports from `scheduler.policies`, `scheduler.candidate_gen` and `scheduler.data_adapter`. It also imports `schedule_step` twice; remove the duplicate.
2. **Stale assertions (about 15 tests).** These were written for the cloud model set. Examples: `'local-llama3.2' in ['haiku','sonnet','opus']`, `est_cost_usd > 0` (local models cost $0), and "policies produce ≥2 distinct winners" (impossible with one location).
   - Decide which behaviour is correct, then update the test, not the code, unless the code is wrong.
   - Make the tests **independent of which API keys are present**. Right now the candidate pool changes depending on `.env`, so results differ from machine to machine. Monkeypatch `GEMINI_AVAILABLE` and `NVIDIA_AVAILABLE` in a fixture.
3. Add a CI workflow (GitHub Actions: `pytest` on push) once the suite is green.

## Phase 2 — Finish hybrid mode properly (~1 day)

The uncommitted work in `HYBRID_MODE_CHANGES_NEEDED.md` is only half wired:

```
data.candidate_builder.DEFAULT_MODELS    -> local + haiku + opus      (hybrid ✓)
data.candidate_builder.DEFAULT_LOCATIONS -> local-gpu + 3 cloud       (hybrid ✓)
scheduler.candidate_gen.MODELS / REGIONS -> local-only, hardcoded     (✗ never sees cloud)
workflow.executor baseline               -> local-qwen3.5 @ local-gpu (✗ hardcoded)
```

1. **Use one source for the candidate pool.** `scheduler/candidate_gen.py` should read `DEFAULT_MODELS`, `DEFAULT_LOCATIONS` and `DEFAULT_OFFSETS` from the data layer (or call `data.candidate_builder.enumerate_candidates`) instead of keeping its own lists.
2. **Fix the dependency direction.** `data/candidate_builder.py` imports `workflow.model_clients` to check key availability, which makes the lowest layer depend on the highest.
   - Move availability detection into a small `shared/availability.py` (reads env vars, no SDK imports). Both layers can then use it.
   - Compute availability lazily, in a function, not at import time. Today it's frozen when the module loads, so tests and the dashboard can't switch modes.
3. **Constrain invalid combinations.** Local models should only pair with `local-gpu`, and cloud models only with cloud regions. Right now the cross product would produce candidates like `haiku @ local-gpu`.
4. **Clean up model naming.** `haiku`/`sonnet`/`opus` are Claude names, but `MODEL_NAME_MAP` sends them to Gemini and Nemotron, and `model_table.py` prices them at Claude rates. Rename the abstract tiers to provider-neutral names (e.g. `cloud-small` / `cloud-mid` / `cloud-large`), or make the pricing and energy rows match the model that actually runs. Otherwise the reported cost and carbon savings rest on the wrong numbers.
5. **Make the baseline mode-aware.** In hybrid mode, the baseline should be the biggest available model at a fixed region with no deferral.
6. Remove `HYBRID_MODE_CHANGES_NEEDED.md` after this lands.

## Phase 3 — Scheduler quality (~1 day)

1. **Performance.** `scheduler/data_adapter.py` calls `estimate_candidate()` five times per candidate, once for each metric. Call it once and reuse the returned `Candidate`.
2. **Fallback edge case.** In `scorer.py`, the fallback falls back to raw latency when `max_latency_ms` is 0 or missing. Also, `min_accuracy` defaults to 0.7 even though every step defines 0.60. Make the defaults explicit and test them.
3. **Normalization.** Min-max normalization over a tiny pool (2–3 candidates) turns small differences into 0/1 swings. Consider normalizing against fixed reference ranges, or reporting the score breakdown in the dashboard so the choices can be explained.
4. **Time shifting.** Check that the `data/time_curve.py` carbon curves give eco a real reason to defer. Then show "deferred N h, saved X g" explicitly.

## Phase 4 — Workflow and dashboard (~1–2 days)

1. **Split `dashboard/app.py`** (about 1,080 lines) into `auth.py`, `data_loading.py`, `charts.py` and `app.py`.
2. **Decide on the OTP email login.** For a local demo tool it adds SMTP setup and failure modes. Keep it behind a flag (it's `OTP_DEV_MODE` today) or drop it.
3. **Show the mode and fallbacks** (Step 3 of the hybrid doc): an "Offline / Hybrid: Gemini / Fully online" badge, plus a visible marker when a cloud call fell back to local.
4. **Replay data management.** `results/` holds 80 JSONL runs, and replay picks the newest file that matches a step and model. Choose a small fixed set of demo runs (e.g. `results/demo/`), point replay at it, and gitignore everything else in `results/`.
5. **Measured vs. estimated.** Label every metric in the UI as MEASURED (real call), ESTIMATED (model table) or REPLAYED.

## Phase 5 — Credibility of the numbers (ongoing)

1. Run `data/eval/run_eval.py` against real models so the accuracy values are measured rather than assumed (`is_measured: true`).
2. Re-run `full_benchmark.py` and `workflow/benchmark_local_models.py` (measured GPU power through `codecarbon` or `nvidia-smi`), then update the latency/energy tables in `data/model_table.py` with a dated source comment.
3. Write down the carbon methodology in one place: grid intensity source, PUE assumptions, and what counts as a projection.

## Suggested order

Phase 0 → Phase 1 → Phase 2 → Phase 3 and Phase 4 in parallel → Phase 5. Commit at the end of each phase.

---

## Cleanup done (2026-10-06)

Staged with `git rm` (any of them can be restored with `git checkout HEAD -- <path>`):

- `carbon-scheduler/`: a stale, older nested copy of the whole project
- every committed `__pycache__/*.pyc` (untracked only)
- one-off scripts: `fix4-7.py`, `fix_tests.py`, `update_tests.py`, `check*.py`, `generate_test.py`, `verify_*.py`, `quick_benchmark.py`, root-level `test_*.py`, `workflow/phase0_diagnosis.py`, `workflow/phase1_audit*.py`, `workflow/local_llm_benchmark*.py`
- captured logs: `test_output*.txt`, `test_results.txt`, `phase1_results.txt`
- 15 point-in-time status reports (`DASHBOARD_*`, `DEMO_READY`, `FINAL_STATUS`, `LATEST_FIXES`, `*_REPORT.md`, `workflow/PHASE*`, and others)
- `.env` (untracked only; the file stays on disk)

Other changes: `.gitignore` rewritten as UTF-8 (the old one was UTF-16, so git ignored nothing), and the hardcoded Desktop path in `demo_prep.py` fixed.

Kept on purpose: `PERSON_A/B/C_*.md` (the original requirements), `DEMO_GUIDE.md`, `DEMO_SCRIPT_LOCAL.md`, `GMAIL_SETUP_GUIDE.md`, `workflow/LOCAL_LLM_PARAMETERS.md`, `workflow/OFFLINE_CAPABILITY.md`, `results/` (needed by replay), `full_benchmark.py`, `workflow/benchmark_local_models.py`, `demo_prep.py`.
