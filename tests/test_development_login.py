"""Local login displays a code to its caller without writing it to logs."""

import logging

from workflow.auth.email_sender import send_otp_email


def test_development_code_is_returned_but_not_logged(monkeypatch, caplog):
    monkeypatch.setenv("OTP_DEV_MODE", "true")
    with caplog.at_level(logging.INFO):
        result = send_otp_email("local-review@example.invalid", "742913")
    assert result["success"] and result["dev_mode"] and result["otp"] == "742913"
    assert "742913" not in caplog.text and "local-review@example.invalid" not in caplog.text
