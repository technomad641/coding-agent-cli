"""
session_store.py
-----------------
Saves and loads a conversation - the `messages` list `main.py` builds up -
so a session can be resumed after the process exits. This is what's
missing to close the "No persistence" item in the README's Known
limitations.

One file per conversation, in `sessions/<id>.json` (gitignored - this is
your conversation history, not source). The id is `observability.SESSION_ID`
from whichever process *started* the conversation - so a resumed session
keeps its original id as its filename even though the resuming process has
its own, different SESSION_ID for its own new observability events. See
main.py for how the two ids relate.

The tricky part isn't the file I/O, it's that `message.content` from the
API is a list of typed SDK objects (TextBlock, ToolUseBlock, ...), not
plain dicts - json.dumps() can't serialize those directly. main.py already
converts them with `message.model_dump(mode="json")["content"]` before
appending to `messages`, specifically so this file can treat the whole
list as plain, boring, JSON-serializable data and never has to know
anything about the Anthropic SDK's types.
"""

import json
import time
from pathlib import Path

SESSIONS_DIR = Path("sessions")


def _path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def save_session(session_id: str, model: str, root: Path, messages: list[dict], turn_count: int) -> None:
    """Overwrite sessions/<session_id>.json with the current conversation.

    Called once per completed turn (see main.py) - never mid-turn - so a
    saved file is always in a state that's safe to resume from: no
    tool_use block without a matching tool_result already appended after it.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(session_id)

    # Preserve the original created_at across repeated saves (and across a
    # later resume, which calls save_session again) - it's read back once
    # here rather than tracked separately in main.py.
    created_at = time.time()
    if path.exists():
        try:
            created_at = json.loads(path.read_text()).get("created_at", created_at)
        except (json.JSONDecodeError, OSError):
            pass  # a corrupt prior save shouldn't block writing a fresh one

    record = {
        "session_id": session_id,
        "model": model,
        "root": str(root),
        "created_at": created_at,
        "updated_at": time.time(),
        "turn_count": turn_count,
        "messages": messages,
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def load_session(session_id: str) -> dict:
    """Load one saved conversation. Raises FileNotFoundError (with the path
    in the message) if there's no session by that id - let the caller
    decide how to report that, rather than printing here."""
    path = _path(session_id)
    if not path.exists():
        raise FileNotFoundError(f"No saved session at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_sessions() -> list[dict]:
    """Metadata for every saved session, most recently updated first.
    Doesn't load `messages` for sessions other than what's asked for -
    just enough to display a picklist."""
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # skip a corrupt file rather than crashing --list
        sessions.append(
            {
                "session_id": data.get("session_id", path.stem),
                "model": data.get("model"),
                "root": data.get("root"),
                "updated_at": data.get("updated_at", 0),
                "turn_count": data.get("turn_count", 0),
            }
        )
    return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)


def most_recent_session_id() -> str | None:
    sessions = list_sessions()
    return sessions[0]["session_id"] if sessions else None
