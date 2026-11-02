"""Draft persistence cannot grant quality or overwrite human judgments."""

from copy import deepcopy

import pytest

from data.quality_reviews import load_rubric, review_template
from workflow.calibration_approvals import load_approvals, save_approval
from workflow.review_drafts import ReviewDraftStore
from test_quality_reviews import trial, approved


def test_draft_is_bound_to_reviewer_kind_and_exact_evidence(tmp_path):
    rubric, record = load_rubric(), trial()
    template = review_template(record, rubric)
    path = tmp_path / "drafts.sqlite3"
    store = ReviewDraftStore(path)
    store.save("calibration", template, "Reviewer A", {"score": None, "reason": "unfinished"})
    store.close()
    store = ReviewDraftStore(path)
    try:
        assert store.load("calibration", template, "Reviewer A")["score"] is None
        assert store.load("calibration", template, "Reviewer B") is None
        changed = deepcopy(record)
        changed["result"]["text"] = "changed output"
        assert store.load("calibration", review_template(changed, rubric), "Reviewer A") is None
        assert load_approvals(path, [record], rubric) == []
    finally:
        store.close()


def test_calibration_approval_is_validated_additive_and_immutable(tmp_path):
    rubric, record = load_rubric(), trial()
    before = deepcopy(record)
    path = tmp_path / "reviews.sqlite3"
    with pytest.raises(ValueError):
        save_approval(path, [record], rubric, review_template(record, rubric))
    assert not path.exists()
    review = approved(record, rubric)
    save_approval(path, [record], rubric, review)
    save_approval(path, [record], rubric, review)
    assert load_approvals(path, [record], rubric) == [review]
    with pytest.raises(ValueError, match="overwritten"):
        save_approval(path, [record], rubric, approved(record, rubric, score=.5))
    assert record == before
