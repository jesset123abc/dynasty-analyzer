"""Supabase-backed memory store for the advisor.

Two tables:
- advisor_memories: curated facts the advisor saves via save_memory tool
- advisor_messages: full conversation log (every user/assistant turn)

If SUPABASE_URL or SUPABASE_KEY are not set, all functions degrade to no-ops
so the rest of the app keeps working.
"""
from __future__ import annotations
import os
from typing import Optional

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None
    Client = None

_client: Optional["Client"] = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if create_client is None:
        return None
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        _client = create_client(url, key)
        return _client
    except Exception:
        return None


def is_configured() -> bool:
    return _get_client() is not None


def save_memory(note: str, category: Optional[str] = None) -> bool:
    """Insert a curated memory. Returns True on success."""
    c = _get_client()
    if not c or not note:
        return False
    try:
        row = {"note": note.strip()}
        if category:
            row["category"] = category
        c.table("advisor_memories").insert(row).execute()
        return True
    except Exception:
        return False


def get_recent_memories(limit: int = 100) -> list[dict]:
    """Return most recent memories, newest first."""
    c = _get_client()
    if not c:
        return []
    try:
        res = (
            c.table("advisor_memories")
            .select("created_at, note, category")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def append_message(session_id: str, role: str, content: str) -> bool:
    c = _get_client()
    if not c or role not in ("user", "assistant") or not content:
        return False
    try:
        c.table("advisor_messages").insert({
            "session_id": session_id or "unknown",
            "role": role,
            "content": content.strip(),
        }).execute()
        return True
    except Exception:
        return False


def get_recent_messages(limit: int = 50) -> list[dict]:
    """Return most recent N messages across all sessions, chronological order."""
    c = _get_client()
    if not c:
        return []
    try:
        res = (
            c.table("advisor_messages")
            .select("created_at, session_id, role, content")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        rows.reverse()  # chronological
        return rows
    except Exception:
        return []


def format_memories_block(memories: list[dict]) -> str:
    if not memories:
        return ""
    lines = ["=== SAVED MEMORIES (newest first) ==="]
    for m in memories:
        ts = (m.get("created_at") or "")[:10]
        cat = f"[{m['category']}] " if m.get("category") else ""
        lines.append(f"- {ts} {cat}{m.get('note', '')}")
    return "\n".join(lines)


def load_draft_state() -> dict | None:
    """Return the persisted draft state from Supabase (single row, id=1)."""
    c = _get_client()
    if not c:
        return None
    try:
        res = c.table("draft_state").select("state").eq("id", 1).execute()
        rows = res.data or []
        if not rows:
            return None
        return rows[0].get("state")
    except Exception:
        return None


def save_draft_state(state: dict) -> bool:
    """Upsert the draft state into Supabase. Returns True on success."""
    c = _get_client()
    if not c or not isinstance(state, dict):
        return False
    try:
        c.table("draft_state").upsert({"id": 1, "state": state}).execute()
        return True
    except Exception:
        return False


def format_messages_block(messages: list[dict], current_session_id: str) -> str:
    """Format prior conversation history excluding the current session."""
    if not messages:
        return ""
    prior = [m for m in messages if m.get("session_id") != current_session_id]
    if not prior:
        return ""
    lines = ["=== PRIOR CONVERSATIONS (last 50 turns across all sessions) ==="]
    for m in prior:
        ts = (m.get("created_at") or "")[:16].replace("T", " ")
        role = m.get("role", "?").upper()
        content = (m.get("content") or "").strip()
        if len(content) > 500:
            content = content[:500] + "…"
        lines.append(f"[{ts}] {role}: {content}")
    return "\n".join(lines)
