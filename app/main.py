"""Feishu bot entry: long-connection events -> local Cursor Agent."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Optional

import lark_oapi as lark

from .commands import HELP_TEXT, parse_command
from .config import Config, load_config, resolve_workspace_path
from .cursor_agent import CursorAgent
from .feishu_bot import FeishuMessenger, extract_message_text
from .cursor_usage import fetch_cursor_usage
from .gitlab_mcp import clear_mcp_allow, ensure_cursor_mcp_entry, write_mcp_allow
from .session import SessionStore
from .slash_commands import sync_slash_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("feishu_cursor")


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = SessionStore(path=cfg.data_dir / "sessions.json")
        self.store.load()
        self._sync_mcp_grant()
        self.agent = CursorAgent(cfg)
        self.messenger = FeishuMessenger(cfg.app_id, cfg.app_secret)
        self._busy: set = set()
        self._busy_lock = threading.Lock()

    def _reply(self, message_id: str, chat_id: str, text: str) -> None:
        try:
            if message_id:
                self.messenger.reply_text(message_id, text)
            else:
                self.messenger.send_text(chat_id, text)
        except Exception:
            logger.exception("reply failed chat_id=%s", chat_id)

    def handle_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        message_type: Optional[str],
    ) -> None:
        # Dedup / serialize per chat so Feishu 3s retries don't double-run agent.
        with self._busy_lock:
            if chat_id in self._busy:
                self._reply(message_id, chat_id, "上一条任务还在执行，请稍候…")
                return
            self._busy.add(chat_id)

        try:
            self._handle_unlocked(chat_id=chat_id, message_id=message_id, text=text, message_type=message_type)
        finally:
            with self._busy_lock:
                self._busy.discard(chat_id)

    def _handle_unlocked(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        message_type: Optional[str],
    ) -> None:
        if message_type and message_type != "text":
            self._reply(message_id, chat_id, f"暂不支持的消息类型: {message_type}，请发送文本。")
            return

        raw = extract_message_text(text, message_type)
        cmd = parse_command(raw)
        sess = self.store.get(chat_id)

        if cmd.name == "help":
            self._reply(message_id, chat_id, HELP_TEXT)
            return

        if cmd.name == "none":
            if not sess.cursor_mode:
                self._reply(message_id, chat_id, HELP_TEXT)
            return

        if cmd.name == "cursor_used":
            self._reply(message_id, chat_id, fetch_cursor_usage(self.cfg))
            return

        if cmd.name == "cursor":
            resumed = bool(sess.agent_chat_id)
            if not sess.agent_chat_id:
                latest = sess.latest_chat()
                if latest:
                    sess.agent_chat_id = latest["chat_id"]
                    resumed = True
            if not sess.agent_chat_id:
                try:
                    created = self.agent.create_chat()
                except Exception as exc:
                    self._reply(message_id, chat_id, f"进入 Cursor 模式失败: {exc}")
                    return
                sess.add_chat(created, sess.workspace)
                resumed = False
            sess.cursor_mode = True
            self.store.update(sess)
            ws = sess.workspace or str(self.cfg.workspace_root)
            action = "已续上当前对话" if resumed else "已新建对话"
            self._reply(
                message_id,
                chat_id,
                f"已进入 Cursor 智能体对话模式（model={self.cfg.agent_model}）\n"
                f"{action}: {sess.agent_chat_id}\n"
                f"当前工作目录: {ws}\n"
                "直接发送任务内容即可；使用 /exit 退出（记忆会保留）。",
            )
            return

        if cmd.name == "exit":
            sess.cursor_mode = False
            self.store.update(sess)
            current = sess.agent_chat_id or "（无）"
            self._reply(
                message_id,
                chat_id,
                f"已退出 Cursor 智能体对话模式。\n当前对话仍保留: {current}\n再次 /cursor 会续上这段上下文。",
            )
            return

        if cmd.name == "new_chat":
            try:
                created = self.agent.create_chat()
            except Exception as exc:
                self._reply(message_id, chat_id, f"新建对话失败: {exc}")
                return
            sess.add_chat(created, sess.workspace)
            sess.cursor_mode = True
            self.store.update(sess)
            self._reply(
                message_id,
                chat_id,
                f"已新建对话并进入: {created}\n之前的对话仍可用 /query_chat 与 /enter_chat 找回。",
            )
            return

        if cmd.name == "query_chat":
            if sess.fill_missing_topics():
                self.store.update(sess)
            self._reply(message_id, chat_id, self._format_chats(sess))
            return

        if cmd.name == "query_current_workspace":
            if sess.workspace:
                self._reply(message_id, chat_id, f"当前工作文件夹:\n{sess.workspace}")
            else:
                self._reply(
                    message_id,
                    chat_id,
                    f"当前未绑定工作文件夹。\n默认目录:\n{self.cfg.workspace_root}",
                )
            return

        if cmd.name == "query_list":
            folder = Path(sess.workspace) if sess.workspace else self.cfg.workspace_root
            self._reply(message_id, chat_id, self._format_dir(folder))
            return

        if cmd.name == "allow_mcp":
            if not sess.workspace:
                self._reply(message_id, chat_id, "不允许：请先 /bind 工作文件夹。MCP 只能用于当前绑定的代码目录。")
                return
            if not self.cfg.gitlab_url or not self.cfg.gitlab_token:
                self._reply(message_id, chat_id, "不允许：未读到 GitLab 地址或访问令牌。")
                return
            try:
                path = resolve_workspace_path(sess.workspace, self.cfg)
            except ValueError as exc:
                self._reply(message_id, chat_id, f"不允许：{exc}")
                return
            sess.mcp_allowed = True
            sess.mcp_workspace = str(path)
            self.store.update(sess)
            self._sync_mcp_grant()
            self._reply(
                message_id,
                chat_id,
                f"已允许 GitLab MCP，仅限当前绑定目录:\n{path}\n"
                "只能在该目录提交代码、建立仓库或建立分支。其它路径会直接拒绝。",
            )
            return

        if cmd.name == "forbid_mcp":
            sess.mcp_allowed = False
            sess.mcp_workspace = None
            self.store.update(sess)
            self._sync_mcp_grant()
            self._reply(message_id, chat_id, "已禁用 GitLab MCP。提交、建仓库、建分支均不允许。")
            return

        if cmd.name == "enter_chat":
            if not cmd.arg:
                self._reply(message_id, chat_id, "用法: /enter_chat {chatid}")
                return
            matches = sess.find_chats(cmd.arg)
            if not matches:
                self._reply(message_id, chat_id, f"未找到对话: {cmd.arg}\n\n" + self._format_chats(sess))
                return
            if len(matches) > 1:
                lines = ["匹配到多段对话，请给更完整的 chatid:"]
                for rec in matches:
                    topic = rec.get("title") or "（还没有主题）"
                    lines.append(f"{rec.get('chat_id')}\n主题：{topic}")
                self._reply(message_id, chat_id, "\n".join(lines))
                return
            chosen = matches[0]
            sess.agent_chat_id = chosen["chat_id"]
            sess.cursor_mode = True
            if chosen.get("workspace") and chosen.get("workspace") != sess.workspace:
                sess.workspace = chosen["workspace"]
                sess.mcp_allowed = False
                sess.mcp_workspace = None
                self._sync_mcp_grant()
            elif chosen.get("workspace"):
                sess.workspace = chosen["workspace"]
            self.store.update(sess)
            ws = sess.workspace or str(self.cfg.workspace_root)
            topic = chosen.get("title") or "（还没有主题）"
            self._reply(
                message_id,
                chat_id,
                f"已跳转到对话: {chosen['chat_id']}\n主题：{topic}\n工作目录: {ws}",
            )
            return

        if cmd.name == "bind":
            try:
                path = resolve_workspace_path(cmd.arg, self.cfg)
            except ValueError as exc:
                self._reply(message_id, chat_id, f"绑定失败: {exc}")
                return
            changed = sess.mcp_workspace and str(path) != sess.mcp_workspace
            sess.workspace = str(path)
            if changed or not sess.mcp_allowed:
                sess.mcp_allowed = False
                sess.mcp_workspace = None
                self._sync_mcp_grant()
            if sess.agent_chat_id:
                sess.touch(sess.agent_chat_id, workspace=sess.workspace)
            self.store.update(sess)
            extra = "\n绑定目录已变化，GitLab MCP 已关闭。需要时请重新 /allow_mcp。" if changed else ""
            self._reply(message_id, chat_id, f"已绑定工作文件夹:\n{path}\n当前对话记忆保持不变。{extra}")
            return

        if cmd.name == "unbind":
            if cmd.arg:
                try:
                    path = resolve_workspace_path(cmd.arg, self.cfg)
                except ValueError as exc:
                    self._reply(message_id, chat_id, f"解绑失败: {exc}")
                    return
                if sess.workspace and Path(sess.workspace).resolve() != path.resolve():
                    self._reply(
                        message_id,
                        chat_id,
                        f"当前绑定的是 {sess.workspace}，与指定路径不一致。",
                    )
                    return
            if not sess.workspace:
                self._reply(message_id, chat_id, "当前没有绑定工作文件夹。")
                return
            old = sess.workspace
            sess.workspace = None
            sess.mcp_allowed = False
            sess.mcp_workspace = None
            self.store.update(sess)
            self._sync_mcp_grant()
            self._reply(
                message_id,
                chat_id,
                f"已解绑工作文件夹:\n{old}\n当前对话记忆保持不变。\nGitLab MCP 已关闭。",
            )
            return

        # Normal prompt
        if not sess.cursor_mode:
            self._reply(
                message_id,
                chat_id,
                "尚未进入 Cursor 模式。请先发送 /cursor。\n\n" + HELP_TEXT,
            )
            return

        workspace = Path(sess.workspace) if sess.workspace else self.cfg.workspace_root
        if not sess.agent_chat_id:
            try:
                sess.agent_chat_id = self.agent.create_chat()
                self.store.update(sess)
            except Exception as exc:
                self._reply(message_id, chat_id, f"创建会话失败: {exc}")
                return

        mcp_workspace = self._allowed_mcp_workspace(sess, workspace)
        notice = f"正在调用 Cursor Agent…\n工作目录: {workspace}"
        if mcp_workspace:
            notice += f"\nGitLab MCP: 仅 {mcp_workspace}"
        self._reply(message_id, chat_id, notice)
        result = self.agent.prompt(
            cmd.arg,
            workspace=workspace,
            chat_id=sess.agent_chat_id,
            mcp_workspace=mcp_workspace,
        )
        if result.chat_id and result.chat_id != sess.agent_chat_id:
            sess.agent_chat_id = result.chat_id
            sess.ensure_history()
        sess.touch(sess.agent_chat_id or "", title=cmd.arg, workspace=str(workspace))
        self.store.update(sess)

        header = "✅ Cursor 结果:\n" if result.ok else "❌ Cursor 失败:\n"
        self.messenger.send_text(chat_id, header + result.text)

    def _sync_mcp_grant(self) -> None:
        """Publish the allow file the MCP process can read without inherited env."""
        workspaces = []
        for sess in self.store.sessions():
            if sess.mcp_allowed and sess.mcp_workspace:
                workspaces.append(sess.mcp_workspace)
        unique = list(dict.fromkeys(workspaces))
        if len(unique) == 1:
            write_mcp_allow(Path(unique[0]), self.cfg.host_workspace)
        else:
            clear_mcp_allow()

    def _allowed_mcp_workspace(self, sess, workspace: Path) -> Optional[Path]:
        if not sess.mcp_allowed or not sess.mcp_workspace:
            return None
        try:
            allowed = Path(sess.mcp_workspace).resolve()
        except OSError:
            return None
        if allowed != workspace.resolve():
            return None
        return allowed

    def _format_dir(self, folder: Path) -> str:
        folder = folder.resolve()
        if not folder.is_dir():
            return f"目录不存在: {folder}"
        try:
            entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            return f"读取目录失败: {folder}\n{exc}"
        if not entries:
            return f"{folder} 下没有文件。"
        lines = [f"{folder} 下的内容（绝对路径）:"]
        for item in entries:
            path = str(item.resolve())
            if item.is_dir():
                path += "/"
            lines.append(path)
        return "\n".join(lines)

    def _format_chats(self, sess) -> str:
        if not sess.chats:
            return "还没有对话。发送 /cursor 或 /new_chat 创建。"
        lines = ["对话列表:"]
        for i, rec in enumerate(sess.chats, start=1):
            cid = rec.get("chat_id") or ""
            mark = "  ← 当前" if cid == sess.agent_chat_id else ""
            topic = rec.get("title") or "（还没有主题，这段对话尚未提问）"
            latest = rec.get("latest") or ""
            when = rec.get("updated_at") or rec.get("created_at") or ""
            ws = rec.get("workspace") or "（未绑定）"
            block = f"{i}. {cid}{mark}\n   主题：{topic}"
            if latest and latest != topic:
                block += f"\n   最近：{latest}"
            block += f"\n   {when}  {ws}"
            lines.append(block)
        lines.append("跳转: /enter_chat {chatid}")
        return "\n".join(lines)


def build_event_handler(app: App):
    def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        try:
            event = data.event
            message = event.message
            chat_id = message.chat_id or ""
            message_id = message.message_id or ""
            msg_type = message.message_type
            content = message.content or ""

            # Ignore bot's own messages if sender type is available.
            sender = event.sender
            if sender and getattr(sender, "sender_type", None) == "app":
                return

            logger.info("message chat=%s type=%s id=%s", chat_id, msg_type, message_id)

            # Must return quickly (<3s); heavy work in background.
            threading.Thread(
                target=app.handle_message,
                kwargs={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": content,
                    "message_type": msg_type,
                },
                daemon=True,
                name=f"chat-{chat_id[:8]}",
            ).start()
        except Exception:
            logger.exception("on_message error")

    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )


def main() -> None:
    try:
        cfg = load_config()
    except Exception as exc:
        logger.error("%s", exc)
        sys.exit(1)

    try:
        ensure_cursor_mcp_entry()
    except Exception as exc:
        logger.warning("GitLab MCP 未写入 Cursor 配置: %s", exc)

    try:
        sync_slash_commands(cfg)
    except Exception as exc:
        logger.warning("飞书快捷指令未同步: %s", exc)

    app = App(cfg)
    handler = build_event_handler(app)
    cli = lark.ws.Client(
        cfg.app_id,
        cfg.app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    logger.info(
        "starting Feishu WS bot, workspace_root=%s model=%s agent=%s",
        cfg.workspace_root,
        cfg.agent_model,
        cfg.agent_bin,
    )
    cli.start()


if __name__ == "__main__":
    main()
