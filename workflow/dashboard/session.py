"""Authentication transitions scoped to the current Streamlit session."""


def logout(state):
    """Discard login codes, cached evidence, outputs and unfinished review fields."""
    state.clear()
    state.update(authenticated=False, user_email=None, otp_sent=False, last_resend_time=0)


def finish_login(state):
    state.pop("dev_otp", None)
    state["authenticated"] = True
