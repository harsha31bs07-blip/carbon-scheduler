"""Offline policy-comparison and output-review inspection."""

import json
from pathlib import Path


def show_review_notice():
    import streamlit as st
    message = st.session_state.pop("review_saved_notice", None)
    if message:
        st.success(message)


def load_evidence(state, path, kind):
    """Cache only a fully validated report; discard stale content after failure."""
    from workflow.policy_comparison import load_comparison, load_evaluation
    state.pop("validated_comparison", None)
    state.pop("validated_comparison_stamp", None)
    if kind not in {"Portable comparison", "Evaluation directory"}:
        raise ValueError("Unsupported evidence format")
    from workflow.dashboard.evidence_cache import evidence_stamp
    from shared.scheduling import SystemClock
    before = evidence_stamp(path)
    report = load_comparison(path) if kind == "Portable comparison" else load_evaluation(path)
    if before != evidence_stamp(path):
        raise ValueError("Evidence changed during validation")
    state["validated_comparison"] = (str(path), kind, report)
    state["validated_comparison_stamp"] = before
    state["validated_comparison_at"] = SystemClock().now().isoformat()
    return report


def render_review_form(directory, protocol, wrapped, *, portable, already_reviewed):
    import streamlit as st
    show_review_notice()
    from shared.scheduling import SystemClock
    import math
    from workflow.workflow_quality import review_template
    from workflow.review_workflow_quality import import_reviews
    if "output_quality" not in protocol:
        st.info("This older protocol has no frozen output-review policy. Its quality remains unreviewed.")
        return
    if portable or already_reviewed:
        st.info("An approved judgment is retained and cannot be overwritten." if already_reviewed
                else "Review import is available in the original evaluation directory. Portable evidence is immutable.")
        return
    try:
        review = review_template(protocol, wrapped["entry"], wrapped["snapshot"])
    except ValueError:
        st.info("This trial lacks the accepted natural completions required for end-to-end review.")
        return
    st.subheader("Human output review")
    st.caption("Read the ticket, KB, prompts and outputs above. Every score and rationale must reflect your own judgment. Blank fields do not supply quality.")
    with st.expander("Frozen knowledge base and review policy"):
        st.json({"knowledge_base": protocol["knowledge_base"], "output_quality": protocol["output_quality"]})
    identity = wrapped["entry"]["trial_id"]
    from copy import deepcopy
    from workflow.dashboard.draft_controls import load_draft, save_draft
    template = deepcopy(review)
    reviewer = st.text_input("Reviewer attribution", key=identity + "-reviewer")
    allowed = {identity + "-overall"}
    for step, criteria in protocol["output_quality"]["rubric"]["steps"].items():
        for criterion in criteria:
            key = identity + "-" + step + "-" + criterion["id"]
            allowed.update((key + "-score", key + "-reason"))
    load_draft("workflow", template, reviewer, allowed)
    with st.form("review-" + identity):
        scores = []
        for step, criteria in protocol["output_quality"]["rubric"]["steps"].items():
            with st.expander("Review criteria: " + step):
                step_scores = []
                for criterion in criteria:
                    key = identity + "-" + step + "-" + criterion["id"]
                    value = st.number_input(step + ": " + criterion["id"] + " score", min_value=0., max_value=1.,
                        value=None, step=.1, help=criterion["description"], key=key + "-score")
                    rationale = st.text_area(step + ": " + criterion["id"] + " rationale", key=key + "-reason")
                    review["steps"][step]["criteria"][criterion["id"]] = {"score": value, "rationale": rationale}
                    step_scores.append(value)
                mean = math.fsum(step_scores) / len(step_scores) if all(value is not None for value in step_scores) else None
                review["steps"][step]["score"] = mean
                scores.append(mean)
        rationale = st.text_area("Overall review rationale", key=identity + "-overall")
        save = st.form_submit_button("Approve and save human review")
        draft = st.form_submit_button("Save review draft")
    if draft:
        save_draft("workflow", template, reviewer, {key: st.session_state.get(key) for key in allowed})
    if save:
        try:
            review.update(status="approved", reviewer=reviewer, reviewed_at=SystemClock().now().isoformat(),
                rationale=rationale, score=min(scores) if all(value is not None for value in scores) else None)
            import_reviews(directory, [review], as_of=review["reviewed_at"])
            load_evidence(st.session_state, directory, "Evaluation directory")
            st.session_state["review_saved_notice"] = "Human approval saved. Review coverage has been refreshed."
            st.rerun()
        except (ValueError, OSError, TypeError, KeyError):
            st.error("Review was not saved. Fill every score, criterion rationale, reviewer attribution and overall rationale. Existing approvals cannot be replaced.")


def render_report(report):
    import streamlit as st
    st.caption(f"Protocol: {report['protocol_id']} · Mode: {report['experiment_mode']} · Dataset: {report['dataset_split']}")
    if report["experiment_mode"] == "simulation":
        st.info("Simulation evidence. These observations do not establish live quality or savings.")
    st.write(f"Finished trials: {report['finished_trials']} / {report['scheduled_trials']}")
    st.warning("Release checks remain open: " + ", ".join(report["release_evidence"]["reasons"]))
    rows = []
    for arm in report["arms"]:
        row = {"Strategy": arm["arm_id"], "Outcomes": json.dumps(arm["status_counts"]),
               "Response deadline met": arm["response_deadline_met"],
               "Final deadline met": arm["completion_deadline_met"],
               "Quality": arm["quality"]["value"], "Reviewed trials": arm["quality"]["reviewed_trials"],
               "Unknown quality trials": arm["quality"]["unknown_trials"]}
        for name in ("cost", "energy", "carbon"):
            resource = arm["resources"][name]
            row[name.title()] = resource["total"]
            row[name.title() + " unit"] = resource["unit"]
            row[name.title() + " coverage"] = f"{resource['known_trials']}/{arm['scheduled_trials']}"
        rows.append(row)
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("Unknown values stay blank. Partial subtotals and provenance are retained in the detailed report. CO2 and CO2e are separate.")
    if report["paired_comparisons"]:
        choice = st.selectbox("Paired strategy", [item["treatment_arm_id"] for item in report["paired_comparisons"]])
        paired = next(item for item in report["paired_comparisons"] if item["treatment_arm_id"] == choice)
        st.caption("Absolute differences are treatment minus baseline. Percentage reductions require a positive known baseline.")
        from workflow.dashboard.explanations import paired_rows
        st.dataframe(paired_rows(paired), width="stretch", hide_index=True)
        chartable = [name for name, value in paired["metrics"].items()
                     if value["absolute_delta"]["mean"] is not None]
        if chartable:
            metric = st.selectbox("Chart paired metric", chartable)
            values = [item["metrics"][metric]["absolute_delta"] for item in paired["pairs"]
                      if item["metrics"][metric]["absolute_delta"] is not None]
            st.bar_chart({"Matched pair": [str(index + 1) for index in range(len(values))],
                          "Difference": values}, x="Matched pair", y="Difference")
            st.caption("Unit: " + ", ".join(paired["metrics"][metric]["units"]) +
                       ". Each bar is one retained matched pair; failures and missing measurements remain in coverage counts above.")
        with st.expander("Individual paired differences and detailed statistics"):
            st.json(paired)
    with st.expander("Full validated comparison and review coverage"):
        st.json(report)
    st.download_button("Download validated report", json.dumps(report, indent=2, allow_nan=False),
                       file_name="policy-comparison.json", mime="application/json")


def render_evidence_view(root):
    import streamlit as st
    show_review_notice()
    from workflow.dashboard.services import local_paths
    st.subheader("Comparison evidence")
    kind = st.radio("Evidence format", ("Evaluation directory", "Portable comparison"))
    path = st.text_input("Evidence directory", placeholder="Path to an evaluation or exported comparison")
    if st.button("Load bundled offline demonstration"):
        try:
            with st.spinner("Validating the demonstration…"):
                load_evidence(st.session_state, local_paths(root)["demo"], "Portable comparison")
        except (ValueError, OSError, KeyError):
            st.error("The bundled simulation package could not be validated.")
    if st.button("Load evidence"):
        try:
            with st.spinner("Validating retained evidence…"):
                load_evidence(st.session_state, path, kind)
        except (ValueError, OSError, KeyError):
            st.session_state.pop("validated_comparison", None)
            st.error("Evidence could not be validated. Select the complete original evaluation or portable package.")
    from workflow.dashboard.evidence_cache import retained_evidence
    try:
        retained = retained_evidence(st.session_state)
    except ValueError:
        st.error("Evidence changed after loading. Load and validate it again before inspecting or reviewing outputs.")
        return
    if retained is None:
        st.info("Load stored evidence to inspect outcomes, quality coverage and paired comparisons. This page performs no inference.")
        return
    loaded_path, loaded_kind, report = retained
    st.caption("Loaded evidence: " + loaded_path)
    st.caption("Validated snapshot: " + st.session_state.get("validated_comparison_at", "unknown") +
               " · Content fingerprint: " + st.session_state["validated_comparison_stamp"])
    render_report(report)
    finished = [row for row in report["trials"] if row["state"] == "finished"]
    if finished:
        selected = st.selectbox("Trial outputs", [row["trial_id"] for row in finished])
        trial = next(row for row in finished if row["trial_id"] == selected)
        try:
            from workflow.review_workflow_quality import review_inputs
            protocol, trials = review_inputs(loaded_path)
            if protocol["fingerprint"] != report["protocol_fingerprint"]:
                raise ValueError("Loaded protocol changed")
            wrapped = next(item for item in trials if item["entry"]["trial_id"] == selected)
            if wrapped["entry"] != {key: trial[key] for key in wrapped["entry"]}:
                raise ValueError("Loaded trial changed")
            snapshot = wrapped["snapshot"]
            st.text(snapshot.request.input_text)
            for job in snapshot.jobs:
                with st.expander("Output: " + job.step_request.step_id):
                    st.text(job.output or "No accepted output")
                    if job.rendered_step:
                        st.text(job.rendered_step.prompt)
            st.json(trial.get("output_quality", {"value": None, "provenance": "unknown"}))
            render_review_form(loaded_path, protocol, wrapped, portable=loaded_kind == "Portable comparison",
                already_reviewed=trial.get("output_quality", {}).get("value") is not None)
        except (ValueError, OSError, KeyError, StopIteration):
            st.error("The selected output evidence changed or is unavailable. Load and validate the comparison again.")
    st.caption("Human review: export a blank packet, supply attributable approvals, then import them into the original evaluation. Portable packages remain immutable.")
