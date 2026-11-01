# End-to-end workflow quality review

New evaluation protocols freeze an `output_quality` rubric and policy before
execution. The existing four-step semantic rubric is retained in full, together
with explicit thresholds. This review is separate from calibration quality and
never retrains profiles or changes raw provider attempts.

After trials finish, export the actual evidence and blank templates:

```powershell
python -m workflow.review_workflow_quality export --evaluation .runtime/my-evaluation --output results/reviews/my-workflow-packet
```

Each reviewable trial needs four accepted natural completions. The packet
includes the exact protocol, ticket/KB, rendered prompts, outputs and all retained
workflow evidence. Incomplete, failed, infeasible or unverified natural
completions remain explicit exclusions. A prepared evaluation has no reviewable
outputs. Export never fills a score, reviewer or approval.

A human reads that evidence and saves a separate JSON list containing only
approved reviews. For every criterion, supply a numeric score from zero to one
and a nonempty rationale. Each step score is its arithmetic criterion mean;
the workflow score is the minimum of the four step means. The default policy
requires workflow score at least 0.8 and every criterion at least 0.5. This
prevents a strong step from hiding a poor step; thresholds are frozen inputs,
not inferred population guarantees.

Set `status` to `approved`, `review_method` to `human-rubric`, and supply the
reviewer's attribution, an aware `reviewed_at` timestamp and overall rationale.
The review must follow all retained completion/accounting facts and precede
validation. Do not edit identity hashes or journal evidence. Hashes bind exact
content; reviewer names and human-method assertions are not identity signatures.

```powershell
python -m workflow.review_workflow_quality validate --evaluation .runtime/my-evaluation --reviews path/to/approved.json
python -m workflow.review_workflow_quality import --evaluation .runtime/my-evaluation --reviews path/to/approved.json
python -m workflow.evaluate_policies report --evaluation .runtime/my-evaluation
```

Imports validate the full protocol, workflow, output and rubric bindings, score
arithmetic and chronology. They are additive: identical approvals are idempotent,
conflicting replacements and duplicate input IDs fail. Previous review-set
revisions are preserved under `quality-review-history/`; the current immutable
judgments are indexed in `quality_reviews.json`. Execution records and planning
proxies stay unchanged. Import shares the evaluation's OS lock with its runner.

Reports show reviewed/unknown/failed coverage across **all scheduled trials**,
descriptive scores and paired absolute quality differences. Partial review cannot
produce a complete arm quality value. Failed judgments remain visible. Simulation
stays simulation even when synthetic review fixtures exercise this contract.
Release eligibility is not automatically granted by filling reviews; final live
measurements, resource coverage and other release checks remain required.

Comparison exports include the approved review set in their checksum manifest
and revalidate it against retained workflows during offline loading. Portable
packages cannot be changed by review import: import into the original evaluation
and export to a new directory. Older protocols/packages preserve their original
unreviewed meaning. A protocol without a frozen review policy must be replaced
by a newly frozen experiment rather than retroactively asserting preregistration.

All packet, validation, import and comparison operations are offline. Actual
human judgments are still required; tests use explicitly synthetic judgments
and do not establish real workflow quality.
