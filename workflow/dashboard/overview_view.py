"""Guided local setup with explicit unknown evidence states."""

import json

from workflow.dashboard.services import local_paths
from workflow.readiness import inspect_readiness


def render_overview(root):
    import streamlit as st

    st.title("Overview and setup")
    st.write("Start with the offline demonstration, or inspect configuration before queueing a live ticket.")
    paths = local_paths(root)
    if st.button("Open offline demonstration"):
        from workflow.dashboard.evidence_view import load_evidence
        try:
            with st.spinner("Validating the demonstration…"):
                load_evidence(st.session_state, paths["demo"], "Portable comparison")
            st.session_state["overview_show_demo"] = True
        except (ValueError, OSError, KeyError):
            st.error("The bundled demonstration is missing or invalid. Restore the complete package; no inference was attempted.")
    if st.session_state.get("overview_show_demo"):
        from workflow.dashboard.evidence_view import render_report
        retained = st.session_state.get("validated_comparison")
        if retained:
            render_report(retained[2])
            st.caption("Inspect individual outputs in Ticket workflows → Comparison evidence. The loaded package is retained for this session.")
        if st.button("Close demonstration"):
            st.session_state.pop("overview_show_demo", None)
            st.rerun()
    with st.expander("Advanced diagnostic inputs"):
        profiles = st.text_input("Profiles to inspect", value=str(paths["profiles"]))
        forecasts = st.text_input("Forecasts to inspect (optional)") or None
        carbon = st.text_input("Carbon snapshot to inspect (optional)") or None
    report = inspect_readiness(root, profiles_path=profiles, forecasts_path=forecasts, carbon_path=carbon)
    st.subheader("Local readiness")
    st.dataframe(report["checks"], width="stretch", hide_index=True)
    st.caption("File validity does not approve dispatch. The planner checks exact model/settings, reviewed quality, context, deadlines, availability and carbon coverage for each request.")
    st.subheader("Configured destinations")
    if report["destinations"]:
        st.dataframe([{**item, "models": json.dumps(item["models"]),
                       "observed_models": ", ".join(item["observed_models"])}
                      for item in report["destinations"]], width="stretch", hide_index=True)
    st.info("Endpoint availability and worker presence are unknown until observed. This overview performs local checks only.")
    st.subheader("Start a local session")
    st.caption("Run the worker in a separate terminal. This command exits after 90 idle seconds; restart it for later work. It can execute eligible live requests already in the queue.")
    st.code(report["commands"]["worker"], language=report["commands"]["shell"])
    with st.expander("Dashboard startup and diagnostics"):
        st.code(report["commands"]["dashboard"], language=report["commands"]["shell"])
        st.code("python -m workflow.readiness\npython -m workflow.readiness --probe", language="bash")
        st.caption("The first command inspects local files. --probe explicitly contacts configured providers for bounded read-only inventory, without generation.")
    st.warning("Full A2 validation still needs real human judgments, a second confirmed destination and controlled live measurements. The demonstration is simulated.")
