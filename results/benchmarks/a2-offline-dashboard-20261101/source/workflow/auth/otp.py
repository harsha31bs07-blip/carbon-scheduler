"""
workflow/auth/otp.py
────────────────────────────────────────────────────────────────────────────────
OTP generation, hashing, verification, and server-side storage.

Security features:
- Cryptographically secure random generation (secrets module)
- SHA-256 hashing with email as salt
- Server-side expiry enforcement
- Rate limiting per email
- Attempt tracking per OTP
- Never stores or logs plaintext OTPs
────────────────────────────────────────────────────────────────────────────────
"""

import secrets
import hashlib
import time
from typing import Optional, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta

# ── Constants ─────────────────────────────────────────────────────────────────
OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = 300  # 5 minutes
MAX_VERIFICATION_ATTEMPTS = 5
MAX_OTP_REQUESTS = 3
RATE_LIMIT_WINDOW_SECONDS = 600  # 10 minutes
RESEND_COOLDOWN_SECONDS = 60

# ── In-memory storage (survives for server lifetime) ──────────────────────────
# NOTE: This is cleared on server restart, which is acceptable for demo/dev use.
# For production, replace with Redis, database, or encrypted file storage.
OTP_STORE: Dict[str, dict] = {}


@dataclass
class OTPResult:
    """Result of OTP operation."""
    success: bool
    message: str
    remaining_attempts: Optional[int] = None
    wait_seconds: Optional[int] = None


def generate_otp() -> str:
    """
    Generate cryptographically secure 6-digit numeric OTP.
    
    Uses secrets.randbelow() for CSPRNG, NOT random.randint().
    
    Returns:
        6-digit string, zero-padded (e.g., "007342")
    """
    code = secrets.randbelow(1000000)  # 0-999999
    return f"{code:06d}"


def hash_otp(otp: str, email: str) -> str:
    """
    Hash OTP with email as salt using SHA-256.
    
    Never store plaintext OTPs. Email is normalized (lowercased, stripped)
    and used as a salt to bind the OTP to the specific email address.
    
    Args:
        otp: 6-digit OTP string
        email: User's email address (normalized internally)
    
    Returns:
        Hexadecimal SHA-256 hash
    """
    normalized_email = email.lower().strip()
    combined = f"{normalized_email}:{otp}"
    return hashlib.sha256(combined.encode('utf-8')).hexdigest()


def _cleanup_expired_entries():
    """Remove expired OTP entries from storage."""
    now = time.time()
    expired_emails = [
        email for email, data in OTP_STORE.items()
        if data.get("expires_at", 0) < now
    ]
    for email in expired_emails:
        del OTP_STORE[email]


def _normalize_email(email: str) -> str:
    """Normalize email for consistent storage keys."""
    return email.lower().strip()


def check_rate_limit(email: str) -> OTPResult:
    """
    Check if email has exceeded OTP request rate limit.
    
    Rate limit: 3 requests per 10 minutes per email.
    
    Args:
        email: User's email address
    
    Returns:
        OTPResult with success=False if rate limited, else success=True
    """
    _cleanup_expired_entries()
    
    email_normalized = _normalize_email(email)
    now = time.time()
    
    if email_normalized not in OTP_STORE:
        return OTPResult(success=True, message="Rate limit OK")
    
    # Get request timestamps and filter to rate limit window
    request_times = OTP_STORE[email_normalized].get("rate_limit_requests", [])
    recent_requests = [t for t in request_times if now - t < RATE_LIMIT_WINDOW_SECONDS]
    
    if len(recent_requests) >= MAX_OTP_REQUESTS:
        # Calculate wait time until oldest request exits the window
        oldest_request = min(recent_requests)
        wait_seconds = int(RATE_LIMIT_WINDOW_SECONDS - (now - oldest_request))
        wait_minutes = (wait_seconds + 59) // 60  # Round up
        
        return OTPResult(
            success=False,
            message=f"Too many verification requests. Please wait {wait_minutes} minute(s) before trying again.",
            wait_seconds=wait_seconds
        )
    
    return OTPResult(success=True, message="Rate limit OK")


def create_otp(email: str) -> Tuple[str, OTPResult]:
    """
    Generate and store a new OTP for the given email.
    
    Checks rate limit before creating. Invalidates any existing OTP
    for this email.
    
    Args:
        email: User's email address
    
    Returns:
        Tuple of (plaintext_otp, OTPResult)
        - If success=False, otp will be empty string
        - If success=True, otp is the 6-digit code to send via email
    
    Security notes:
        - The returned plaintext OTP must be sent via email immediately
        - It should NEVER be logged or stored anywhere
        - Only the hash is persisted in OTP_STORE
    """
    # Check rate limit first
    rate_check = check_rate_limit(email)
    if not rate_check.success:
        return ("", rate_check)
    
    email_normalized = _normalize_email(email)
    
    # Generate OTP
    otp = generate_otp()
    otp_hash = hash_otp(otp, email_normalized)
    
    # Store hashed OTP with metadata
    now = time.time()
    
    # Get existing rate limit history or create new
    existing_requests = []
    if email_normalized in OTP_STORE:
        existing_requests = OTP_STORE[email_normalized].get("rate_limit_requests", [])
        # Filter to keep only recent requests
        existing_requests = [t for t in existing_requests if now - t < RATE_LIMIT_WINDOW_SECONDS]
    
    # Add current request timestamp
    existing_requests.append(now)
    
    OTP_STORE[email_normalized] = {
        "otp_hash": otp_hash,
        "created_at": now,
        "expires_at": now + OTP_EXPIRY_SECONDS,
        "attempts": 0,
        "invalidated": False,
        "rate_limit_requests": existing_requests,
    }
    
    return (otp, OTPResult(
        success=True,
        message=f"OTP generated successfully. Valid for {OTP_EXPIRY_SECONDS // 60} minutes."
    ))


def verify_otp(email: str, otp: str) -> OTPResult:
    """
    Verify the provided OTP against stored hash.
    
    Checks:
    1. OTP exists for this email
    2. Not expired
    3. Not invalidated
    4. Not exceeded max attempts
    5. Hash matches
    
    Args:
        email: User's email address
        otp: 6-digit OTP string entered by user
    
    Returns:
        OTPResult with success=True if verified, else success=False
        
    Side effects:
        - Increments attempt counter on failure
        - Invalidates OTP after max attempts
        - Auto-invalidates on successful verification (single-use)
    """
    _cleanup_expired_entries()
    
    email_normalized = _normalize_email(email)
    now = time.time()
    
    # Check if OTP exists
    if email_normalized not in OTP_STORE:
        return OTPResult(
            success=False,
            message="Invalid or expired code."
        )
    
    stored = OTP_STORE[email_normalized]
    
    # Check if invalidated
    if stored.get("invalidated", False):
        return OTPResult(
            success=False,
            message="This code has been invalidated. Please request a new code."
        )
    
    # Check if expired
    if now > stored.get("expires_at", 0):
        stored["invalidated"] = True
        return OTPResult(
            success=False,
            message="Code has expired. Please request a new code."
        )
    
    # Check max attempts
    current_attempts = stored.get("attempts", 0)
    if current_attempts >= MAX_VERIFICATION_ATTEMPTS:
        stored["invalidated"] = True
        return OTPResult(
            success=False,
            message="Too many failed attempts. Please request a new code."
        )
    
    # Verify hash
    provided_hash = hash_otp(otp, email_normalized)
    stored_hash = stored.get("otp_hash", "")
    
    if provided_hash == stored_hash:
        # SUCCESS - invalidate OTP (single-use)
        stored["invalidated"] = True
        return OTPResult(
            success=True,
            message="Verification successful!"
        )
    else:
        # FAILURE - increment attempts
        stored["attempts"] = current_attempts + 1
        remaining = MAX_VERIFICATION_ATTEMPTS - stored["attempts"]
        
        if remaining > 0:
            return OTPResult(
                success=False,
                message=f"Invalid code. {remaining} attempt(s) remaining.",
                remaining_attempts=remaining
            )
        else:
            # Just exceeded max attempts
            stored["invalidated"] = True
            return OTPResult(
                success=False,
                message="Too many failed attempts. Please request a new code.",
                remaining_attempts=0
            )


def invalidate_otp(email: str):
    """
    Manually invalidate an OTP (e.g., for resend flow).
    
    Args:
        email: User's email address
    """
    email_normalized = _normalize_email(email)
    if email_normalized in OTP_STORE:
        OTP_STORE[email_normalized]["invalidated"] = True


def get_otp_info(email: str) -> Optional[dict]:
    """
    Get OTP metadata for debugging/testing (never returns plaintext OTP).
    
    Args:
        email: User's email address
    
    Returns:
        Dict with metadata or None if no OTP exists
    """
    _cleanup_expired_entries()
    email_normalized = _normalize_email(email)
    
    if email_normalized not in OTP_STORE:
        return None
    
    stored = OTP_STORE[email_normalized]
    now = time.time()
    
    return {
        "exists": True,
        "expired": now > stored.get("expires_at", 0),
        "invalidated": stored.get("invalidated", False),
        "attempts": stored.get("attempts", 0),
        "remaining_attempts": MAX_VERIFICATION_ATTEMPTS - stored.get("attempts", 0),
        "expires_in_seconds": max(0, int(stored.get("expires_at", 0) - now)),
        "created_ago_seconds": int(now - stored.get("created_at", now)),
    }
