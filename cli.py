import argparse
from cram.config import config
from cram.db.repository import Repository
from cram.db.schema import initialize_database
from cram.pipeline.session_processor import SessionProcessor
from cram.pipeline.ingestion import IngestionService, TranscriptParseError

def main():
    parser = argparse.ArgumentParser(description="CRAM CLI - Context Resolution in AI Memory")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: init
    subparsers.add_parser("init", help="Initialize the database")

    # Command: add-message
    msg_parser = subparsers.add_parser("add-message", help="Add a message to a session")
    msg_parser.add_argument("--session", required=True, help="Session ID")
    msg_parser.add_argument("--role", default="user", choices=["user", "assistant"], help="Role")
    msg_parser.add_argument("--content", required=True, help="Message content text")

    # Command: import-transcript
    trans_parser = subparsers.add_parser("import-transcript", help="Import a multi-turn transcript into a session")
    trans_parser.add_argument("--session", required=True, help="Session ID")
    trans_parser.add_argument("--file", help="Path to transcript file")
    trans_parser.add_argument("--text", help="Transcript text string")

    # Command: process
    proc_parser = subparsers.add_parser("process", help="Process unprocessed messages in a session")
    proc_parser.add_argument("--session", required=True, help="Session ID to process")

    # Command: list-facts
    subparsers.add_parser("list-facts", help="List all active facts")

    # Command: list-assertions
    ass_parser = subparsers.add_parser("list-assertions", help="List all assertions")
    ass_parser.add_argument("--session", help="Filter by session ID")

    # Command: list-decisions
    dec_parser = subparsers.add_parser("list-decisions", help="List all processing decisions")
    dec_parser.add_argument("--session", help="Filter by session ID")

    # Command: delete-all-sessions
    del_parser = subparsers.add_parser("delete-all-sessions", help="Delete all sessions and derived memories")
    del_parser.add_argument("--confirm", required=True, help="Confirmation phrase: must be 'DELETE ALL SESSIONS'")

    args = parser.parse_args()

    initialize_database(config.db_path)
    repo = Repository(config.db_path)

    if args.command == "init":
        print(f"Database initialized at {config.db_path}")

    elif args.command == "add-message":
        service = IngestionService(repository=repo)
        msg = service.ingest_message(session_id=args.session, role=args.role, content=args.content)
        print(f"Message persisted: ID={msg.message_id} Session={msg.session_id} Turn={msg.turn_index} Role={msg.role}")

    elif args.command == "import-transcript":
        service = IngestionService(repository=repo)
        if args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                transcript_text = f.read()
        elif args.text:
            transcript_text = args.text
        else:
            print("Error: Either --file or --text must be provided for import-transcript.")
            return
        try:
            msgs = service.ingest_transcript(session_id=args.session, transcript_text=transcript_text)
            print(f"Successfully imported {len(msgs)} messages (Turns {msgs[0].turn_index}–{msgs[-1].turn_index}) into session '{args.session}'.")
        except Exception as e:
            print(f"Transcript import failed: {e}")

    elif args.command == "delete-all-sessions":
        if args.confirm == "DELETE ALL SESSIONS":
            counts = repo.delete_all_sessions()
            print(f"Successfully deleted {counts['sessions']} session(s) and all derived memory records.")
            for k, v in counts.items():
                print(f"  - {k}: {v} deleted")
        else:
            print("Error: Confirmation phrase does not match. You must pass --confirm 'DELETE ALL SESSIONS'.")

    elif args.command == "process":
        processor = SessionProcessor(repository=repo)
        print(f"Processing session '{args.session}'...")
        summary = processor.process_session(args.session)
        print(f"Done! Processed {summary.messages_processed} messages, created {summary.assertions_created} assertions.")
        print("Decisions breakdown:", summary.decisions_count)
        if summary.validation_failures_count > 0:
            print(f"Validation failures: {summary.validation_failures_count}")

    elif args.command == "list-facts":
        facts = repo.list_fact_identities()
        entities = {e.entity_id: e for e in repo.list_entities()}
        print(f"\n--- ACTIVE FACTS ({len(facts)}) ---")
        for f in facts:
            ent = entities.get(f.entity_id)
            ent_name = ent.name if ent else "Unknown"
            ass = repo.get_assertion(f.current_assertion_id) if f.current_assertion_id else None
            val = ass.raw_value if ass else "None"
            print(f"Entity: {ent_name:<12} | Attr: {f.canonical_attribute:<15} | Active Value: {val}")

    elif args.command == "list-assertions":
        assertions = repo.list_assertions(session_id=args.session)
        print(f"\n--- HISTORICAL ASSERTIONS ({len(assertions)}) ---")
        for a in assertions:
            sup = f" (supersedes {a.supersedes_assertion_id[:8]})" if a.supersedes_assertion_id else ""
            print(f"[{a.assertion_type:<10}] {a.attribute}: {a.raw_value}{sup} [Session: {a.session_id}]")

    elif args.command == "list-decisions":
        decisions = repo.list_decisions(session_id=args.session)
        print(f"\n--- DECISION AUDIT LOG ({len(decisions)}) ---")
        for d in decisions:
            print(f"[{d.decision_type:<10}] {d.reason} (conf: {d.confidence})")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
