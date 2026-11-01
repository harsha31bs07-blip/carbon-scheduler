"""Connected workload behavior: actual inputs, evidence and response deadlines."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from workflow.workload import (
    DEFAULT_KB_PATH,
    DEFAULT_TICKETS_PATH,
    NO_EVIDENCE,
    OUTPUT_LIMITS,
    STEP_ORDER,
    SUPPORTED_CATEGORIES,
    TOKEN_ESTIMATOR,
    KnowledgeArticle,
    KnowledgeBase,
    build_workflow_request,
    parse_classification,
    render_step,
    validate_retrieval_output,
    validate_step_output,
)


NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
TICKET = "I forgot my password and need the reset email."


@pytest.fixture
def kb():
    return KnowledgeBase.load()


def request(ticket=TICKET, **changes):
    return build_workflow_request(ticket, workflow_id="connected-1", arrived_at=NOW, **changes)


def grounded_outputs():
    return {
        "classify": "account_access",
        "retrieve": "Choose Forgot password on the sign-in page and check spam. [KB-ACCOUNT-001]",
        "draft": "Please choose Forgot password, then check your spam folder. [KB-ACCOUNT-001]",
    }


def test_separate_response_and_completion_deadlines_with_fixed_dependencies():
    workflow = request(response_budget_seconds=90, completion_budget_seconds=240)
    assert workflow.response_deadline == NOW + timedelta(seconds=90)
    assert workflow.completion_deadline == NOW + timedelta(seconds=240)
    assert tuple(step.step_id for step in workflow.steps) == STEP_ORDER
    assert tuple(step.dependencies for step in workflow.steps) == ((), ("classify",), ("classify", "retrieve"), ("classify", "retrieve", "draft"))
    for step in workflow.steps[:3]:
        assert step.required_for_response and not step.allow_deferral
        assert step.latest_finish == workflow.response_deadline
    summary = workflow.steps[3]
    assert not summary.required_for_response and summary.allow_deferral
    assert summary.latest_finish == workflow.completion_deadline
    assert all(step.earliest_start == NOW for step in workflow.steps)
    assert all(step.input_tokens is None and step.input_size_source == "unknown" for step in workflow.steps)
    assert {step.step_id: step.output_limit_tokens for step in workflow.steps} == OUTPUT_LIMITS
    assert all(step.enable_thinking is False for step in workflow.steps)


def test_all_urgent_case_constrains_summary_to_customer_deadline():
    workflow = request(all_urgent=True)
    assert all(step.required_for_response for step in workflow.steps)
    assert all(not step.allow_deferral for step in workflow.steps)
    assert all(step.latest_finish == workflow.response_deadline for step in workflow.steps)


@pytest.mark.parametrize("settings", [
    {"response_budget_seconds": 0}, {"response_budget_seconds": True},
    {"response_budget_seconds": 2.5}, {"completion_budget_seconds": -1},
    {"response_budget_seconds": 30, "completion_budget_seconds": 29},
    {"all_urgent": 1}, {"enable_thinking": "false"},
])
def test_invalid_workload_constraints_fail(settings):
    with pytest.raises(ValueError):
        request(**settings)


def test_workload_requires_real_input_and_timezone():
    with pytest.raises(ValueError, match="ticket"):
        request(" \n")
    with pytest.raises(ValueError, match="timezone"):
        build_workflow_request(TICKET, workflow_id="test", arrived_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="datetime range"):
        request(response_budget_seconds=10**30, completion_budget_seconds=10**30)
    with pytest.raises(ValueError, match="KnowledgeBase"):
        request(kb=False)


def test_explicit_provider_default_thinking_is_preserved():
    assert all(step.enable_thinking is None for step in request(enable_thinking=None).steps)


@pytest.mark.parametrize("label", SUPPORTED_CATEGORIES)
def test_classifier_accepts_only_canonical_labels(label):
    assert parse_classification(f"  {label}\n") == label


@pytest.mark.parametrize("text", ["", "Account_access", "technical", "billing because this is an invoice", '"billing"', "billing\nother", "unknown"])
def test_invalid_classification_is_never_guessed(text):
    with pytest.raises(ValueError):
        parse_classification(text)


def test_kb_lookup_uses_category_and_actual_ticket_terms(kb):
    password = kb.lookup("account_access", "Forgot my password; the reset email is missing")
    invoice = kb.lookup("billing", "I need to download an invoice receipt")
    assert password[0].source_id == "KB-ACCOUNT-001"
    assert invoice[0].source_id == "KB-BILLING-001"
    assert all(article.category == "account_access" for article in password)
    assert len(password) <= 3
    assert kb.lookup("other", TICKET) == ()
    assert kb.lookup("technical_issue", "My laser printer produces purple stripes across photographs") == ()


def test_kb_lookup_ties_are_stable_across_article_file_order(kb):
    reversed_kb = replace(kb, articles=tuple(reversed(kb.articles)))
    assert kb.fingerprint() == reversed_kb.fingerprint()
    assert kb.lookup("account_access", "account login password") == reversed_kb.lookup("account_access", "account login password")


@pytest.mark.parametrize("limit", [0, 4, True, 1.5])
def test_kb_lookup_rejects_invalid_top_k(kb, limit):
    with pytest.raises(ValueError):
        kb.lookup("billing", "invoice", limit=limit)


def test_kb_schema_rejects_duplicate_sources_and_unknown_fields(tmp_path):
    data = json.loads(DEFAULT_KB_PATH.read_text(encoding="utf-8"))
    data["articles"].append(data["articles"][0])
    path = tmp_path / "kb.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        KnowledgeBase.load(path)
    data["articles"].pop()
    data["articles"][0]["url"] = "https://invented.invalid"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        KnowledgeBase.load(path)


def test_kb_schema_rejects_duplicate_json_keys_and_malformed_categories(tmp_path):
    path = tmp_path / "kb.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        KnowledgeBase.load(path)
    data = json.loads(DEFAULT_KB_PATH.read_text(encoding="utf-8"))
    data["categories"][0] = ["account_access"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="categories"):
        KnowledgeBase.load(path)


def test_article_validation_has_real_category_and_source_identity():
    with pytest.raises(ValueError, match="source IDs"):
        KnowledgeArticle("made-up", "billing", "Title", "Body")
    with pytest.raises(ValueError, match="category"):
        KnowledgeArticle("KB-TEST-001", "made_up", "Title", "Body")


def test_ticket_and_actual_classification_change_retrieval_prompt(kb):
    workflow = request()
    account = render_step(workflow, "retrieve", {"classify": "account_access"}, kb=kb)
    billing = render_step(workflow, "retrieve", {"classify": "billing"}, kb=kb)
    changed_input = render_step(request("Download our invoice receipt"), "retrieve", {"classify": "billing"}, kb=kb)
    assert TICKET in account.step.prompt
    assert "account_access" in account.step.prompt
    assert "Reset a forgotten password" in account.step.prompt
    assert "KB-ACCOUNT-001" in account.source_ids
    assert account.step.prompt != billing.step.prompt != changed_input.step.prompt
    assert account.input_fingerprint != billing.input_fingerprint != changed_input.input_fingerprint
    assert "KB-BILLING-001" in changed_input.source_ids
    assert account.lookup_seconds >= 0
    by_id = {article.source_id: article for article in kb.articles}
    assert all(source_id in by_id for source_id in account.source_ids)


def test_downstream_prompts_use_actual_retrieval_and_draft(kb):
    workflow = request()
    outputs = grounded_outputs()
    draft = render_step(workflow, "draft", outputs, kb=kb)
    revised = {**outputs, "retrieve": "The reset link expires after 30 minutes. [KB-ACCOUNT-001]"}
    revised_draft = render_step(workflow, "draft", revised, kb=kb)
    assert outputs["retrieve"] in draft.step.prompt
    assert revised["retrieve"] in revised_draft.step.prompt
    assert draft.step.prompt != revised_draft.step.prompt
    assert draft.input_fingerprint != revised_draft.input_fingerprint
    assert draft.source_ids == ("KB-ACCOUNT-001",)
    summary = render_step(workflow, "summarize", outputs, kb=kb)
    revised_summary = render_step(workflow, "summarize", {**outputs, "draft": "Check the spam folder for your reset email. [KB-ACCOUNT-001]"}, kb=kb)
    assert outputs["draft"] in summary.step.prompt
    assert outputs["retrieve"] in summary.step.prompt
    assert summary.step.prompt != revised_summary.step.prompt
    assert summary.input_fingerprint != revised_summary.input_fingerprint


@pytest.mark.parametrize("step_id,outputs", [
    ("retrieve", {}), ("draft", {"classify": "billing"}),
    ("summarize", {"classify": "billing", "retrieve": NO_EVIDENCE}),
])
def test_missing_dependencies_fail_before_a_prompt_is_built(kb, step_id, outputs):
    with pytest.raises(ValueError, match="blocked by missing prerequisite"):
        render_step(request(), step_id, outputs, kb=kb)


def test_invalid_classifier_output_blocks_retrieval(kb):
    with pytest.raises(ValueError, match="exactly one"):
        render_step(request(), "retrieve", {"classify": "Probably account access"}, kb=kb)


def test_changed_dependency_contract_is_rejected_before_render(kb):
    workflow = request()
    workflow = replace(workflow, steps=(workflow.steps[0], replace(workflow.steps[1], dependencies=())) + workflow.steps[2:])
    with pytest.raises(ValueError, match="dependency structure"):
        render_step(workflow, "retrieve", {}, kb=kb)


def test_empty_prerequisite_output_blocks_dependents(kb):
    with pytest.raises(ValueError, match="Prerequisite"):
        render_step(request(), "draft", {"classify": "account_access", "retrieve": " "}, kb=kb)


def test_no_match_has_explicit_grounded_fallback(kb):
    workflow = request("My laser printer produces purple stripes across photographs")
    retrieval = render_step(workflow, "retrieve", {"classify": "technical_issue"}, kb=kb)
    assert retrieval.source_ids == ()
    assert NO_EVIDENCE in retrieval.step.prompt
    validate_retrieval_output(NO_EVIDENCE, ())
    with pytest.raises(ValueError, match="explicitly report"):
        validate_retrieval_output("Replace the ink cartridge.", ())
    draft = render_step(workflow, "draft", {"classify": "technical_issue", "retrieve": NO_EVIDENCE}, kb=kb)
    assert draft.source_ids == ()
    assert "verified product guidance is unavailable" in draft.step.prompt


@pytest.mark.parametrize("text", [
    "Use a reset link. [KB-FAKE-001]",
    "Use a reset link from KB-FAKE-001. [KB-ACCOUNT-001]",
    "Use a reset link. [KB-BILLING-001]",
    "Use a reset link. [kb-account-001]",
    "Use a reset link with no citation.",
])
def test_retrieval_cannot_invent_or_omit_source_identity(text):
    with pytest.raises(ValueError):
        validate_retrieval_output(text, ("KB-ACCOUNT-001",))


def test_draft_cannot_introduce_a_selected_but_uncited_retrieval_source(kb):
    workflow = request("My account login password reset email is missing")
    outputs = grounded_outputs()
    draft = render_step(workflow, "draft", outputs, kb=kb)
    assert "KB-ACCOUNT-002" in {article.source_id for article in kb.lookup("account_access", workflow.input_text)}
    assert "KB-ACCOUNT-002" not in draft.source_ids
    with pytest.raises(ValueError, match="outside"):
        validate_step_output("draft", "Wait for lockout. [KB-ACCOUNT-002]", source_ids=draft.source_ids)


def test_summary_preserves_citation_boundary_and_can_be_citation_free():
    validate_step_output("summarize", "Password assistance was advised; outcome unknown.", source_ids=("KB-ACCOUNT-001",))
    with pytest.raises(ValueError, match="outside"):
        validate_step_output("summarize", "Issue resolved. [KB-EXTERNAL-001]", source_ids=("KB-ACCOUNT-001",))


@pytest.mark.parametrize("step_id", STEP_ORDER)
def test_every_step_rejects_empty_output(step_id):
    with pytest.raises(ValueError, match="nonempty"):
        validate_step_output(step_id, "\n ")


def test_prompt_estimator_uses_materialized_context_and_marks_estimate(kb):
    small = render_step(request(), "classify", {}, kb=kb)
    large = render_step(request(TICKET + " Detailed context." * 50), "classify", {}, kb=kb)
    unicode_ticket = render_step(request("Cannot log in: 帐户访问"), "classify", {}, kb=kb)
    assert small.step.input_tokens == len(small.step.prompt.encode("utf-8"))
    assert large.step.input_tokens > small.step.input_tokens
    assert unicode_ticket.step.input_tokens > len(unicode_ticket.step.prompt)
    assert small.step.input_size_source == TOKEN_ESTIMATOR
    assert small.lookup_seconds == 0


def test_kb_edits_change_workload_version_and_block_old_request(kb):
    article = replace(kb.articles[0], body=kb.articles[0].body + " New policy text.")
    changed = replace(kb, articles=(article,) + kb.articles[1:])
    assert changed.fingerprint() != kb.fingerprint()
    original = request(kb=kb)
    updated = request(kb=changed)
    assert original.workload_version != updated.workload_version
    with pytest.raises(ValueError, match="differs"):
        render_step(original, "classify", {}, kb=changed)
    assert render_step(updated, "classify", {}, kb=changed).step.prompt


def test_materialized_fingerprint_tracks_generation_settings(kb):
    thinking_off = render_step(request(enable_thinking=False), "classify", {}, kb=kb)
    thinking_default = render_step(request(enable_thinking=None), "classify", {}, kb=kb)
    assert thinking_off.step.prompt == thinking_default.step.prompt
    assert thinking_off.input_fingerprint != thinking_default.input_fingerprint


def test_live_prompt_constraints_leave_bounded_generation_headroom(kb):
    workflow = request()
    outputs = grounded_outputs()
    retrieval = render_step(workflow, "retrieve", outputs, kb=kb)
    draft = render_step(workflow, "draft", outputs, kb=kb)
    summary = render_step(workflow, "summarize", outputs, kb=kb)
    assert "at most 3 short numbered evidence statements" in retrieval.step.prompt
    assert "each at most 30 words" in retrieval.step.prompt
    assert "no introduction or heading" in retrieval.step.prompt
    assert "one complete, friendly customer reply of at most 80 words" in draft.step.prompt
    assert "no headings, templates, customer-name placeholders or invented identifiers" in draft.step.prompt
    assert draft.step.output_limit_tokens == 256
    assert "exactly one complete sentence of at most 40 words" in summary.step.prompt
    assert "no headings, templates, placeholders or invented ticket IDs" in summary.step.prompt
    assert summary.step.output_limit_tokens == 128


def test_no_evidence_draft_keeps_the_same_brevity_and_placeholder_constraints(kb):
    workflow = request("My laser printer produces purple stripes across photographs")
    draft = render_step(workflow, "draft", {"classify": "technical_issue", "retrieve": NO_EVIDENCE}, kb=kb)
    assert "one complete, friendly customer reply of at most 80 words" in draft.step.prompt
    assert "no headings, templates, customer-name placeholders or invented identifiers" in draft.step.prompt
    assert draft.step.output_limit_tokens == 256


def test_agent_role_and_actual_citation_instructions_follow_ticket_data(kb):
    rendered = render_step(request(), "draft", grounded_outputs(), kb=kb)
    before, after = rendered.step.prompt.split("\n\nRequired output instructions (outside the ticket data): ")
    assert grounded_outputs()["retrieve"] in before
    assert TICKET in before
    for section in (before, after):
        assert "You are a CarbonDesk SUPPORT AGENT replying TO the customer" in section
        assert "Address the customer as you/your" in section
        assert "do not write as the customer or claim they tried actions absent from the ticket" in section
    assert "Return only the support agent's reply" in after
    assert "Every factual recommendation or action must include a citation" in after
    assert "for example [KB-ACCOUNT-001]" in after


def test_post_data_citation_example_comes_from_actual_retrieval(kb):
    workflow = request("I need to download an invoice receipt")
    outputs = {"classify": "billing", "retrieve": "Owners can download invoices from Invoice history. [KB-BILLING-001]"}
    rendered = render_step(workflow, "draft", outputs, kb=kb)
    after = rendered.step.prompt.split("\n\nRequired output instructions (outside the ticket data): ")[1]
    assert "for example [KB-BILLING-001]" in after
    assert "KB-ACCOUNT" not in after
    assert rendered.source_ids == ("KB-BILLING-001",)


def test_no_evidence_agent_role_does_not_invent_a_citation_example(kb):
    workflow = request("My laser printer produces purple stripes across photographs")
    rendered = render_step(workflow, "draft", {"classify": "technical_issue", "retrieve": NO_EVIDENCE}, kb=kb)
    after = rendered.step.prompt.split("\n\nRequired output instructions (outside the ticket data): ")[1]
    assert "SUPPORT AGENT replying TO the customer" in after
    assert "No verified source is available" in after
    assert "for example [" not in after


def test_materialized_prompt_preserves_request_output_limit_and_fingerprints_it(kb):
    workflow = request()
    default = render_step(workflow, "draft", grounded_outputs(), kb=kb)
    workflow = replace(workflow, steps=workflow.steps[:2] + (replace(workflow.steps[2], output_limit_tokens=384),) + workflow.steps[3:])
    changed = render_step(workflow, "draft", grounded_outputs(), kb=kb)
    assert changed.step.output_limit_tokens == 384
    assert changed.step.prompt == default.step.prompt
    assert changed.input_fingerprint != default.input_fingerprint


def test_fixture_splits_and_expected_sources_are_real(kb):
    data = json.loads(DEFAULT_TICKETS_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    tickets = data["tickets"]
    assert len({ticket["id"] for ticket in tickets}) == len(tickets)
    assert {ticket["split"] for ticket in tickets} == {"calibration", "held_out"}
    assert {ticket["expected_category"] for ticket in tickets} == set(SUPPORTED_CATEGORIES)
    calibration = {ticket["ticket"] for ticket in tickets if ticket["split"] == "calibration"}
    held_out = {ticket["ticket"] for ticket in tickets if ticket["split"] == "held_out"}
    assert calibration.isdisjoint(held_out)
    for ticket in tickets:
        retrieved = {article.source_id for article in kb.lookup(ticket["expected_category"], ticket["ticket"])}
        assert set(ticket["expected_source_ids"]) <= retrieved
        if not ticket["answerable"]:
            assert retrieved == set()
        assert ticket["rubric"]
