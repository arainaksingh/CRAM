from pathlib import Path
import streamlit as st
from cram.config import config
from cram.db.repository import Repository
from cram.db.schema import initialize_database
from cram.pipeline.session_processor import SessionProcessor
from cram.pipeline.ingestion import IngestionService, TranscriptParseError


@st.cache_resource
def get_repo():
    initialize_database(config.db_path)
    return Repository(config.db_path)


def main():
    st.set_page_config(
        page_title="CRAM — AI Memory Fact Extraction",
        page_icon="🧠",
        layout="wide",
    )

    # Custom CSS for clean, professional look
    st.markdown(
        """
        <style>
        .main-header { font-size: 1.8rem; font-weight: 700; color: #1E293B; margin-bottom: 0.2rem; }
        .sub-header { color: #64748B; font-size: 0.95rem; margin-bottom: 1.5rem; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
        .badge-new { background-color: #DBEAFE; color: #1D4ED8; }
        .badge-dup { background-color: #F3F4F6; color: #4B5563; }
        .badge-update { background-color: #FEF3C7; color: #D97706; }
        .badge-corr { background-color: #FCE7F3; color: #BE185D; }
        .badge-unres { background-color: #E0E7FF; color: #4338CA; }
        .card { background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    repo = get_repo()

    # Sidebar: Controls & Setup
    st.sidebar.title("🧠 CRAM Memory")
    st.sidebar.caption("Feature 1: Structured Fact Extraction")

    st.sidebar.markdown("---")
    st.sidebar.markdown(f"**Database:** `{Path(config.db_path).name}`")
    st.sidebar.markdown(f"**Model:** `{config.model_name}`")
    api_key_status = "✅ Configured" if config.gemini_api_key else "❌ Missing"
    st.sidebar.markdown(f"**Gemini API Key:** {api_key_status}")

    st.sidebar.markdown("---")
    st.sidebar.subheader("Sessions")

    # Create new session
    new_sess_name = st.sidebar.text_input("New Session ID", placeholder="e.g. session-user-1")
    if st.sidebar.button("➕ Create Session"):
        if new_sess_name.strip():
            repo.create_or_get_session(new_sess_name.strip())
            st.session_state["selected_session"] = new_sess_name.strip()
            st.sidebar.success(f"Session '{new_sess_name.strip()}' ready!")
            st.rerun()

    # Select session
    sessions = repo.list_sessions()
    session_ids = [s["session_id"] for s in sessions]

    if not session_ids:
        repo.create_or_get_session("session-default-1")
        sessions = repo.list_sessions()
        session_ids = [s["session_id"] for s in sessions]

    default_idx = 0
    if "selected_session" in st.session_state and st.session_state["selected_session"] in session_ids:
        default_idx = session_ids.index(st.session_state["selected_session"])

    current_session_id = st.sidebar.selectbox("Active Session", session_ids, index=default_idx)
    st.session_state["selected_session"] = current_session_id

    current_session = repo.get_session(current_session_id)
    st.sidebar.markdown(f"**Status:** `{current_session['status'] if current_session else 'unknown'}`")

    # Danger Zone: Delete All Sessions and Derived Memories
    st.sidebar.markdown("---")
    with st.sidebar.expander("🚨 Danger Zone — Reset All Sessions", expanded=False):
        st.markdown(
            "⚠️ **Permanently delete all sessions and their derived memories.**\n\n"
            "This removes all conversation history, extracted facts, assertions, evidence links, "
            "and audit logs. Database tables and schema will be preserved."
        )

        stats = repo.get_storage_stats()
        st.markdown(
            f"**Current Records:**\n"
            f"- Sessions: `{stats['sessions']}`\n"
            f"- Messages: `{stats['messages']}`\n"
            f"- Facts: `{stats['facts']}`\n"
            f"- Assertions: `{stats['assertions']}`\n"
            f"- Evidence: `{stats['evidence']}`\n"
            f"- Decisions: `{stats['decisions']}`"
        )

        confirm_text = st.text_input(
            "Type **DELETE ALL SESSIONS** to confirm:",
            placeholder="DELETE ALL SESSIONS",
            key="confirm_delete_all",
        )

        if st.button("🗑️ Delete All Sessions", type="primary", use_container_width=True):
            if confirm_text.strip() == "DELETE ALL SESSIONS":
                try:
                    del_counts = repo.delete_all_sessions()
                    st.session_state.pop("selected_session", None)
                    st.session_state.pop("confirm_delete_all", None)
                    st.sidebar.success(
                        f"Deleted {del_counts['sessions']} session(s) and all derived records!"
                    )
                    st.rerun()
                except Exception as e:
                    st.sidebar.error(f"Deletion failed and was rolled back: {e}")
            else:
                st.sidebar.error("Confirmation phrase does not match. Type 'DELETE ALL SESSIONS' exactly.")

    # Main Header
    st.markdown('<div class="main-header">CRAM Memory Inspector</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="sub-header">Active Session: <b>{current_session_id}</b> | '
        f'Persisted in SQLite | Extraction Engine: Google Gemini ({config.model_name})</div>',
        unsafe_allow_html=True,
    )

    # Tabs
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "💬 Messages & Ingestion",
        "📌 Active Facts",
        "📜 All Historical Assertions",
        "⚖️ Decision Audit Log",
        "🔍 Evidence & Provenance",
        "⚠️ Validation Failures",
    ])

    # ---------------- TAB 1: MESSAGES & INGESTION ----------------
    with tab1:
        col_msg_in, col_msg_list = st.columns([1, 1.2])

        with col_msg_in:
            st.subheader("Add Message")
            role = st.selectbox("Role", ["user", "assistant"], key="msg_role")
            content = st.text_area(
                "Message Content",
                height=100,
                placeholder="e.g. I live in Delhi and work at Google.",
                key="msg_content",
            )

            if st.button("💾 Save Message", use_container_width=True):
                if content.strip():
                    try:
                        ingestion_service = IngestionService(repository=repo)
                        msg = ingestion_service.ingest_message(
                            session_id=current_session_id,
                            role=role,
                            content=content,
                        )
                        st.success(f"Message `{msg.message_id[:8]}` (Turn {msg.turn_index}) saved!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to save message: {e}")
                else:
                    st.warning("Message content cannot be empty.")

            st.markdown("---")
            st.subheader("Or Import Multi-Turn Transcript")
            transcript = st.text_area(
                "Paste conversation (turns formatted as 'User: ...' or 'Assistant: ...')",
                height=130,
                placeholder="User: I started a project in 2024.\n\nAssistant: What is the project called?\n\nUser: CRAM is the name. It has two modules.",
                key="transcript_area",
            )
            if st.button("📥 Import Transcript", use_container_width=True):
                try:
                    ingestion_service = IngestionService(repository=repo)
                    imported_msgs = ingestion_service.ingest_transcript(
                        session_id=current_session_id,
                        transcript_text=transcript,
                    )
                    st.success(
                        f"Successfully imported {len(imported_msgs)} messages "
                        f"(Turns {imported_msgs[0].turn_index}–{imported_msgs[-1].turn_index}) "
                        f"into session `{current_session_id}`!"
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Transcript import failed: {e}")

        with col_msg_list:
            st.subheader("Stored Session Messages")
            messages = repo.get_session_messages(current_session_id)
            unprocessed = [m for m in messages if not m.is_processed]

            # Trigger Processing Button
            col_proc, col_stat = st.columns([1.5, 1])
            with col_proc:
                proc_btn = st.button(
                    f"⚡ Process Session ({len(unprocessed)} unprocessed)",
                    type="primary",
                    use_container_width=True,
                    disabled=len(unprocessed) == 0,
                )
            with col_stat:
                st.write(f"Total: **{len(messages)}** | Unprocessed: **{len(unprocessed)}**")

            if proc_btn:
                try:
                    with st.spinner("Extracting structured facts via Gemini and validating evidence..."):
                        processor = SessionProcessor(repository=repo)
                        summary = processor.process_session(current_session_id)
                    st.success(
                        f"Processing Complete! Created {summary.assertions_created} assertions across "
                        f"{summary.messages_processed} messages."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Processing error: {e}")

            # Message List
            if not messages:
                st.info("No messages in this session yet. Submit a message above.")
            else:
                for m in reversed(messages):
                    badge_color = "🟢 Processed" if m.is_processed else "🟡 Unprocessed"
                    st.markdown(
                        f"""
                        <div class="card">
                            <b>{m.role.upper()}</b> <span style="font-size:11px; color:#888;">({m.message_id[:8]}... | {badge_color})</span>
                            <p style="margin-top:6px; margin-bottom:0;">{m.content}</p>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

    # ---------------- TAB 2: ACTIVE FACTS ----------------
    with tab2:
        st.subheader("Active Conceptual Facts by Entity")
        entities = repo.list_entities()
        facts = repo.list_fact_identities()

        if not facts:
            st.info("No active facts have been created yet. Process a session with factual claims.")
        else:
            for ent in entities:
                ent_facts = [f for f in facts if f.entity_id == ent.entity_id]
                if not ent_facts:
                    continue
                with st.expander(f"Entity: **{ent.name}** ({ent.entity_type}) — {len(ent_facts)} facts", expanded=True):
                    for f in ent_facts:
                        active_ass = repo.get_assertion(f.current_assertion_id) if f.current_assertion_id else None
                        val_display = active_ass.raw_value if active_ass else "(No active assertion)"
                        temp_display = f" <i>[{active_ass.temporal_expression}]</i>" if (active_ass and active_ass.temporal_expression) else ""
                        st.markdown(
                            f"""
                            <div class="card">
                                <b>Attribute:</b> <code>{f.canonical_attribute}</code> (raw: {f.attribute})<br>
                                <b>Current Value:</b> <code>{val_display}</code>{temp_display}<br>
                                <span style="font-size:12px; color:#666;">Fact ID: <code>{f.fact_id[:8]}</code> | Active Assertion: <code>{f.current_assertion_id[:8] if f.current_assertion_id else 'None'}</code></span>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

    # ---------------- TAB 3: ALL HISTORICAL ASSERTIONS ----------------
    with tab3:
        st.subheader("Immutable Assertion History")
        all_assertions = repo.list_assertions()

        if not all_assertions:
            st.info("No assertions recorded yet.")
        else:
            badge_map = {
                "NEW": '<span class="badge badge-new">NEW</span>',
                "DUPLICATE": '<span class="badge badge-dup">DUPLICATE</span>',
                "UPDATE": '<span class="badge badge-update">UPDATE</span>',
                "CORRECTION": '<span class="badge badge-corr">CORRECTION</span>',
                "UNRESOLVED": '<span class="badge badge-unres">UNRESOLVED</span>',
            }
            for a in all_assertions:
                badge = badge_map.get(a.assertion_type, f'<span class="badge">{a.assertion_type}</span>')
                ent = repo.get_entity(a.entity_id)
                ent_name = ent.name if ent else "Unknown"
                supersedes = f" | Supersedes: <code>{a.supersedes_assertion_id[:8]}</code>" if a.supersedes_assertion_id else ""
                st.markdown(
                    f"""
                    <div class="card">
                        {badge} <b>{ent_name}</b> &bull; <code>{a.attribute}</code>: <b>{a.raw_value}</b><br>
                        <span style="font-size:12px; color:#555;">
                            Temporal: {a.temporal_expression or 'None'} | Session: <code>{a.session_id}</code> | Assertion ID: <code>{a.assertion_id[:8]}</code>{supersedes}
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    # ---------------- TAB 4: DECISION AUDIT LOG ----------------
    with tab4:
        st.subheader("Decision Audit Log")
        decisions = repo.list_decisions()
        if not decisions:
            st.info("No processing decisions recorded yet.")
        else:
            for d in decisions:
                matched_fact = f"Matched Fact: <code>{d.matched_fact_id[:8]}</code>" if d.matched_fact_id else "No prior fact matched"
                st.markdown(
                    f"""
                    <div class="card">
                        <b>Decision:</b> <code>{d.decision_type}</code> &bull; <b>Confidence:</b> {d.confidence:.2f}<br>
                        <b>Reason:</b> {d.reason}<br>
                        <span style="font-size:12px; color:#666;">
                            Session: <code>{d.session_id}</code> | {matched_fact} | Time: {d.created_at}
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    # ---------------- TAB 5: EVIDENCE & PROVENANCE ----------------
    with tab5:
        st.subheader("Evidence Quotes & Message Grounding")
        evidence_list = repo.list_all_evidence()
        if not evidence_list:
            st.info("No evidence records found.")
        else:
            for ev in evidence_list:
                match_str = "✅ Exact Substring" if ev.is_exact_match else "⚠️ Normalized/Partial"
                src_msg = repo.get_message(ev.message_id)
                full_msg_text = src_msg.content if src_msg else "(Message not found)"
                st.markdown(
                    f"""
                    <div class="card">
                        <b>Evidence Quote:</b> <i>"{ev.evidence_quote}"</i><br>
                        <b>Grounding Status:</b> <code>{ev.validation_status}</code> ({match_str})<br>
                        <b>Full Source Message:</b> <span style="color:#334155;">"{full_msg_text}"</span><br>
                        <span style="font-size:11px; color:#666;">
                            Assertion ID: <code>{ev.assertion_id[:8]}</code> | Source Message ID: <code>{ev.message_id[:8]}</code>
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    # ---------------- TAB 6: VALIDATION FAILURES ----------------
    with tab6:
        st.subheader("Logged Validation Failures & Ungrounded Claims")
        failures = repo.list_validation_failures()
        if not failures:
            st.success("No validation failures recorded. All accepted assertions are grounded in message evidence!")
        else:
            for f in failures:
                st.markdown(
                    f"""
                    <div class="card" style="border-left: 4px solid #EF4444;">
                        <b>Failure Reason:</b> <span style="color:#B91C1C;">{f.failure_reason}</span><br>
                        <b>Candidate JSON:</b> <code>{f.candidate_json}</code><br>
                        <span style="font-size:11px; color:#666;">
                            Session: <code>{f.session_id}</code> | Message ID: <code>{f.message_id or 'None'}</code> | Time: {f.created_at}
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )


if __name__ == "__main__":
    main()
