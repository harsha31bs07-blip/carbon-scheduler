"""Presentation of retained decisions; never changes planner results."""

REASONS = {
    "unknown-accuracy": ("Semantic quality is unreviewed or lacks coverage", "Obtain genuine rubric reviews and freeze profiles with sufficient coverage."),
    "unknown-latency": ("Latency is unsupported for this context", "Calibrate the actual prompt-size range and provide a compatible workload forecast."),
    "unknown-carbon": ("Carbon cannot be attributed", "Supply a fresh grid-specific snapshot covering the full predicted execution interval."),
    "unknown-energy": ("Energy evidence is missing", "Collect deployment-specific energy observations without substituting zero for missing values."),
    "unknown-cost": ("API fees are unknown", "Configure an exact model/deployment tariff with valid usage coverage."),
    "deployment-unavailable-or-unknown": ("Destination availability is unverified or unavailable", "Run explicit read-only inventory and let the worker recheck the exact model before dispatch."),
    "empty-step-pool": ("A step has no eligible choices", "Resolve the other missing evidence and constraint failures for that step."),
    "unknown-grid": ("The deployment grid is unknown", "Provide confirmed physical location/grid evidence."),
    "pair-not-permitted": ("An experiment restricts this model/destination pair", "Check the frozen arm restrictions; do not alter a running experiment."),
    "planning-input-unavailable": ("Planning inputs could not be validated", "Check the retained configuration, profiles, KB and forecast bindings."),
}


def planning_rows(report):
    reasons = dict(report.get("result", {}).get("rejection_counts", {}))
    if report.get("error_code"):
        reasons.setdefault(report["error_code"], 1)
    for context in report.get("contexts", []):
        if context.get("input_mode") == "unavailable":
            reasons.setdefault("unknown-latency", 0)
    return [{"Reason code": code, "Count": count,
             "Explanation": REASONS.get(code, (code.replace("-", " "), "Inspect the retained constraint and candidate details."))[0],
             "Next action": REASONS.get(code, ("", "Inspect the retained constraint and candidate details."))[1]}
            for code, count in sorted(reasons.items())]


def render_planning(report):
    import streamlit as st
    result = report.get("result", {})
    st.caption(f"Search examined {result.get('examined_count', 0)} / {result.get('total_combinations', 0)} combinations; {result.get('feasible_count', 0)} feasible choices found.")
    if result.get("truncated"):
        st.warning("The search limit was reached. Unexamined choices may be feasible or better; this is not an exhaustive infeasibility or optimality result.")
    rows = planning_rows(report)
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    winner = result.get("winner")
    if winner:
        st.dataframe([{"Step": row["candidate"]["step_id"], "Model": row["candidate"]["model_id"],
            "Destination": row["candidate"]["deployment_id"], "Start": row["start"], "Finish": row["finish"]}
            for row in winner["assignments"]], width="stretch", hide_index=True)
        st.dataframe([{"Metric": name, "Normalized value": winner["normalized_metrics"][name],
                       "Weighted contribution": value} for name, value in winner["weighted_contributions"].items()],
                      width="stretch", hide_index=True)
        st.caption("These are retained planning estimates and score contributions. Actual execution and resource coverage are reported separately.")


def paired_rows(comparison):
    return [{"Metric": name, "Unit": ", ".join(value["units"]),
        "Matched pairs": value["matched_pairs"], "Unmatched pairs": value["unmatched_pairs"],
        "Mean difference": value["absolute_delta"]["mean"],
        "Mean reduction (%)": value["reduction_percent"]["mean"],
        "Sample SD": value["absolute_delta"]["sample_standard_deviation"],
        "Missing/incompatible reasons": str(value["reason_counts"]), "Aggregation issue": value["aggregate_reason"]}
        for name, value in comparison["metrics"].items()]
