"""Authentication boundaries across expiry, resends and concurrent sessions."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from workflow.auth import otp
from workflow.dashboard.session import finish_login, logout


@pytest.fixture
def clock(monkeypatch):
    otp.OTP_STORE.clear()
    current = [1000.0]
    monkeypatch.setattr(otp.time, "time", lambda: current[0])
    yield current
    otp.OTP_STORE.clear()


def test_expired_code_retains_ten_minute_request_limit(clock):
    for timestamp in (1000, 1061, 1122):
        clock[0] = timestamp
        assert otp.create_otp("person@example.invalid")[1].success
    clock[0] = 1423
    code, result = otp.create_otp(" PERSON@example.invalid ")
    assert code == "" and not result.success and result.wait_seconds == 177
    clock[0] = 1600
    assert otp.create_otp("person@example.invalid")[1].success


def test_expiry_boundary_and_cleanup_are_independent(clock):
    code, _ = otp.create_otp("person@example.invalid")
    clock[0] += otp.OTP_EXPIRY_SECONDS
    assert not otp.verify_otp("person@example.invalid", code).success
    assert otp.get_otp_info("person@example.invalid")["expired"]
    assert len(otp.OTP_STORE["person@example.invalid"]["rate_limit_requests"]) == 1
    clock[0] = 1600
    assert otp.get_otp_info("person@example.invalid") is None


def test_backend_cooldown_preserves_existing_code(clock):
    code, result = otp.create_otp("person@example.invalid")
    clock[0] += 59.2
    denied_code, result = otp.create_otp("person@example.invalid")
    assert not result.success and result.wait_seconds == 1 and denied_code == ""
    assert otp.verify_otp("person@example.invalid", code).success
    assert not otp.verify_otp("person@example.invalid", code).success
    clock[0] = 1060
    assert otp.create_otp("person@example.invalid")[1].success


def test_success_and_manual_invalidation_do_not_reset_request_limit(clock):
    for index, timestamp in enumerate((1000, 1061, 1122)):
        clock[0] = timestamp
        code, _ = otp.create_otp("person@example.invalid")
        if index % 2:
            otp.invalidate_otp("person@example.invalid")
        else:
            assert otp.verify_otp("person@example.invalid", code).success
    clock[0] = 1183
    assert not otp.create_otp("person@example.invalid")[1].success


def test_wrong_codes_exhaust_attempts_and_cannot_be_reused(clock):
    code, _ = otp.create_otp("person@example.invalid")
    wrong = "000000" if code != "000000" else "111111"
    for expected in range(otp.MAX_VERIFICATION_ATTEMPTS - 1, -1, -1):
        result = otp.verify_otp("person@example.invalid", wrong)
        assert not result.success and result.remaining_attempts == expected
    assert not otp.verify_otp("person@example.invalid", code).success


def test_concurrent_creation_accepts_one_request(clock):
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: otp.create_otp("person@example.invalid"), range(32)))
    assert sum(result.success for _, result in results) == 1
    assert len(otp.OTP_STORE["person@example.invalid"]["rate_limit_requests"]) == 1


def test_concurrent_verification_accepts_code_once(clock):
    code, _ = otp.create_otp("person@example.invalid")
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: otp.verify_otp("person@example.invalid", code), range(32)))
    assert sum(result.success for result in results) == 1


def test_emails_have_independent_limits(clock):
    assert otp.create_otp("first@example.invalid")[1].success
    assert otp.create_otp("second@example.invalid")[1].success
    assert not otp.create_otp(" FIRST@example.invalid ")[1].success


def test_logout_discards_evidence_and_drafts_only_in_current_session():
    current = {"authenticated": True, "user_email": "person@example.invalid",
               "dev_otp": "example", "validated_comparison": {}, "trial-score": .5,
               "queue_selected_workflow": "workflow", "login_otp_input": "example"}
    other = {"authenticated": True, "validated_comparison": {"untouched": True}}
    logout(current)
    assert current == {"authenticated": False, "user_email": None,
                       "otp_sent": False, "last_resend_time": 0}
    assert other["authenticated"] and other["validated_comparison"]


def test_login_removes_development_code():
    state = {"dev_otp": "example", "user_email": "person@example.invalid"}
    finish_login(state)
    assert state["authenticated"] and "dev_otp" not in state


@pytest.mark.parametrize("workspace", ["Ticket workflows", "Legacy policy comparisons"])
def test_both_workspace_logout_buttons_clear_session(workspace):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] /
                              "workflow/dashboard/app.py"), default_timeout=20)
    for key, value in {"authenticated": True, "user_email": "person@example.invalid",
                       "dashboard_workspace": workspace, "dev_otp": "example",
                       "validated_comparison": {"cached": True}, "draft-review": "private"}.items():
        app.session_state[key] = value
    app.run()
    assert not app.exception
    next(button for button in app.button if button.label == "Logout").click().run()
    assert not app.exception and not app.session_state["authenticated"]
    for key in ("dev_otp", "validated_comparison", "draft-review"):
        with pytest.raises(KeyError):
            app.session_state[key]
