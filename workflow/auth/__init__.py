"""
workflow/auth/__init__.py
────────────────────────────────────────────────────────────────────────────────
Authentication module for OTP-based email verification.

⚠️ SECURITY NOTICE:
This module handles sensitive authentication logic. All OTPs are hashed before
storage, and credentials are read from environment variables only.

Never log plaintext OTPs, passwords, or hashes.
────────────────────────────────────────────────────────────────────────────────
"""

from workflow.auth.otp import (
    create_otp,
    verify_otp,
    invalidate_otp,
    check_rate_limit,
    OTPResult,
)

from workflow.auth.email_sender import send_otp_email

__all__ = [
    "create_otp",
    "verify_otp",
    "invalidate_otp",
    "check_rate_limit",
    "send_otp_email",
    "OTPResult",
]
