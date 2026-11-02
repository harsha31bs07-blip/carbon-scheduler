"""Human review navigation for calibration and completed workflows."""

from copy import deepcopy
import json
import math
import sqlite3
from pathlib import Path


def render_review_inbox(root):
    import streamlit as st
    from workflow.dashboard.evidence_view import show_review_notice
    show_review_notice()
    st.subheader("Review inbox")
    st.caption("Reviewer names are asserted attribution, not authenticated signatures. Only enter judgments you personally make.")
    kind = st.radio("Review kind", ("Calibration", "Workflow outputs"), horizontal=True)
    try:
        if kind == "Calibration":
            render_calibration(root)
        else:
            render_workflows(root)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
        st.error("Review evidence could not be validated. Check the original complete calibration or evaluation directory.")


def render_workflows(root):
    import streamlit as st
    from workflow.dashboard.evidence_view import load_evidence, render_review_form
    from workflow.dashboard.evidence_cache import retained_evidence
    from workflow.review_workflow_quality import review_inputs
    from workflow.workflow_quality import review_template
    directory = st.text_input("Original evaluation directory")
    if st.button("Load workflow review inbox"):
        load_evidence(st.session_state, directory, "Evaluation directory")
    retained = retained_evidence(st.session_state)
    if retained is None or retained[1] != "Evaluation directory":
        st.info("Load an original evaluation. Portable comparisons remain immutable.")
        return
    directory, _, report = retained
    protocol, trials = review_inputs(directory)
    quality = {row["trial_id"]: row.get("output_quality", {}).get("value") for row in report["trials"]}
    pending, excluded, approved = [], 0, 0
    for wrapped in trials:
        try:
            review_template(protocol, wrapped["entry"], wrapped["snapshot"])
            if quality.get(wrapped["entry"]["trial_id"]) is not None:
                approved += 1
            else:
                pending.append(wrapped)
        except (ValueError, TypeError, AttributeError):
            excluded += 1
    st.write(f"Pending: {len(pending)} · Approved: {approved} · Ineligible: {excluded}")
    st.caption("Ineligible trials lack a frozen review policy or accepted natural completion of every step.")
    if not pending:
        return
    selected = st.selectbox("Next workflow to review", [item["entry"]["trial_id"] for item in pending])
    wrapped = next(item for item in pending if item["entry"]["trial_id"] == selected)
    st.text(wrapped["snapshot"].request.input_text)
    for job in wrapped["snapshot"].jobs:
        with st.expander("Evidence: " + job.step_request.step_id, expanded=True):
            st.text(job.output or "No output")
            if job.rendered_step:
                st.text(job.rendered_step.prompt)
    st.info("Evidence mode: " + report["experiment_mode"] + ". Simulation reviews cannot establish live quality.")
    render_review_form(directory, protocol, wrapped, portable=False, already_reviewed=False)


def render_calibration(root):
    import streamlit as st
    from data.quality_reviews import apply_reviews, load_rubric, review_template
    from workflow.profile_report import load_calibration_inputs
    from workflow.review_quality import _recorded_context
    from workflow.calibration_approvals import load_approvals, save_approval
    from workflow.dashboard.draft_controls import draft_path, load_draft, save_draft
    from shared.scheduling import SystemClock
    defaults = [Path(root) / "results/benchmarks" / name for name in
                ("m3-local-calibration-20261019", "m3-local-classify-followup-20261019")]
    directories_text = st.text_area("Calibration directories (one per line)", value="\n".join(map(str, defaults)))
    directories = [Path(line.strip()) for line in directories_text.splitlines() if line.strip()]
    if not directories:
        return
    trials, _ = load_calibration_inputs(directories)
    contexts = {str(directory): _recorded_context(directory) for directory in directories}
    rubric = load_rubric()
    database = draft_path(st.session_state)
    approvals = load_approvals(database, trials, rubric)
    approved = {item["trial_id"] for item in approvals}
    _, coverage = apply_reviews(trials, approvals, rubric=rubric)
    pending, excluded = [], 0
    for trial in trials:
        try:
            review_template(trial, rubric)
            if trial["trial_id"] not in approved:
                pending.append(trial)
        except ValueError:
            excluded += 1
    st.write(f"Pending: {len(pending)} · Approved: {len(approved)} · Ineligible: {excluded}")
    st.caption("Warmups, failed/truncated outputs and held-out data do not become calibration judgments.")
    with st.expander("Review coverage and frozen context"):
        st.json({"coverage": coverage, "rubric": rubric, "recorded_contexts": contexts})
    from data.profiles import build_profiles
    from data.quality_policy import validate_quality_policy
    from data.quality_reviews import load_json
    policy_path = st.text_input("Coverage policy file", value=str(Path(root) / "config/quality/pilot-v1.json"))
    policy = validate_quality_policy(load_json(policy_path))
    enriched, _ = apply_reviews(trials, approvals, rubric=rubric)
    progress = build_profiles(enriched, calibration_id="review-inbox-progress", quality_policy=policy)
    st.dataframe([{"Model": profile["model_id"], "Step": profile["step_id"],
        "Reviewed trials": profile["metrics"]["accuracy"]["count"],
        "Coverage": str(profile["metrics"]["accuracy"]["coverage"])} for profile in progress["profiles"]],
        width="stretch", hide_index=True)
    st.caption("Coverage uses the selected policy. Review progress does not replace freezing new reviewed profiles or request-specific planning checks.")
    if approvals:
        st.download_button("Download approved calibration reviews", json.dumps(approvals, indent=2, allow_nan=False),
                           file_name="calibration-reviews.approved.json", mime="application/json")
        st.caption("Use the downloaded review list with workflow.profile_report to freeze reviewed profiles. Raw journals and existing profile files are unchanged.")
    if not pending:
        return
    selected = st.selectbox("Next calibration output to review", [trial["trial_id"] for trial in pending])
    trial = next(trial for trial in pending if trial["trial_id"] == selected)
    st.text("Step: " + trial["step_id"] + " · Case: " + trial["case_id"])
    case = next((case for context in contexts.values() for case in context.get("cases", {}).get("tickets", [])
                 if case.get("id") == trial["case_id"]), None)
    if case:
        st.text(case["ticket"])
    st.text(trial.get("prompt", "No retained prompt"))
    st.text(trial["result"]["text"])
    with st.expander("Usage and raw evidence"):
        st.json(trial)
    review = review_template(trial, rubric)
    template = deepcopy(review)
    identity = "cal-" + selected
    reviewer = st.text_input("Reviewer attribution", key=identity + "-reviewer")
    keys = {identity + "-overall"}
    for criterion in rubric["steps"][trial["step_id"]]:
        keys.update((identity + "-" + criterion["id"] + "-score", identity + "-" + criterion["id"] + "-reason"))
    load_draft("calibration", template, reviewer, keys)
    with st.form(identity):
        for criterion in rubric["steps"][trial["step_id"]]:
            key = identity + "-" + criterion["id"]
            score = st.number_input(criterion["id"] + " score", min_value=0., max_value=1., value=None,
                                    help=criterion["description"], key=key + "-score")
            reason = st.text_area(criterion["id"] + " rationale", key=key + "-reason")
            review["criteria"][criterion["id"]] = {"score": score, "rationale": reason}
        rationale = st.text_area("Overall rationale", key=identity + "-overall")
        approve = st.form_submit_button("Approve calibration judgment")
        draft = st.form_submit_button("Save calibration draft")
    if draft:
        save_draft("calibration", template, reviewer, {key: st.session_state.get(key) for key in keys})
    if approve:
        scores = [value["score"] for value in review["criteria"].values()]
        review.update(status="approved", reviewer=reviewer, review_method="human", rationale=rationale,
                      reviewed_at=SystemClock().now().isoformat(),
                      score=math.fsum(scores) / len(scores) if all(value is not None for value in scores) else None)
        try:
            save_approval(database, trials, rubric, review)
            st.session_state["review_saved_notice"] = "Calibration judgment saved. Pending counts refreshed; download approvals to freeze profiles."
            st.rerun()
        except ValueError:
            st.error("Approval was not saved. Fill all scores, rationales and attribution. Existing judgments cannot be replaced.")
