"""Local OTP login view."""

import time
import streamlit as st
from workflow.auth import create_otp, verify_otp, send_otp_email, check_rate_limit
from workflow.dashboard.session import finish_login


def initialize_authentication():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "user_email" not in st.session_state:
        st.session_state.user_email = None
    if "otp_sent" not in st.session_state:
        st.session_state.otp_sent = False
    if "last_resend_time" not in st.session_state:
        st.session_state.last_resend_time = 0


def render_login_screen():
    """Render OTP-based login screen."""
    st.markdown('<div style="max-width: 500px; margin: 100px auto; padding: 2rem; background: white; border: 1px solid #E0E0E0; border-radius: 8px;">', unsafe_allow_html=True)

    st.title("🔐 Secure Login")
    st.caption("Local AI Scheduler - Email Verification Required")

    st.divider()

    # Email input section
    if not st.session_state.otp_sent:
        st.markdown("### Enter Your Email")
        st.caption("You'll receive a 6-digit verification code valid for 5 minutes")

        email = st.text_input(
            "Email Address",
            placeholder="your@email.com",
            key="login_email_input",
            label_visibility="collapsed"
        )

        if st.button("Send Verification Code", type="primary", use_container_width=True):
            if not email or "@" not in email:
                st.error("Please enter a valid email address")
            else:
                # Check rate limit
                rate_check = check_rate_limit(email)
                if not rate_check.success:
                    st.error(rate_check.message)
                else:
                    # Generate and send OTP
                    with st.spinner("Sending verification code..."):
                        otp, otp_result = create_otp(email)

                        if otp_result.success:
                            # Send email
                            email_result = send_otp_email(email, otp)

                            if email_result["success"]:
                                st.session_state.otp_sent = True
                                st.session_state.user_email = email
                                st.session_state.last_resend_time = time.time()

                                # Dev mode: show OTP on screen
                                if email_result.get("dev_mode"):
                                    st.session_state.dev_otp = email_result.get("otp")
                                    st.success(f"✓ [DEV MODE] Your code is: **{email_result.get('otp')}**")
                                else:
                                    st.success(f"✓ Code sent to {email}. Check your inbox!")
                                st.rerun()
                            else:
                                st.error(f"❌ {email_result['error']}")
                        else:
                            st.error(f"❌ {otp_result.message}")

    # OTP verification section
    else:
        st.markdown(f"### Verify Code")

        # Show dev mode indicator and OTP if available
        if "dev_otp" in st.session_state and st.session_state.dev_otp:
            st.info(f"🔓 **DEV MODE** - Your verification code is: **{st.session_state.dev_otp}**")
            st.caption(f"Email: **{st.session_state.user_email}** (no email sent in dev mode)")
        else:
            st.caption(f"Code sent to: **{st.session_state.user_email}**")

        otp_input = st.text_input(
            "Enter 6-digit code",
            placeholder="123456",
            max_chars=6,
            key="otp_verification_input",
            label_visibility="collapsed"
        )

        col1, col2 = st.columns([1, 1])

        with col1:
            if st.button("Verify Code", type="primary", use_container_width=True):
                if not otp_input or len(otp_input) != 6 or not otp_input.isdigit():
                    st.error("Please enter a valid 6-digit code")
                else:
                    # Verify OTP
                    result = verify_otp(st.session_state.user_email, otp_input)

                    if result.success:
                        # Authentication successful!
                        finish_login(st.session_state)
                        st.success("✓ Verification successful! Loading dashboard...")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"❌ {result.message}")
                        if result.remaining_attempts is not None and result.remaining_attempts == 0:
                            # Reset to email input
                            st.session_state.otp_sent = False
                            st.session_state.user_email = None

        with col2:
            # Resend button with cooldown
            now = time.time()
            cooldown_remaining = 60 - int(now - st.session_state.last_resend_time)

            if cooldown_remaining > 0:
                st.button(f"Resend ({cooldown_remaining}s)", disabled=True, use_container_width=True)
            else:
                if st.button("Resend Code", use_container_width=True):
                    # Check rate limit
                    rate_check = check_rate_limit(st.session_state.user_email)
                    if not rate_check.success:
                        st.error(rate_check.message)
                    else:
                        with st.spinner("Resending code..."):
                            otp, otp_result = create_otp(st.session_state.user_email)

                            if otp_result.success:
                                email_result = send_otp_email(st.session_state.user_email, otp)

                                if email_result["success"]:
                                    st.session_state.last_resend_time = time.time()

                                    # Dev mode: update displayed OTP
                                    if email_result.get("dev_mode"):
                                        st.session_state.dev_otp = email_result.get("otp")
                                        st.success(f"✓ [DEV MODE] New code: **{email_result.get('otp')}**")
                                    else:
                                        st.success("✓ New code sent!")
                                    st.rerun()
                                else:
                                    st.error(f"❌ {email_result['error']}")
                            else:
                                st.error(f"❌ {otp_result.message}")

        st.divider()

        if st.button("← Back to email input", use_container_width=True):
            st.session_state.pop("dev_otp", None)
            st.session_state.otp_sent = False
            st.session_state.user_email = None
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)

    # Footer with security info
    st.markdown("""
    <div style="text-align: center; margin-top: 2rem; color: #6B6B6B; font-size: 0.85rem;">
        <p>🔒 Your email is used only for authentication and is never shared.</p>
        <p>OTPs are hashed using SHA-256 and expire after 5 minutes.</p>
    </div>
    """, unsafe_allow_html=True)
