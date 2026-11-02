"""Explicit draft save/load controls, independent of human approval."""

from pathlib import Path
import sqlite3

from workflow.review_drafts import ReviewDraftStore


def draft_path(state):
    return Path(state.get("review_draft_database", Path(__file__).resolve().parents[2] / ".runtime/review-drafts.sqlite3"))


def load_draft(kind, template, reviewer, allowed_keys):
    import streamlit as st
    from data.quality_reviews import fingerprint
    binding_key = "review-binding-" + kind + "-" + template["trial_id"]
    binding = fingerprint(template)
    owner_key = binding_key + "-owner"
    previous_owner = st.session_state.get(owner_key)
    if st.session_state.get(binding_key) != binding or (previous_owner and previous_owner != reviewer.strip()):
        for key in allowed_keys:
            st.session_state.pop(key, None)
        st.session_state[binding_key] = binding
    st.session_state[owner_key] = reviewer.strip()
    if st.button("Load my saved draft", key="load-draft-" + template["trial_id"]):
        store = None
        try:
            store = ReviewDraftStore(draft_path(st.session_state))
            fields = store.load(kind, template, reviewer)
            if fields is None:
                st.info("No draft matches this reviewer and exact evidence. Changed evidence does not inherit old judgments.")
            else:
                for key, value in fields.items():
                    if key in allowed_keys:
                        st.session_state[key] = value
                st.success("Draft restored. It remains unapproved.")
        except (ValueError, OSError, sqlite3.Error):
            st.error("Enter your reviewer attribution before loading a draft.")
        finally:
            if store is not None:
                store.close()


def save_draft(kind, template, reviewer, fields):
    import streamlit as st
    store = None
    try:
        store = ReviewDraftStore(draft_path(st.session_state))
        store.save(kind, template, reviewer, fields)
        st.success("Draft saved locally. No approval or quality score was imported.")
    except (ValueError, OSError, sqlite3.Error):
        st.error("Draft could not be saved. Enter reviewer attribution and valid editable fields.")
    finally:
        if store is not None:
            store.close()
