import sys
import time
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.express as px
from dotenv import load_dotenv

# ── Load environment variables from .env file ─────────────────────────────────
root_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(root_dir / ".env")  # Load .env file before any other imports

# ── Ensure project root is in sys.path so workflow imports work ─────────────
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from workflow.executor import run_pipeline, run_comparison, schedule_step
from workflow.dashboard.data_loading import load_runs, select_comparison, provenance_label
from shared import availability
from shared.schema import STEP_DEFINITIONS
from workflow.auth import create_otp, verify_otp, send_otp_email, check_rate_limit
from workflow.dashboard.session import finish_login, logout

# ── Configuration ────────────────────────────────────────────────────────────
RESULTS_DIR = root_dir / "results" / "runs"

# ── MONOCHROME PALETTE + ONE ACCENT COLOR ───────────────────────────────────
# Base palette (grayscale only)
COLOR_BG_PAGE = "#FAFAFA"        # Near-white page background
COLOR_BG_CARD = "#F5F5F5"        # Light grey card/section background
COLOR_BORDER = "#E0E0E0"         # Grey borders and dividers
COLOR_TEXT_PRIMARY = "#1A1A1A"   # Near-black primary text
COLOR_TEXT_SECONDARY = "#6B6B6B" # Mid-grey secondary text/captions
COLOR_BASELINE = "#9E9E9E"       # Grey for baseline bars/neutral

# Accent color (ONLY for scheduled/savings - muted green)
COLOR_ACCENT = "#4A9B7F"         # Desaturated teal-green for "eco/scheduled/savings"

# Warning/error (ONLY for actual problems)
COLOR_WARNING = "#D97706"        # Amber for warnings/errors (not used if no errors)

st.set_page_config(
    page_title="Local AI Scheduler",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── CUSTOM CSS FOR MONOCHROME THEME ──────────────────────────────────────────
st.markdown(f"""
<style>
    /* Override Streamlit's default theme colors */
    :root {{
        --background-color: {COLOR_BG_PAGE};
        --secondary-background-color: {COLOR_BG_CARD};
        --text-color: {COLOR_TEXT_PRIMARY};
        --font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }}
    
    /* Page background */
    .stApp {{
        background-color: {COLOR_BG_PAGE};
    }}
    
    /* Sidebar styling */
    section[data-testid="stSidebar"] {{
        background-color: {COLOR_BG_CARD};
        border-right: 1px solid {COLOR_BORDER};
    }}
    
    /* All text defaults to monochrome */
    .stMarkdown, p, span, div {{
        color: {COLOR_TEXT_PRIMARY};
    }}
    
    /* Headers - clean, bold, monochrome */
    h1, h2, h3, h4, h5, h6 {{
        color: {COLOR_TEXT_PRIMARY} !important;
        font-weight: 700 !important;
        letter-spacing: -0.02em;
    }}
    
    h1 {{ font-size: 2.2rem !important; }}
    h2 {{ font-size: 1.4rem !important; }}
    
    /* Metric cards - larger, bold, monochrome */
    [data-testid="stMetricValue"] {{
        font-size: 2.8rem !important;
        font-weight: 700 !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    [data-testid="stMetricLabel"] {{
        font-size: 0.95rem !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: {COLOR_TEXT_SECONDARY} !important;
    }}
    
    [data-testid="stMetricDelta"] {{
        font-size: 1.1rem !important;
        font-weight: 600 !important;
    }}
    
    /* Accent color ONLY on positive deltas (savings) */
    [data-testid="stMetricDelta"] svg[fill*="green"] {{
        fill: {COLOR_ACCENT} !important;
    }}
    
    /* Scheduling decision badges - monochrome with accent */
    .schedule-badge {{
        display: inline-block;
        padding: 0.6rem 1.2rem;
        margin: 0.25rem;
        border-radius: 0.25rem;
        font-size: 1.1rem;
        font-weight: 700;
        background-color: {COLOR_ACCENT};
        color: white;
        text-align: center;
        border: none;
    }}
    
    /* Savings callout - accent color only */
    .savings-callout {{
        font-size: 2rem !important;
        font-weight: 800 !important;
        color: {COLOR_ACCENT} !important;
        text-align: center;
        padding: 1.5rem;
        background-color: {COLOR_BG_CARD};
        border: 2px solid {COLOR_ACCENT};
        border-radius: 0.5rem;
        margin: 1.5rem 0;
    }}
    
    /* Section headers - minimal style */
    .section-header {{
        font-size: 1.4rem !important;
        font-weight: 700 !important;
        margin-top: 2rem;
        margin-bottom: 0.75rem;
        color: {COLOR_TEXT_PRIMARY};
        border-bottom: 2px solid {COLOR_BORDER};
        padding-bottom: 0.5rem;
    }}
    
    /* Section containers with subtle card background */
    .section-card {{
        background-color: white;
        border: 1px solid {COLOR_BORDER};
        border-radius: 0.5rem;
        padding: 1.5rem;
        margin: 1rem 0;
    }}
    
    /* Captions and helper text - grey */
    .stCaption, .chart-help {{
        color: {COLOR_TEXT_SECONDARY} !important;
        font-size: 0.9rem;
        text-align: center;
    }}
    
    /* Buttons - monochrome with subtle hover */
    .stButton button {{
        border-radius: 0.25rem;
        border: 1px solid {COLOR_BORDER};
        font-weight: 600;
        transition: all 0.2s;
    }}
    
    .stButton button[kind="primary"] {{
        background-color: {COLOR_TEXT_PRIMARY};
        color: white;
        border: none;
        font-size: 1.05rem !important;
        padding: 0.6rem 1.5rem !important;
    }}
    
    .stButton button[kind="primary"]:hover {{
        background-color: {COLOR_TEXT_SECONDARY};
    }}
    
    .stButton button[kind="secondary"] {{
        background-color: white;
        color: {COLOR_TEXT_PRIMARY};
        border: 1px solid {COLOR_BORDER};
        font-size: 0.9rem !important;
    }}
    
    .stButton button[kind="secondary"]:hover {{
        background-color: {COLOR_BG_CARD};
    }}
    
    /* Sliders - monochrome track with accent on active */
    .stSlider [data-baseweb="slider"] {{
        background-color: {COLOR_BG_CARD};
    }}
    
    .stSlider [data-baseweb="slider"] [role="slider"] {{
        background-color: {COLOR_ACCENT};
        border: 2px solid {COLOR_ACCENT};
    }}
    
    /* Dividers - subtle grey */
    hr {{
        border-color: {COLOR_BORDER};
        margin: 2rem 0;
    }}
    
    /* Info/warning boxes - monochrome */
    .stAlert {{
        background-color: {COLOR_BG_CARD} !important;
        border-left: 4px solid {COLOR_ACCENT};
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    .stAlert > div {{
        background-color: transparent !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* SELECTBOX - Complete styling for closed and open states */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    /* Closed selectbox container */
    .stSelectbox {{
        color: {COLOR_TEXT_PRIMARY};
    }}
    
    /* Closed selectbox input box */
    .stSelectbox [data-baseweb="select"] {{
        background-color: white !important;
        border-color: {COLOR_BORDER} !important;
    }}
    
    /* Closed selectbox - the inner div with the selected value */
    .stSelectbox [data-baseweb="select"] > div {{
        background-color: white !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* Selected value text */
    .stSelectbox [data-baseweb="select"] > div > div {{
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* Dropdown arrow icon */
    .stSelectbox [data-baseweb="select"] svg {{
        fill: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* Dropdown popover (rendered in portal, needs explicit targeting) */
    div[data-baseweb="popover"] {{
        background-color: white !important;
    }}
    
    /* Dropdown list container */
    ul[role="listbox"] {{
        background-color: white !important;
        border: 1px solid {COLOR_BORDER} !important;
    }}
    
    /* Individual dropdown options */
    li[role="option"] {{
        background-color: white !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* Dropdown option hover state */
    li[role="option"]:hover {{
        background-color: {COLOR_BG_CARD} !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* Selected option in dropdown */
    li[role="option"][aria-selected="true"] {{
        background-color: {COLOR_BG_CARD} !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* CHECKBOX - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .stCheckbox {{
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    .stCheckbox > label {{
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    .stCheckbox input[type="checkbox"] {{
        background-color: white !important;
        border-color: {COLOR_BORDER} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* TEXT INPUT - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .stTextInput input {{
        background-color: white !important;
        color: {COLOR_TEXT_PRIMARY} !important;
        border-color: {COLOR_BORDER} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* NUMBER INPUT - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .stNumberInput input {{
        background-color: white !important;
        color: {COLOR_TEXT_PRIMARY} !important;
        border-color: {COLOR_BORDER} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* MULTISELECT - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .stMultiSelect [data-baseweb="select"] {{
        background-color: white !important;
        border-color: {COLOR_BORDER} !important;
    }}
    
    .stMultiSelect [data-baseweb="select"] > div {{
        background-color: white !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    .stMultiSelect [data-baseweb="tag"] {{
        background-color: {COLOR_BG_CARD} !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* RADIO - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .stRadio > label {{
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    .stRadio [role="radiogroup"] label {{
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* EXPANDER - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .streamlit-expanderHeader {{
        background-color: {COLOR_BG_CARD} !important;
        color: {COLOR_TEXT_PRIMARY} !important;
    }}
    
    .streamlit-expanderContent {{
        background-color: white !important;
        border-color: {COLOR_BORDER} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* TABS - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .stTabs [data-baseweb="tab-list"] {{
        background-color: transparent !important;
    }}
    
    .stTabs [data-baseweb="tab"] {{
        background-color: transparent !important;
        color: {COLOR_TEXT_SECONDARY} !important;
    }}
    
    .stTabs [aria-selected="true"] {{
        color: {COLOR_TEXT_PRIMARY} !important;
        border-bottom-color: {COLOR_ACCENT} !important;
    }}
    
    /* ═══════════════════════════════════════════════════════════════════ */
    /* SPINNER - Ensure light theme */
    /* ═══════════════════════════════════════════════════════════════════ */
    
    .stSpinner > div {{
        border-top-color: {COLOR_ACCENT} !important;
    }}
    
    /* Dataframes - clean monochrome */
    .stDataFrame {{
        border: 1px solid {COLOR_BORDER};
    }}
    
    /* Consistent spacing between sections */
    .block-container {{
        padding-top: 2rem;
        padding-bottom: 3rem;
    }}
</style>
""", unsafe_allow_html=True)

# ── Helper to find the most recent run ───────────────────────────────────────
def get_latest_run_id():
    runs = load_runs(RESULTS_DIR)
    return runs[0]["run_id"] if runs else None


def get_comparison_data(scheduled_run_id):
    pair = select_comparison(load_runs(RESULTS_DIR), scheduled_run_id)
    if pair is None:
        return None, None, None, None, None
    baseline, scheduled = pair["baseline"], pair["scheduled"]
    return baseline["totals"], scheduled["totals"], baseline["run_id"], scheduled["run_id"], scheduled["policy"]


# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION GATE - All dashboard content gated behind OTP verification
# ══════════════════════════════════════════════════════════════════════════════

# Initialize authentication state
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


# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION CHECK - Show login screen if not authenticated
# ══════════════════════════════════════════════════════════════════════════════

if not st.session_state.authenticated:
    render_login_screen()
    st.stop()  # Stop rendering the rest of the dashboard

# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED CONTENT BELOW THIS LINE
# ══════════════════════════════════════════════════════════════════════════════

# ── State Management ─────────────────────────────────────────────────────────
workspace = st.sidebar.radio("Workspace", ["Ticket workflows", "Legacy policy comparisons"], key="dashboard_workspace")
if workspace == "Ticket workflows":
    from workflow.dashboard.queue_view import render_queue_view
    st.sidebar.caption(f"Logged in: {st.session_state.user_email}")
    if st.sidebar.button("Logout"):
        logout(st.session_state)
        st.rerun()
    render_queue_view(root_dir)
    st.stop()

if "run_id" not in st.session_state:
    st.session_state.run_id = get_latest_run_id()

# ── Sidebar / Controls ───────────────────────────────────────────────────────
with st.sidebar:
    # Show logged in user
    st.caption(f"✓ Logged in: {st.session_state.user_email}")
    if st.button("Logout", use_container_width=True):
        logout(st.session_state)
        st.rerun()
    st.divider()
    
    # ── Mode Status Indicator ────────────────────────────────────────────────
    GEMINI_AVAILABLE, gemini_detail = availability.gemini_status()
    NVIDIA_AVAILABLE, nvidia_detail = availability.nvidia_status()
    
    if GEMINI_AVAILABLE and NVIDIA_AVAILABLE:
        mode_status = "🌐 **Hybrid: Gemini + NVIDIA configured**"
        mode_detail = "Provider configuration present; connectivity is checked when run"
    elif GEMINI_AVAILABLE:
        mode_status = "🌐 **Hybrid: Gemini configured**"
        mode_detail = gemini_detail
    elif NVIDIA_AVAILABLE:
        mode_status = "🌐 **Hybrid: NVIDIA configured**"
        mode_detail = nvidia_detail
    else:
        mode_status = "🔌 **Offline Only**"
        mode_detail = "Local models only; Ollama must be running for live execution"
    
    st.markdown(mode_status)
    st.caption(mode_detail)
    st.divider()
    
    st.header("Controls")
    
    st.caption("🖥️ Local Models: qwen3.5:4b (4B params) · llama3.2:3b (3B params)")
    if GEMINI_AVAILABLE or NVIDIA_AVAILABLE:
        st.caption("☁️ Cloud Models: " + (
            "gemini-3-flash" if GEMINI_AVAILABLE else ""
        ) + (", " if GEMINI_AVAILABLE and NVIDIA_AVAILABLE else "") + (
            "nemotron-ultra-3" if NVIDIA_AVAILABLE else ""
        ))
    st.caption("💰 Local Cost: $0 (no API charges)")
    st.divider()
    
    policies = ["eco", "balanced", "speed", "custom"]
    selected_policy = st.selectbox(
        "Scheduling Policy", 
        policies, 
        index=0,
        help="eco: minimize carbon · speed: minimize latency · balanced: optimize quality per task"
    )
    
    # Replay mode toggle for demo
    replay_mode = st.checkbox("Replay Mode", value=True, help="Use curated recordings. Missing matches stop the run without live inference.")
    
    # ── Custom Weight Sliders ────────────────
    if selected_policy == "custom":
        st.divider()
        st.subheader("Custom Weights")
        st.caption("Adjust priorities for the five tradeoff dimensions")
        
        if "custom_weights" not in st.session_state:
            st.session_state.custom_weights = {
                "w_cost": 0.2, "w_energy": 0.2, "w_carbon": 0.2,
                "w_latency": 0.2, "w_accuracy": 0.2,
            }
        
        cost_w = st.slider("Cost", 0.0, 1.0, st.session_state.custom_weights["w_cost"], 0.05, key="w_cost")
        energy_w = st.slider("Energy", 0.0, 1.0, st.session_state.custom_weights["w_energy"], 0.05, key="w_energy")
        carbon_w = st.slider("Carbon", 0.0, 1.0, st.session_state.custom_weights["w_carbon"], 0.05, key="w_carbon")
        latency_w = st.slider("Latency", 0.0, 1.0, st.session_state.custom_weights["w_latency"], 0.05, key="w_latency")
        accuracy_w = st.slider("Accuracy", 0.0, 1.0, st.session_state.custom_weights["w_accuracy"], 0.05, key="w_accuracy")
        
        raw_weights = {
            "w_cost": cost_w, "w_energy": energy_w, "w_carbon": carbon_w,
            "w_latency": latency_w, "w_accuracy": accuracy_w,
        }
        
        total = sum(raw_weights.values())
        if total > 0:
            normalized = {k: v / total for k, v in raw_weights.items()}
        else:
            normalized = {k: 0.2 for k in raw_weights}
        
        st.session_state.custom_weights = normalized
        
        if "live_schedule" not in st.session_state:
            st.session_state.live_schedule = {}
        
        live_schedule = {}
        for step_def in STEP_DEFINITIONS:
            step_id = step_def["step_id"]
            candidate = schedule_step(step_id, "custom", normalized)
            live_schedule[step_id] = candidate
        st.session_state.live_schedule = live_schedule
    
    # ── Action Buttons ───────────────────────────────────────────────────────
    st.divider()
    if st.button("Run Comparison", type="primary", use_container_width=True, key="btn_run_comparison"):
        custom_w = st.session_state.get("custom_weights") if selected_policy == "custom" else None
        with st.spinner(f"Running baseline and {selected_policy.upper()} with shared settings..."):
            try:
                comparison = run_comparison(policy=selected_policy, custom_weights=custom_w, replay_mode=replay_mode)
            except Exception as exc:
                st.error(f"Comparison stopped: {exc}")
            else:
                st.session_state.run_id = comparison["scheduled_run_id"]
                st.rerun()
    st.caption("Runs a baseline and selected policy on the same workload and reference time.")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Single Run", type="secondary", use_container_width=True, key="btn_run_pipeline"):
            with st.spinner(f"Running {selected_policy}..."):
                try:
                    custom_w = st.session_state.get("custom_weights") if selected_policy == "custom" else None
                    new_run_id = run_pipeline(policy=selected_policy, custom_weights=custom_w, replay_mode=replay_mode)
                except Exception as exc:
                    st.error(f"Run stopped: {exc}")
                else:
                    st.session_state.run_id = new_run_id
                    st.rerun()
    
    with col2:
        confirm_clear = st.session_state.get("confirm_clear", False)
        if st.button("Clear All", type="secondary", use_container_width=True, disabled=not confirm_clear, key="btn_clear"):
            deleted_count = 0
            if RESULTS_DIR.exists():
                for f in RESULTS_DIR.glob("*.jsonl"):
                    try:
                        f.unlink()
                        f.with_suffix(".manifest.json").unlink(missing_ok=True)
                        deleted_count += 1
                    except Exception:
                        pass
            st.session_state.run_id = None
            st.session_state.pop("live_schedule", None)
            st.session_state.confirm_clear = False
            st.success(f"Deleted {deleted_count} files")
            st.rerun()
    
    st.checkbox("Confirm clear", key="confirm_clear")

# ── Main Content Area ────────────────────────────────────────────────────────
mode_caption = availability.mode_label() + " · Latency from execution or replay · Cost, energy, carbon and quality proxy use estimates"

st.title("Local AI Scheduler")
st.caption(mode_caption)

available_runs = load_runs(RESULTS_DIR)
if st.checkbox("Show legacy runs", help="Browse older complete runs with unknown metadata; these cannot be used for comparisons."):
    known_ids = {run["run_id"] for run in available_runs}
    available_runs.extend(run for run in load_runs(root_dir / "results")
                          if run["schema_version"] == 1 and run["run_id"] not in known_ids)
run_ids = [run["run_id"] for run in available_runs]
if run_ids:
    current_index = run_ids.index(st.session_state.run_id) if st.session_state.run_id in run_ids else 0
    st.session_state.run_id = st.selectbox("Completed run", run_ids, index=current_index)
run_id = st.session_state.run_id

# ══════════════════════════════════════════════════════════════════════════════
# LIVE CUSTOM SCHEDULE PREVIEW (for custom policy)
# ══════════════════════════════════════════════════════════════════════════════
if selected_policy == "custom" and "live_schedule" in st.session_state and st.session_state.live_schedule:
    st.markdown('<p class="section-header">Scenario Preview</p>', unsafe_allow_html=True)
    st.caption("Projected regions and time windows. Live execution uses configured endpoints and runs immediately.")
    
    live_schedule = st.session_state.live_schedule
    
    # Scheduling decisions
    dec_cols = st.columns(4)
    for idx, step_def in enumerate(STEP_DEFINITIONS):
        step_id = step_def["step_id"]
        cand = live_schedule.get(step_id)
        if cand:
            with dec_cols[idx]:
                st.markdown(f"**{step_id.upper()}**")
                st.markdown(
                    f'<div class="schedule-badge">{cand.model}<br/>{cand.location}</div>',
                    unsafe_allow_html=True
                )
                if cand.time_offset_hr > 0:
                    st.caption(f"Projected +{cand.time_offset_hr}h wait")
    
    st.divider()
    
    # Five tradeoff totals
    total_cost = sum(c.est_cost_usd for c in live_schedule.values())
    total_energy = sum(c.est_energy_kwh * 1000 for c in live_schedule.values())
    total_carbon = sum(c.est_carbon_g for c in live_schedule.values())
    total_latency = sum(c.est_latency_ms for c in live_schedule.values())
    avg_accuracy = sum(c.est_accuracy for c in live_schedule.values()) / len(live_schedule)
    
    cols = st.columns(5)
    cols[0].metric("Carbon", f"{total_carbon:.2f} gCO₂e", help="ESTIMATED: energy × grid factor")
    cols[1].metric("Cost", f"${total_cost:.5f}", help="Estimated from configured pricing and token assumptions")
    cols[2].metric("Energy", f"{total_energy:.1f} Wh", help="Estimated model energy")
    cols[3].metric("Inference latency", f"{total_latency:.0f} ms", help="Estimated from benchmarks; projected waiting is shown separately per step")
    cols[4].metric("Accuracy*", f"{avg_accuracy:.3f}", help="Benchmark-based proxy scores")
    
    st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# MAIN RESULTS DISPLAY
# ══════════════════════════════════════════════════════════════════════════════
if run_id:
    selected_run = next((run for run in available_runs if run["run_id"] == run_id), None)
    if selected_run:
        records = selected_run["records"]
        
        if records:
            policy_used = records[0].get("policy", "unknown")
            execution_mode = selected_run.get("execution_mode") or "unknown (legacy)"
            st.caption(
                f"Mode: {execution_mode} · Policy: {policy_used} · "
                f"Workload: {selected_run.get('workload_id') or 'unknown'} · "
                f"Calibration: {selected_run.get('calibration_id') or 'unknown'}"
            )
            if execution_mode == "replay":
                st.info("Replay: latency and outputs come from recorded execution. This run does not measure current hardware.")
            st.caption("Cloud execution regions are provider-managed and unknown; displayed cloud locations are reference assumptions for projected carbon.")
            
            # ──────────────────────────────────────────────────────────────────
            # SCHEDULING DECISIONS
            # ──────────────────────────────────────────────────────────────────
            st.markdown('<p class="section-header">Scheduling Decisions</p>', unsafe_allow_html=True)
            
            # Check for model diversity
            models_used = set(r.get("model") for r in records)
            if len(models_used) == 1 and "llama3.2" in list(models_used)[0].lower():
                st.info("💡 Note: llama3.2:3b (smaller model) often wins due to better speed/carbon tradeoff vs. qwen3.5:4b. Policies may converge when one model dominates across all metrics.")
            
            dec_cols = st.columns(4)
            
            for idx, r in enumerate(records):
                with dec_cols[idx]:
                    st.markdown(f"**{r.get('step_id', '').upper()}**")
                    
                    # Check if fallback was used for this step
                    fallback_used = r.get("fallback_used", False)
                    model_display = r.get("model")
                    
                    if fallback_used:
                        original_model = r.get("original_model", "unknown")
                        st.markdown(
                            f'<div class="schedule-badge" style="background-color: {COLOR_WARNING};">'
                            f'{model_display}<br/>{r.get("location")}<br/>⚠️ Fallback</div>',
                            unsafe_allow_html=True
                        )
                        st.caption(f"Original: {original_model}")
                    else:
                        st.markdown(
                            f'<div class="schedule-badge">{model_display}<br/>{r.get("location")}</div>',
                            unsafe_allow_html=True
                        )
                    
                    if r.get("time_offset", 0) > 0:
                        st.caption(f"Projected +{r.get('time_offset', 0) / 3600:g}h wait")
            
            st.divider()
            
            # ──────────────────────────────────────────────────────────────────
            # FIVE TRADEOFF TOTALS
            # ──────────────────────────────────────────────────────────────────
            st.markdown('<p class="section-header">Total Metrics</p>', unsafe_allow_html=True)
            
            totals = selected_run["totals"]
            total_cost, total_energy, total_carbon = totals["cost"], totals["energy"], totals["carbon"]
            total_latency, avg_accuracy = totals["latency"], totals["accuracy"]
            
            metric_cols = st.columns(5)
            
            metric_cols[0].metric("Carbon", f"{total_carbon:.2f} gCO₂e", delta=provenance_label(records, "carbon"), delta_color="off", help="Modeled energy × reference grid intensity; region/time scenarios are projections")
            metric_cols[1].metric("Cost", f"${total_cost:.5f}", delta=provenance_label(records, "cost"), delta_color="off", help="Configured pricing and token assumptions; not an invoice")
            metric_cols[2].metric("Energy", f"{total_energy:.1f} Wh", delta=provenance_label(records, "energy"), delta_color="off", help="Model estimate. Any client-reported observations are retained separately in run records.")
            metric_cols[3].metric("Latency", f"{total_latency:.0f} ms", delta=provenance_label(records, "latency"), delta_color="off", help="Dispatch duration including failed attempts when recorded; legacy runs use successful response duration")
            metric_cols[4].metric("Quality proxy", f"{avg_accuracy:.3f}", delta=provenance_label(records, "accuracy"), delta_color="off", help="Benchmark-based estimate; not a task-specific quality assessment")
            
            st.divider()
            
            # ──────────────────────────────────────────────────────────────────
            # BASELINE COMPARISON
            # ──────────────────────────────────────────────────────────────────
            st.markdown('<p class="section-header">Savings vs Baseline</p>', unsafe_allow_html=True)
            
            baseline_totals, scheduled_totals, baseline_run_id, scheduled_run_id, scheduled_policy = get_comparison_data(run_id)
            
            if baseline_totals and scheduled_totals and policy_used != "baseline":
                st.caption(
                    f"Matched comparison: {selected_run['comparison_id']} · "
                    f"Provider mode: {selected_run['provider_mode']} · Reference: {selected_run['reference_utc_time']}"
                )
                st.caption(f"Baseline: {baseline_run_id} · Scheduled: {scheduled_run_id}")
                base_carb = baseline_totals.get("carbon", 0)
                base_lat = baseline_totals.get("latency", 0)
                carb_saved_pct = ((base_carb - scheduled_totals["carbon"]) / base_carb * 100) if base_carb != 0 else None
                lat_saved_pct = ((base_lat - scheduled_totals["latency"]) / base_lat * 100) if base_lat != 0 else None
                carbon_change = "Carbon percentage unavailable (zero baseline)" if carb_saved_pct is None else (
                    "Carbon unchanged" if carb_saved_pct == 0 else f'{abs(carb_saved_pct):.0f}% modeled carbon {"lower" if carb_saved_pct > 0 else "higher"}'
                )
                latency_change = "Latency percentage unavailable (zero baseline)" if lat_saved_pct is None else (
                    "Latency unchanged" if lat_saved_pct == 0 else f'{abs(lat_saved_pct):.0f}% {"faster" if lat_saved_pct > 0 else "slower"}'
                )
                
                # Savings callout (local-only scope: carbon and latency)
                st.markdown(
                    f'<div class="savings-callout">'
                    f'{carbon_change} · {latency_change}'
                    f'</div>',
                    unsafe_allow_html=True
                )
                
                # Comparison chart - monochrome with accent on scheduled
                metrics_config = [
                    ("carbon", "Carbon", "gCO₂e", True),
                    ("cost", "Cost", "USD", True),
                    ("energy", "Energy", "Wh", True),
                    ("latency", "Latency", "ms", True),
                    ("accuracy", "Quality proxy", "", False),
                ]
                
                plot_data = []
                
                for key, label, unit, lower_better in metrics_config:
                    baseline_val = baseline_totals[key]
                    scheduled_val = scheduled_totals[key]
                    
                    metric_label = f"{label} ({unit})" if unit else label
                    plot_data.append({"Metric": metric_label, "Value": baseline_val, "Type": "Baseline"})
                    plot_data.append({"Metric": metric_label, "Value": scheduled_val, "Type": "Scheduled"})
                
                plot_df = pd.DataFrame(plot_data)
                
                # Create figure with explicit monochrome colors
                fig = px.bar(
                    plot_df,
                    x="Type",
                    y="Value",
                    color="Type",
                    barmode="group",
                    facet_col="Metric",
                    facet_col_wrap=3,
                    color_discrete_map={"Baseline": COLOR_BASELINE, "Scheduled": COLOR_ACCENT},
                    text_auto='.3s',
                )
                
                # Style chart to match monochrome theme
                fig.update_layout(
                    height=600,
                    paper_bgcolor='white',
                    plot_bgcolor='white',
                    font=dict(family="Inter, sans-serif", size=13, color=COLOR_TEXT_PRIMARY),
                    legend_title_text="",
                    yaxis_title="",
                    xaxis_title="",
                    showlegend=True,
                    legend=dict(
                        orientation="h",
                        yanchor="bottom",
                        y=1.02,
                        xanchor="right",
                        x=1
                    ),
                    margin=dict(l=40, r=40, t=40, b=40),
                )
                
                # Monochrome grid and axes
                fig.update_xaxes(
                    showgrid=False,
                    showline=True,
                    linewidth=1,
                    linecolor=COLOR_BORDER,
                    tickfont=dict(color=COLOR_TEXT_PRIMARY)
                )
                fig.update_yaxes(
                    matches=None,
                    showgrid=True,
                    gridwidth=1,
                    gridcolor=COLOR_BORDER,
                    showline=True,
                    linewidth=1,
                    linecolor=COLOR_BORDER,
                    tickfont=dict(color=COLOR_TEXT_PRIMARY)
                )
                
                fig.update_traces(textposition='outside', textfont=dict(color=COLOR_TEXT_PRIMARY))
                
                st.plotly_chart(fig, use_container_width=True)
                
                st.caption(f"Energy and cost: estimates · Carbon: model projection · Latency: {provenance_label(records, 'latency')} · Quality: estimated proxy")
                
            elif policy_used == "baseline":
                st.info("📊 Baseline reference: Always qwen3.5:4b (larger local model), no time-of-day optimization")
            else:
                st.info("No compatible complete comparison for this run. Use Run Comparison to create a baseline and policy pair with shared settings. Legacy and partial runs are excluded.")
            with st.expander("Step outputs and metric sources"):
                for record in records:
                    st.markdown(f"**{record['step_id'].upper()}**")
                    st.text(record.get("output_text") or "Output unavailable in this recording")
                st.json(records)
            
    else:
        st.warning("This run is incomplete, malformed, or missing a completed manifest. Select a complete run or run a new comparison.")
else:
    st.info("No complete runs yet. Select a policy and click Run Comparison to begin. Replay requires curated recordings; missing or invalid records never count as results.")
