"""Set-based history SQL with bound operator values."""


def history_query(sql: str, planning_available: bool) -> str:
    planning_join = ("LEFT JOIN planning_decisions p ON p.workflow_id=b.id AND p.revision="
        "(SELECT max(revision) FROM planning_decisions WHERE workflow_id=b.id)" if planning_available else "")
    report = "p.report" if planning_available else "NULL"
    # Aggregate statuses in SQLite before filtering/limiting. Only the final
    # page crosses the Python boundary; rare filters cause no per-run queries.
    return """WITH base AS (""" + sql + """), summaries AS (
        SELECT b.*,count(j.step_id) AS steps,
            sum(j.status='completed') AS completed,
            CASE WHEN b.cancelled_at IS NOT NULL THEN 'cancelled'
            WHEN sum(j.status='needs-reconciliation')>0 THEN 'needs-reconciliation'
            WHEN sum(j.status='deadline_missed')>0 THEN 'deadline_missed'
            WHEN sum(j.status IN ('failed','cancelled'))>0 THEN 'failed'
            WHEN count(j.step_id)>0 AND sum(j.status='completed')=count(j.step_id) THEN 'completed'
            WHEN sum(j.status='running')>0 THEN 'running'
            ELSE CASE WHEN """ + report + """ IS NOT NULL AND json_extract(""" + report + """,'$.result.winner') IS NULL
                THEN CASE WHEN json_extract(""" + report + """,'$.record_type')='PlanningPreparationFailure'
                    THEN 'planning-failed' ELSE 'infeasible' END
                WHEN NOT EXISTS (SELECT 1 FROM json_each(b.request,'$.steps') s
                    LEFT JOIN jobs r ON r.workflow_id=b.id AND r.step_id=json_extract(s.value,'$.step_id')
                    WHERE json_extract(s.value,'$.required_for_response')=1 AND r.status IS NOT 'completed')
                    THEN 'response_ready' ELSE 'scheduled' END END AS status,
            (SELECT error_code FROM jobs WHERE workflow_id=b.id AND error_code IS NOT NULL
                AND error_code!='' ORDER BY rowid LIMIT 1) AS issue
        FROM base b LEFT JOIN jobs j ON j.workflow_id=b.id """ + planning_join + """
        GROUP BY b.id)
        SELECT * FROM summaries WHERE (? IS NULL OR status=?) ORDER BY cursor DESC LIMIT ?"""
