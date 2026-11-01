"""
workflow/auth/email_sender.py
────────────────────────────────────────────────────────────────────────────────
Email delivery via SMTP for OTP verification codes.

DEV MODE: Set OTP_DEV_MODE=true in .env to show OTP on screen instead of email.

Security requirements:
- ALL credentials read from environment variables
- ZERO hardcoded credentials
- Clear errors if credentials missing
- Never log plaintext OTP or credentials
────────────────────────────────────────────────────────────────────────────────
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict
import logging

# Suppress verbose SMTP debug output
logging.getLogger("smtplib").setLevel(logging.WARNING)


class EmailSendError(Exception):
    """Raised when email fails to send."""
    pass


def _is_dev_mode() -> bool:
    """Check if OTP development mode is enabled."""
    return os.getenv("OTP_DEV_MODE", "false").lower() == "true"


def send_otp_email(to_email: str, otp: str) -> Dict[str, any]:
    """
    Send OTP verification code via SMTP email or display on screen (dev mode).
    
    DEV MODE: If OTP_DEV_MODE=true in .env, returns success immediately
    and the OTP should be displayed in the UI (handled by dashboard).
    
    Args:
        to_email: Recipient email address
        otp: 6-digit OTP code (plaintext - handle with care)
    
    Returns:
        Dict with:
            - success: bool (True if sent/dev mode, False if failed)
            - error: str (error message if failed, None if success)
            - dev_mode: bool (True if dev mode active)
            - otp: str (OTP code if dev mode, None otherwise)
    
    Security notes:
        - OTP is sent in plaintext via email (standard practice)
        - Email content is NOT logged
        - SMTP credentials from env vars only
        - Errors are sanitized (no credential leakage)
    """
    # Check for dev mode
    if _is_dev_mode():
        logging.info("[DEV MODE] Local verification code generated")
        return {
            "success": True,
            "error": None,
            "dev_mode": True,
            "otp": otp  # Return OTP to display in UI
        }
    
    # Production mode - send real email
    try:
        # Get SMTP config from environment
        config = _get_smtp_config()
        
        # Create email message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your Local AI Scheduler Login Code"
        msg["From"] = config["SMTP_USER"]
        msg["To"] = to_email
        
        # Email body (plain text)
        body = f"""Your verification code is:

      {otp}

This code expires in 5 minutes.

If you didn't request this code, you can safely ignore this email.

─────────────────────────────────────────
Local AI Scheduler
Secure access to carbon-aware AI scheduling
"""
        
        # Attach plain text body
        msg.attach(MIMEText(body, "plain"))
        
        # Connect to SMTP server and send
        with smtplib.SMTP(config["SMTP_HOST"], int(config["SMTP_PORT"])) as server:
            server.starttls()  # Enable TLS encryption
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(msg)
        
        # Success - sanitize email for logging
        email_prefix = to_email[:3] if len(to_email) >= 3 else to_email[0]
        logging.info(f"OTP email sent successfully to {email_prefix}***")
        
        return {
            "success": True,
            "error": None,
            "dev_mode": False,
            "otp": None
        }
    
    except EmailSendError as e:
        # Configuration error (missing env vars)
        logging.error(f"SMTP configuration error: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "dev_mode": False,
            "otp": None
        }
    
    except smtplib.SMTPAuthenticationError:
        # Authentication failed
        logging.error("SMTP authentication failed - check SMTP_USER and SMTP_PASSWORD")
        return {
            "success": False,
            "error": "Email authentication failed. Check SMTP credentials configuration.",
            "dev_mode": False,
            "otp": None
        }
    
    except smtplib.SMTPException as e:
        # Other SMTP errors
        error_type = type(e).__name__
        logging.error(f"SMTP error ({error_type}) when sending to {to_email[:3]}***")
        return {
            "success": False,
            "error": f"Failed to send email: {error_type}. Please try again or contact support.",
            "dev_mode": False,
            "otp": None
        }
    
    except Exception as e:
        # Unexpected errors
        error_type = type(e).__name__
        logging.error(f"Unexpected error ({error_type}) when sending email")
        return {
            "success": False,
            "error": f"Failed to send email: {error_type}. Please try again.",
            "dev_mode": False,
            "otp": None
        }


def _get_smtp_config() -> Dict[str, str]:
    """
    Load SMTP configuration from environment variables.
    
    Required environment variables:
        SMTP_HOST: SMTP server hostname (e.g., smtp.gmail.com)
        SMTP_PORT: SMTP server port (e.g., 587)
        SMTP_USER: SMTP username / from address
        SMTP_PASSWORD: SMTP app password (NOT your regular password)
    
    Returns:
        Dict with SMTP configuration
    
    Raises:
        EmailSendError: If any required env var is missing
    """
    required_vars = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"]
    config = {}
    missing = []
    
    for var in required_vars:
        value = os.getenv(var)
        if not value or value.startswith("your-"):
            missing.append(var)
        config[var] = value
    
    if missing:
        raise EmailSendError(
            f"SMTP credentials not configured. Missing environment variables: {', '.join(missing)}. "
            f"Please set them in your .env file or enable OTP_DEV_MODE=true for testing."
        )
    
    return config


def test_smtp_connection() -> Dict[str, any]:
    """
    Test SMTP connection without sending an email.
    
    Useful for verifying credentials are configured correctly.
    
    Returns:
        Dict with:
            - success: bool
            - message: str (description of result)
    """
    if _is_dev_mode():
        return {
            "success": True,
            "message": "DEV MODE enabled - SMTP not required"
        }
    
    try:
        config = _get_smtp_config()
        
        # Try to connect and authenticate
        with smtplib.SMTP(config["SMTP_HOST"], int(config["SMTP_PORT"]), timeout=10) as server:
            server.starttls()
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        
        return {
            "success": True,
            "message": f"SMTP connection successful to {config['SMTP_HOST']}"
        }
    
    except EmailSendError as e:
        return {
            "success": False,
            "message": str(e)
        }
    
    except smtplib.SMTPAuthenticationError:
        return {
            "success": False,
            "message": "SMTP authentication failed. Check SMTP_USER and SMTP_PASSWORD."
        }
    
    except Exception as e:
        return {
            "success": False,
            "message": f"Connection failed: {type(e).__name__}"
        }
