# Human quality review

The first quality-review implementation exports retained calibration evidence,
validates explicit human judgments, and joins those judgments when freezing new
profiles. It never edits raw trials or fills in semantic scores automatically.
Quality remains unknown until judgments are supplied. This tooling does not yet
establish release-quality review coverage or end-to-end workflow quality.

## Prepared local packet

The initial packet is at
[`results/reviews/m3-local-pending-20261026/`](../results/reviews/m3-local-pending-20261026/manifest.json).
It contains 99 retained trials, 80 reviewable outputs, and 19 excluded warm-up or
failed attempts:

- `packet.json`: exact raw trials (including prompts and outputs), the rubric,
  recorded cases/KB, input fingerprints and exclusion reasons.
- `reviews.pending.json`: one blank review template for each eligible output.
- `manifest.json`: file checksums and counts, written after the other files.

The first pilot used a source-snapshot hash with JSON default separators; the
follow-up used canonical JSON. The packet identifies the method and checks both
against their retained files. When the old manifest lacks a snapshot binding,
its case and KB fingerprints independently bind the review context. Missing
retained context is explicit; today's workload files are never substituted.

## Review and approve

Read each trial's prompt, actual `result.text`, case guidance and corresponding
retained KB. The calibration context is a fixture, so these judgments concern
individual task outputs; they do not establish that a complete workflow ran.
The [versioned rubric](../data/workloads/quality_rubric.v1.json) covers category
correctness, relevant and faithful evidence, grounded responses, invented
actions/promises, unsupported questions and faithful handoff summaries.

Work on a copy of `reviews.pending.json`. For each output personally reviewed:

1. Fill every criterion's `score` (0 to 1) and nonempty `rationale`.
2. Set the overall `score` to the arithmetic mean of those criterion scores.
3. Add an overall `rationale`, your `reviewer` name, and `reviewed_at` as an
   ISO-8601 timestamp with a timezone, after the trial completed.
4. Set `review_method` to `human` and `status` to `approved`.
5. Save a **JSON list containing only approved records** in a new file such as
   `results/reviews/m3-local-approved-20261026.json`. Omit pending records.

Keep all identity fields and hashes unchanged. Name/date fields record supplied
attribution; they do not authenticate a person's identity. Hashes detect content
mismatches, and do not provide a signature against someone rewriting all inputs.
Automated suggestions are not human reviews. Leave unreviewed outputs unknown.

Validate the approvals without inference:

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
.\.venv-a2\Scripts\python.exe -m workflow.review_quality validate --input results/benchmarks/m3-local-calibration-20261019 results/benchmarks/m3-local-classify-followup-20261019 --reviews results/reviews/m3-local-approved-20261026.json
```

Validation binds the full raw trial, exact output, case, split, step and rubric.
It refuses pending/nonhuman judgments, duplicate or conflicting reviews,
unknown trials, bad timestamps/scores, missing rationales, edited output,
held-out/warm-up/invalid trials, and attempts to replace existing quality scores.
One approved judgment per trial is supported; multi-reviewer adjudication is a
separate task. Multiple approved files may be supplied after `--reviews`.

## Freeze reviewed profiles

Use a new output directory:

```powershell
.\.venv-a2\Scripts\python.exe -m workflow.profile_report --input results/benchmarks/m3-local-calibration-20261019 results/benchmarks/m3-local-classify-followup-20261019 --reviews results/reviews/m3-local-approved-20261026.json --output results/benchmarks/m3-local-reviewed-20261026
```

The new aggregate includes `quality_reviews.json` with the full rubric, approved
judgments, fingerprints and coverage, referenced by the aggregate manifest's
checksum. The profile identity includes the review content, even if two reviews
have identical numerical scores. Reviewed scores remain estimated scheduling
proxies. Profiles without reviews retain unknown quality; using `--reviews`
does not supply a default score. Existing aggregate commands without reviews
retain their original profile identity.

Use `--rubric path/to/rubric.json` consistently at export, validation and freezing
to select another versioned rubric. Different rubric hashes are different
observation scales and cannot be silently pooled.

Reviewed freezes now use the explicit pilot policy in
[`config/quality/pilot-v1.json`](../config/quality/pilot-v1.json): at least ten
reviewed trials from five distinct cases, covering every valid natural
completion in each profile. Scores and reasons stay visible when coverage falls
short, but the estimator returns unknown scheduling quality. Failed attempts
remain in reliability counts and do not count as valid semantic completions.
This pilot coverage rule does not establish population accuracy or stable tails.
Use `--quality-policy path/to/policy.json` to freeze a different explicit policy;
the policy and its eligibility outcomes enter the aggregate/profile identity.
Changing case attribution, review content or thresholds changes that identity.
Old aggregates without a policy keep their historical meaning; live preparation
requires an explicit policy. Freeze a semantic quality floor before evaluation.
Prompt/configuration changes require new compatible trials and profiles. Keep
the final evaluation data isolated from calibration and development feedback.

## Export another packet

```powershell
.\.venv-a2\Scripts\python.exe -m workflow.review_quality export --input path/to/completed-calibration-run --output results/reviews/new-packet
```

Export and profile freezing refuse existing output directories. Incomplete
exports lack a valid manifest. Keep packet files as historical evidence and save
approvals separately. This initial command accepts completed calibration runs;
held-out/final evaluation review has a separate future workflow and cannot train
these profiles.
