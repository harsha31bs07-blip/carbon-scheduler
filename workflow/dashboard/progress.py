"""Plain-language interpretation of existing durable workflow states."""

from datetime import datetime
from workflow.queue_store import WorkflowSnapshot


def explain_progress(snapshot: WorkflowSnapshot, now: datetime) -> str:
    messages = {
        "completed": "All requested steps are complete. Retained outputs and measurement coverage are below.",
        "cancelled": "Remaining work was cancelled. A request already sent to a provider may still return; incurred usage is retained.",
        "failed": "A step failed. Inspect its issue and attempts before using an explicit retry command.",
        "deadline_missed": "A saved deadline was missed. Existing outputs remain available; missed deadlines are not silently extended.",
        "needs-reconciliation": "A provider request may already have executed. Reconcile its retained evidence before explicitly acknowledging any possible duplicate retry.",
        "infeasible": "No eligible plan is installed. Review the missing evidence and rejection reasons; the worker may recheck eligibility.",
        "planning-failed": "Planning inputs could not be used. Inspect the saved decision and correct the underlying evidence or configuration.",
        "running": "A provider request is recorded as running. Cancellation cannot stop a request already sent.",
    }
    if snapshot.status in messages:
        return messages[snapshot.status]
    future = [job.candidate.planned_start for job in snapshot.jobs
              if job.status == "scheduled" and job.candidate.planned_start > now]
    if future:
        return "Work is waiting for its earliest offered start, " + min(future).isoformat() + " (UTC). Worker presence is unknown."
    if snapshot.status == "response_ready":
        return "The customer response is ready; background work remains. Worker presence is unknown."
    return "Work is queued and awaits eligible execution. Worker presence is unknown; start the separate worker if needed."
