"""Shared dashboard palette and existing theme."""

import streamlit as st

COLOR_BG_PAGE = "#FAFAFA"
COLOR_BG_CARD = "#F5F5F5"
COLOR_BORDER = "#E0E0E0"
COLOR_TEXT_PRIMARY = "#1A1A1A"
COLOR_TEXT_SECONDARY = "#6B6B6B"
COLOR_BASELINE = "#9E9E9E"
COLOR_ACCENT = "#4A9B7F"
COLOR_WARNING = "#D97706"


def apply_theme():
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
