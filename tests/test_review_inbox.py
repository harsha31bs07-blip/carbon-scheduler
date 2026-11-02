"""The inbox exposes real evidence and drafts without inventing human approvals."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from test_evaluation_protocol import protocol
from test_workflow_quality import reviewed
from workflow.policy_comparison import load_evaluation

ROOT = Path(__file__).resolve().parents[1]


def test_calibration_inbox_keeps_blank_judgments_unapproved(tmp_path):
    script = tmp_path / "app.py"
    script.write_text("from workflow.dashboard.review_inbox import render_review_inbox\n"
                      + f"render_review_inbox({str(ROOT)!r})\n", encoding="utf-8")
    app = AppTest.from_file(str(script), default_timeout=30)
    database = tmp_path / "drafts.sqlite3"
    app.session_state["review_draft_database"] = str(database)
    app.run()
    assert not app.exception and app.number_input
    assert all(item.value is None for item in app.number_input)
    next(item for item in app.button if item.label == "Approve calibration judgment").click().run()
    assert not app.exception and app.error and not database.exists()


def test_workflow_partial_draft_does_not_import_quality(tmp_path, reviewed):
    root, *_ = reviewed
    script = tmp_path / "app.py"
    script.write_text("from workflow.review_workflow_quality import review_inputs\n"
        "from workflow.dashboard.evidence_view import render_review_form\n"
        + f"protocol, trials = review_inputs({str(root)!r})\n"
        + f"render_review_form({str(root)!r}, protocol, trials[0], portable=False, already_reviewed=False)\n",
        encoding="utf-8")
    app = AppTest.from_file(str(script), default_timeout=30)
    database = tmp_path / "drafts.sqlite3"
    app.session_state["review_draft_database"] = str(database)
    app.run()
    assert not app.exception
    next(item for item in app.text_input if item.label == "Reviewer attribution").set_value("Synthetic draft fixture")
    app.number_input[0].set_value(.4)
    next(item for item in app.button if item.label == "Save review draft").click().run()
    assert not app.exception and app.success and database.exists()
    assert not (root / "quality_reviews.json").exists()
    assert all(arm["quality"]["value"] is None for arm in load_evaluation(root)["arms"])
