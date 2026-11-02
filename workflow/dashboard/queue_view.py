"""Durable ticket submission and progress; inference belongs to the worker CLI."""

from pathlib import Path
import sqlite3

from shared.deployments import load_deployments
from shared.scheduling import SystemClock
from workflow.queue_store import QueueStore
from workflow.dashboard.queue_actions import submit_fixed_ticket, validate_fields
from workflow.queue_store import SubmissionConflict
from uuid import uuid4
from workflow.workload import KnowledgeBase
from workflow.dashboard.services import local_paths, startup_commands


def render_queue_view(root, *, registry_path=None, database_path=None):
    import streamlit as st

    root = Path(root)
    paths = local_paths(root, registry_path=registry_path, database_path=database_path)
    config, database = paths["registry"], paths["database"]
    st.title("Ticket workflows")
    st.caption("Durable execution through a separate worker. Completed steps survive a restart.")
    activity = st.radio("Activity", ("Workflows", "Comparison evidence", "Review inbox"), horizontal=True)
    if activity == "Review inbox":
        from workflow.dashboard.review_inbox import render_review_inbox
        render_review_inbox(root)
        return
    if activity == "Comparison evidence":
        from workflow.dashboard.evidence_view import render_evidence_view
        render_evidence_view(root)
        return
    mode = st.radio("Execution strategy", ("Fixed assignment", "Optimized policy"), horizontal=True)
    policy, weights = "fixed", None
    profiles_path = forecasts_path = carbon_path = None
    offsets, floor = (0,), .8
    if mode == "Optimized policy":
        policy = st.selectbox("Scheduling policy", ("balanced", "speed", "eco", "custom"))
        with st.expander("Planning evidence", expanded=True):
            profiles_path = st.text_input("Reviewed profiles file", value=str(root / "results/benchmarks/m3-local-gated-20261026/profiles.json"))
            forecasts_path = st.text_input("Workload forecasts file (optional)") or None
            carbon_path = st.text_input("Carbon snapshot file (optional)") or None
            offset_text = st.text_input("Offered delays (seconds, comma separated)", value="0,30,60")
            floor = st.number_input("Minimum reviewed step quality", min_value=0., max_value=1., value=.8)
            weight_text = st.text_input("Custom weights (JSON)", value='{"latency":0.2,"accuracy":0.2,"cost":0.2,"energy":0.2,"carbon":0.2}') if policy == "custom" else None
        st.info("Unknown quality, context sizes, carbon or availability can make a plan infeasible. The worker verifies destinations; this page does not contact providers.")
    try:
        registry = load_deployments(config)
        kb = KnowledgeBase.load()
        deployments = tuple(registry)
        deployment_id = st.selectbox("Execution destination", [item.deployment_id for item in deployments], disabled=mode != "Fixed assignment")
        deployment = registry.get(deployment_id)
        model_id = st.selectbox("Model", list(deployment.models), disabled=mode != "Fixed assignment")
        st.caption(f"Region: {deployment.region or 'Unknown'} · Grid: {deployment.grid or 'Unknown'}")
        if mode == "Optimized policy":
            st.caption("The planner considers the full saved registry. The fixed assignment selectors above do not restrict optimized choices.")
        store = QueueStore(database)
    except (ValueError, OSError, sqlite3.Error):
        st.error("The workflow configuration or database is unavailable.")
        return
    try:
        if "queue_submission_token" not in st.session_state:
            st.session_state["queue_submission_token"] = uuid4().hex
        if st.button("Start another ticket"):
            st.session_state["queue_submission_token"] = uuid4().hex
            st.info("A new submission is ready. Queueing the same details again will create a separate workflow.")
        with st.form("durable_ticket_submission", clear_on_submit=False):
            ticket = st.text_area("Support ticket", placeholder="Describe the customer's CarbonDesk issue.")
            left, right = st.columns(2)
            response = left.number_input("Response deadline (seconds)", min_value=1, max_value=3600, value=300)
            completion = right.number_input("Final deadline (seconds)", min_value=1, max_value=7200, value=600)
            delay = st.number_input("Earliest background summary (seconds after submission)",
                                    min_value=0, max_value=7199, value=0)
            submitted = st.form_submit_button("Queue ticket")
        if submitted:
            try:
                issues = validate_fields(ticket, response, completion, delay)
                if issues:
                    for field, message in issues.items():
                        st.error(field + ": " + message)
                    return
                if mode == "Fixed assignment":
                    identity = submit_fixed_ticket(store, registry, kb, ticket, token=st.session_state["queue_submission_token"], deployment_id=deployment_id,
                        model_id=model_id, response_seconds=response, completion_seconds=completion,
                        summary_delay_seconds=delay)
                else:
                    from workflow.dashboard.queue_actions import submit_unverified
                    from workflow.queue_store import _load
                    offsets = tuple(float(value.strip()) for value in offset_text.split(","))
                    weights = _load(weight_text) if weight_text is not None else None
                    with st.spinner("Saving planning inputs…"):
                        identity = submit_unverified(store, registry, kb, ticket, profiles_path=profiles_path,
                            forecasts_path=forecasts_path, carbon_path=carbon_path, policy=policy, token=st.session_state["queue_submission_token"],
                            custom_weights=weights, quality_floor=floor, start_offsets_seconds=offsets,
                            response_seconds=response, completion_seconds=completion, summary_delay_seconds=delay)
                st.session_state["queue_selected_workflow"] = identity
                st.session_state["queue_last_status"] = store.read(identity).status
                # Reveal the saved workflow even when prior filters/pages hid it.
                st.session_state["queue_history_query"] = identity
                st.session_state["queue_history_status"] = "All"
                st.session_state["queue_history_age"] = "All time"
                st.session_state["queue_history_cursors"] = [None]
                st.session_state["queue_history_filter"] = None
                st.success("Ticket queued. A worker will run eligible steps when ready.")
            except SubmissionConflict:
                st.error("These details differ from the saved submission. Choose Start another ticket to queue a separate workflow.")
            except (ValueError, OSError, sqlite3.Error):
                st.error("The ticket could not be saved. Check deadlines and planning evidence/settings. The summary must start before the final deadline.")
    finally:
        store.close()
    commands = startup_commands(root, database)
    st.code(commands["worker"], language=commands["shell"])
    st.caption("Run this worker in a terminal. Refresh below to load progress; this page does not run model calls.")
    refresh = st.checkbox("Refresh active workflow every 5 seconds", value=False)
    active = st.session_state.get("queue_last_status") not in {"completed", "cancelled", "failed", "deadline_missed", "needs-reconciliation"}
    @st.fragment(run_every="5s" if refresh and active else None)
    def progress():
        status = render_progress(database, config)
        if refresh and active and status in {"completed", "cancelled", "failed", "deadline_missed", "needs-reconciliation"}:
            st.rerun()
    progress()


def render_progress(database, config):
    import streamlit as st
    from datetime import timedelta
    try:
        if not Path(database).is_file():
            raise OSError("Queue disappeared")
        store = QueueStore(database)
    except (ValueError, OSError, sqlite3.Error):
        st.error("The queue is unavailable. Refresh after restoring access; no replacement database was created.")
        return None
    try:
        st.button("Refresh progress")
        query = st.text_input("Search ticket or workflow ID", key="queue_history_query")
        status = st.selectbox("History status", ["All", "scheduled", "running", "response_ready", "completed", "infeasible", "planning-failed", "failed", "deadline_missed", "needs-reconciliation", "cancelled"], key="queue_history_status")
        age = st.selectbox("Submitted", ["All time", "Last 24 hours", "Last 7 days"], key="queue_history_age")
        signature = (query, status, age)
        if st.session_state.get("queue_history_filter") != signature:
            st.session_state["queue_history_filter"] = signature
            st.session_state["queue_history_cursors"] = [None]
        cursors = st.session_state.get("queue_history_cursors", [None])
        since = None if age == "All time" else SystemClock().now() - timedelta(days=1 if age == "Last 24 hours" else 7)
        rows = store.workflow_summaries(query=query, status=None if status == "All" else status,
                                       before=cursors[-1], since=since, limit=21)
        left, right = st.columns(2)
        if left.button("Newer tickets", disabled=len(cursors) == 1):
            st.session_state["queue_history_cursors"] = cursors[:-1]
            st.rerun()
        if right.button("Older tickets", disabled=len(rows) <= 20):
            st.session_state["queue_history_cursors"] = [*cursors, rows[19]["cursor"]]
            st.rerun()
        rows = rows[:20]
        ids = [row["workflow_id"] for row in rows]
        labels = {row["workflow_id"]: f"{row['status']} ? {row['completed']}/{row['steps']} ? {row['ticket']} ? {row['workflow_id']}" for row in rows}
        if not ids:
            st.info("No tickets match this page and filters. Clear the search or change filters to find other work.")
            return
        selected = st.session_state.get("queue_selected_workflow")
        identity = st.selectbox("Workflow", ids, index=ids.index(selected) if selected in ids else 0, format_func=labels.get)
        st.session_state["queue_selected_workflow"] = identity
        snapshot = store.read(identity)
        st.session_state["queue_last_status"] = snapshot.status
        workers = store.presence_report(now=SystemClock().now())
        if workers:
            st.dataframe(workers, width="stretch", hide_index=True)
            st.caption("Worker observations are informational. Stale presence never frees an uncertain lease or authorizes a retry.")
        from workflow.dashboard.progress import explain_progress
        message = explain_progress(snapshot, SystemClock().now())
        if workers:
            message = message.replace("Worker presence is unknown.", "See the worker observations above; presence does not guarantee eligibility.")
        st.info(message)
        st.caption(f"Evidence mode: {snapshot.configuration.get('experiment_mode', 'live')} · Policy: {snapshot.request.policy}")
        if snapshot.configuration.get("experiment_mode") == "simulation":
            st.info("Simulation workflow. Scripted observations do not establish live quality or savings.")
        if st.button("Cancel remaining work", disabled=snapshot.status in {"completed", "cancelled"}):
            store.cancel(identity, now=SystemClock().now())
            snapshot = store.read(identity)
            st.success("Cancellation saved. An in-flight request may still finish; its usage will be retained.")
        a, b, c = st.columns(3)
        a.metric("Status", snapshot.status)
        b.metric("Completed steps", f"{len(snapshot.completed_outputs())}/{len(snapshot.request.steps)}")
        c.metric("Recorded attempts", len(snapshot.attempts))
        st.caption(f"Response deadline: {snapshot.request.response_deadline.isoformat()} · "
                   f"Final deadline: {snapshot.request.completion_deadline.isoformat()}")
        attempts = {(attempt.planned_candidate.step_id, attempt.retry_index): attempt for attempt in snapshot.attempts}
        destinations = load_deployments(config)
        if "registry_snapshot" in snapshot.configuration:
            from shared.deployments import DeploymentRegistry
            destinations = DeploymentRegistry.from_dict(snapshot.configuration["registry_snapshot"])
        rows = []
        for job in snapshot.jobs:
            attempt = attempts.get((job.step_request.step_id, job.retry_index))
            rows.append({"Step": job.step_request.step_id, "Status": job.status,
                "Planned model": job.candidate.model_id, "Executed model": attempt.executed_model_id if attempt else None,
                "Planned destination": job.candidate.deployment_id,
                "Executed destination": attempt.executed_deployment_id if attempt else None,
                "Region": destinations.get(job.candidate.deployment_id).region or "Unknown",
                "Planned start (UTC)": job.candidate.planned_start.isoformat(),
                "Actual start (UTC)": attempt.actual_start.isoformat() if attempt and attempt.actual_start else None,
                "Finished (UTC)": job.finished_at.isoformat() if job.finished_at else None,
                "Issue": job.error_code})
        st.dataframe(rows, width="stretch", hide_index=True)
        from workflow.accounting_report import execution_report
        accounting = execution_report(snapshot)
        st.subheader("Executed resources and coverage")
        st.dataframe([{"Metric": name, "Total": value["total"], "Unit": value["unit"],
            "Known attempts": value["known_attempts"], "Unknown attempts": value["unknown_attempts"],
            "Complete": value["complete"], "Known subtotals": str(value["known_subtotals"]),
            "Provenance": str(value["provenance_counts"])} for name, value in accounting["resources"].items()],
            width="stretch", hide_index=True)
        st.caption("Blank totals are unknown or incomplete. API fees exclude electricity/ownership. Format checks do not establish reviewed semantic quality.")
        with st.expander("Timing, measurement scope and missing evidence"):
            st.json({key: accounting[key] for key in ("timing", "coverage", "quality", "notes")})
        if snapshot.planning_context is not None:
            st.subheader("Planning decisions")
            latest = snapshot.decisions[-1]["report"]
            from workflow.dashboard.explanations import render_planning
            render_planning(latest)
            st.write(f"Policy: {snapshot.request.policy} · Revisions: {len(snapshot.decisions)}")
            if snapshot.status in {"infeasible", "planning-failed"}:
                st.warning("No eligible plan is installed. Inspect the rejection reasons and missing inputs below; a worker will recheck eligibility.")
            diagnostics = {key: latest.get(key) for key in ("trigger", "error_code", "weights", "quality_floor", "mode_exclusions")}
            diagnostics["rejection_counts"] = latest.get("result", {}).get("rejection_counts", {})
            diagnostics["unavailable_contexts"] = [item["step_id"] for item in latest.get("contexts", []) if item["input_mode"] == "unavailable"]
            st.json(diagnostics)
            with st.expander("Decision history, candidates and score contributions"):
                st.json(list(snapshot.decisions))
        outputs = snapshot.completed_outputs()
        if "draft" in outputs:
            st.subheader("Customer response")
            st.text(outputs["draft"])
        if "summarize" in outputs:
            st.subheader("Background summary")
            st.text(outputs["summarize"])
        with st.expander("Input, output and attempt evidence"):
            st.text(snapshot.request.input_text)
            for job in snapshot.jobs:
                st.write(job.step_request.step_id)
                st.text(job.output or "No output retained")
            st.json([attempt.to_dict() for attempt in snapshot.attempts])
        return snapshot.status
    except (ValueError, OSError, sqlite3.Error):
        st.error("Saved workflow data could not be loaded. Its records have been preserved.")
    finally:
        store.close()
