"""Streamlit entry point; each workspace loads its own dependencies."""

import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

root_dir = Path(__file__).resolve().parents[2]
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
load_dotenv(root_dir / ".env")

from workflow.dashboard.login_view import initialize_authentication, render_login_screen
from workflow.dashboard.session import logout
from workflow.dashboard.styles import apply_theme

st.set_page_config(page_title="Carbon Scheduler", layout="wide", initial_sidebar_state="expanded")
apply_theme()
initialize_authentication()
if not st.session_state.authenticated:
    render_login_screen()
    st.stop()

workspace = st.sidebar.radio("Workspace", ["Overview", "Ticket workflows", "Legacy policy comparisons"],
                             key="dashboard_workspace")
st.sidebar.caption(f"Logged in: {st.session_state.user_email}")
if st.sidebar.button("Logout"):
    logout(st.session_state)
    st.rerun()

if workspace == "Overview":
    from workflow.dashboard.overview_view import render_overview
    render_overview(root_dir)
elif workspace == "Ticket workflows":
    from workflow.dashboard.queue_view import render_queue_view
    render_queue_view(root_dir)
else:
    from workflow.dashboard.legacy_view import render_legacy_view
    render_legacy_view(root_dir)
