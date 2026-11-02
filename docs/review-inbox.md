# Human review inbox and drafts

Open **Ticket workflows → Review inbox**. Calibration and workflow judgments have
separate bindings, eligibility and approval destinations. No reviewer is assigned
automatically. A typed name is asserted attribution, not an authenticated signature.
Scores and rationales must reflect judgments the reviewer personally makes.

**Calibration** validates original journals and their retained cases/KB context.
It shows pending/approved/ineligible counts, the actual ticket/prompt/output and
rubric criteria. Warmups, failed/truncated outputs and held-out data cannot become
calibration judgments. Scores start blank. The existing `apply_reviews` contract
validates approvals, which are additive and cannot be overwritten.

**Download approved calibration reviews** produces the JSON list accepted by
`workflow.profile_report`. Freeze new profiles with those approvals; the inbox
does not overwrite journals or existing profile files. Quality remains unknown
until genuine judgments satisfy the applicable coverage policy.

**Workflow outputs** loads an original evaluation with a frozen review policy.
Pending navigation requires accepted natural completions of all four steps.
Read actual tickets, outputs, prompts, KB and policy before judging. Approval uses
the existing strict importer and refreshes coverage/pending navigation. Portable
comparisons remain immutable.

Explicit **Save draft** and **Load my saved draft** controls use local storage at
`.runtime/review-drafts.sqlite3`, outside raw/evaluation evidence. Drafts bind to
review kind, reviewer and exact evidence-template fingerprint. Changed evidence
does not inherit old drafts; changing attribution clears another reviewer's widget
values. Incomplete drafts never supply approved quality. They survive navigation
and restart; logout clears browser fields while saved local drafts remain.
This local design is not a multi-user authorization boundary.

Calibration approvals occupy a separate local table; workflow approvals remain in
the original evaluation's immutable review history. Draft saving calls neither
approval importer. Practice on simulation retains simulation labels and cannot
establish live quality or carbon savings. No real human reviews were generated
while implementing these tools.
