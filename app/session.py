"""Per-chat session state for Cursor dialogue mode."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


def _topics_from_transcript(path: Path) -> tuple:
    first = ""
    last = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") != "user":
            continue
        message = obj.get("message") or {}
        content = message.get("content") or []
        texts = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
        elif isinstance(content, str):
            texts.append(content)
        raw = "\n".join(texts)
        match = _USER_QUERY_RE.search(raw)
        body = (match.group(1) if match else raw).strip()
        if not body or _SKIP_TOPIC_RE.match(body):
            continue
        hint = topic_hint(body)
        if not hint:
            continue
        if not first:
            first = hint
        last = hint
    return first, last


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_PROMPT_PREFIX_RE = re.compile(
    r"^工作目录是本机真实路径.*?不要使用沙箱或虚拟环境。\s*",
    re.DOTALL,
)
_SKIP_TOPIC_RE = re.compile(r"^Briefly inform the user about the task result\b", re.IGNORECASE)


def topic_hint(text: str, limit: int = 42) -> str:
    """One-line topic preview for /query_chat."""
    text = _PROMPT_PREFIX_RE.sub("", text or "")
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def new_chat_record(chat_id: str, workspace: Optional[str]) -> dict:
    ts = now_str()
    return {
        "chat_id": chat_id,
        "title": "",
        "latest": "",
        "workspace": workspace,
        "created_at": ts,
        "updated_at": ts,
    }


@dataclass
class ChatSession:
    """One Feishu chat. `chats` holds Cursor conversation records."""

    chat_id: str
    cursor_mode: bool = False
    workspace: Optional[str] = None
    agent_chat_id: Optional[str] = None
    mcp_allowed: bool = False
    mcp_workspace: Optional[str] = None
    chats: List[dict] = field(default_factory=list)

    def ensure_history(self) -> None:
        if self.agent_chat_id and not any(
            c.get("chat_id") == self.agent_chat_id for c in self.chats
        ):
            self.chats.append(new_chat_record(self.agent_chat_id, self.workspace))

    def find_chats(self, chat_id: str) -> List[dict]:
        key = (chat_id or "").strip()
        if not key:
            return []
        exact = [c for c in self.chats if c.get("chat_id") == key]
        if exact:
            return exact
        return [c for c in self.chats if str(c.get("chat_id", "")).startswith(key)]

    def latest_chat(self) -> Optional[dict]:
        if not self.chats:
            return None
        return max(
            self.chats,
            key=lambda c: (c.get("updated_at") or "", c.get("created_at") or ""),
        )

    def add_chat(self, chat_id: str, workspace: Optional[str]) -> dict:
        rec = new_chat_record(chat_id, workspace)
        self.chats.append(rec)
        self.agent_chat_id = chat_id
        return rec

    def touch(self, chat_id: str, *, title: Optional[str] = None, workspace: Optional[str] = None) -> None:
        for rec in self.chats:
            if rec.get("chat_id") != chat_id:
                continue
            rec["updated_at"] = now_str()
            if workspace is not None:
                rec["workspace"] = workspace
            hint = topic_hint(title or "")
            if hint:
                rec["latest"] = hint
                if not rec.get("title"):
                    rec["title"] = hint
            return

    def fill_missing_topics(self) -> bool:
        """Backfill topic hints from local Cursor transcripts."""
        changed = False
        root = Path.home() / ".cursor" / "projects"
        if not root.is_dir():
            return False
        for rec in self.chats:
            if rec.get("title") and rec.get("latest"):
                continue
            cid = rec.get("chat_id") or ""
            files = list(root.glob(f"*/agent-transcripts/{cid}/{cid}.jsonl"))
            if not files:
                continue
            path = max(files, key=lambda item: item.stat().st_mtime)
            first, last = _topics_from_transcript(path)
            if not first and not last:
                continue
            if not rec.get("title") and first:
                rec["title"] = first
                changed = True
            if not rec.get("latest") and (last or first):
                rec["latest"] = last or first
                changed = True
        return changed


@dataclass
class SessionStore:
    path: Path
    _sessions: Dict[str, ChatSession] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            loaded: Dict[str, ChatSession] = {}
            for chat_id, payload in data.items():
                if not isinstance(payload, dict):
                    continue
                chats = payload.get("chats") or []
                if not isinstance(chats, list):
                    chats = []
                sess = ChatSession(
                    chat_id=str(payload.get("chat_id") or chat_id),
                    cursor_mode=bool(payload.get("cursor_mode", False)),
                    workspace=payload.get("workspace"),
                    agent_chat_id=payload.get("agent_chat_id"),
                    mcp_allowed=bool(payload.get("mcp_allowed", False)),
                    mcp_workspace=payload.get("mcp_workspace"),
                    chats=[c for c in chats if isinstance(c, dict) and c.get("chat_id")],
                )
                sess.ensure_history()
                loaded[chat_id] = sess
            self._sessions = loaded

    def save(self) -> None:
        with self._lock:
            payload = {cid: asdict(sess) for cid, sess in self._sessions.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def sessions(self) -> List[ChatSession]:
        with self._lock:
            return list(self._sessions.values())

    def get(self, chat_id: str) -> ChatSession:
        with self._lock:
            sess = self._sessions.get(chat_id)
            if sess is None:
                sess = ChatSession(chat_id=chat_id)
                self._sessions[chat_id] = sess
            return sess

    def update(self, sess: ChatSession) -> None:
        with self._lock:
            self._sessions[sess.chat_id] = sess
            self.save()
