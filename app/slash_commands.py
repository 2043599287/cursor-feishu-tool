"""Sync Feishu input-box slash commands to the list in need.txt."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Dict, List

from .config import Config

logger = logging.getLogger(__name__)

_API = "https://open.feishu.cn/open-apis"
_SCOPE_HINT = (
    "请在飞书开放平台为该应用开通权限 application:app_slash_command:write "
    "和 application:app_slash_command:read，发布应用后再试。"
)

# command name (no leading slash) -> description shown after the command in Feishu.
SLASH_COMMANDS = [
    (
        "cursor",
        "进入 Cursor 智能体对话模式，使用 Auto Intelligence 模式。再次进入会续上当前对话和上下文。",
    ),
    (
        "cursor_used",
        "查询当前 Cursor 账户已使用的额度（计划百分比、按需花费等）。",
    ),
    (
        "exit",
        "退出 Cursor 智能体对话模式。已有对话记忆会保留，再次 /cursor 可续上。",
    ),
    (
        "bind",
        "绑定工作文件夹。用法：/bind {工作文件夹}。绑定后针对该目录下的工程开发。",
    ),
    (
        "unbind",
        "解绑当前工作文件夹。用法：/unbind {工作文件夹}。解绑后不再限定到该工程。",
    ),
    (
        "query_current_workspace",
        "查询当前绑定的工作文件夹。未绑定时说明默认使用 /home/uisee/workspace。",
    ),
    (
        "new_chat",
        "新建一段对话。旧对话仍保留，可用 /query_chat 查看并用 /enter_chat 跳回。",
    ),
    (
        "query_chat",
        "查询当前飞书会话下的全部对话，并标出正在使用的那一段。",
    ),
    (
        "enter_chat",
        "跳转到某一个对话。用法：/enter_chat {chatid}，可填完整 ID 或唯一前缀。",
    ),
    (
        "query_list",
        "查询当前绑定文件夹下的文件和子目录，均返回绝对路径。未绑定时列出 /home/uisee/workspace。",
    ),
    (
        "allow_mcp",
        "允许 Cursor 智能体只对当前已绑定的工作文件夹提交代码、建立 GitLab 仓库或分支。未绑定则直接拒绝。",
    ),
    (
        "forbid_mcp",
        "禁用 GitLab MCP。禁用后不允许提交代码、建立仓库或建立分支。",
    ),
]


def help_text() -> str:
    lines = ["可用指令："]
    for name, desc in SLASH_COMMANDS:
        lines.append(f"/{name}")
        lines.append(f"说明：{desc}")
        lines.append("")
    lines.append("进入 /cursor 后，直接发送自然语言即可调用本地 Cursor Agent。")
    return "\n".join(lines).strip()


def _request(method: str, url: str, token: str = "", body: dict = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"飞书接口 HTTP {exc.code}: {raw[:300]}") from exc
    if payload.get("code") not in (0, None):
        code = payload.get("code")
        msg = payload.get("msg")
        if code in (99991672, 99991663, 99991661):
            raise RuntimeError(f"飞书快捷指令同步失败 code={code} msg={msg}。{_SCOPE_HINT}")
        raise RuntimeError(f"飞书快捷指令同步失败 code={code} msg={msg}")
    return payload


def _tenant_token(cfg: Config) -> str:
    payload = _request(
        "POST",
        f"{_API}/auth/v3/tenant_access_token/internal",
        body={"app_id": cfg.app_id, "app_secret": cfg.app_secret},
    )
    token = payload.get("tenant_access_token") or ""
    if not token:
        raise RuntimeError("获取 tenant_access_token 失败")
    return token


def _list_commands(token: str) -> List[dict]:
    payload = _request("GET", f"{_API}/application/v7/app_slash_commands", token=token)
    data = payload.get("data") or {}
    items = data.get("items") or data.get("commands") or []
    return [item for item in items if isinstance(item, dict)]


def _description(text: str) -> dict:
    return {"default_value": text, "i18n": {"zh_cn": text}}


def sync_slash_commands(cfg: Config) -> None:
    """Replace the app's Feishu slash commands with SLASH_COMMANDS."""
    token = _tenant_token(cfg)
    existing = _list_commands(token)
    by_name: Dict[str, dict] = {}
    for item in existing:
        name = str(item.get("command") or "")
        if name:
            by_name[name] = item

    desired = {name: desc for name, desc in SLASH_COMMANDS}
    for name, desc in SLASH_COMMANDS:
        body = {"command": name, "description": _description(desc)}
        current = by_name.get(name)
        if current and current.get("command_id"):
            _request(
                "PATCH",
                f"{_API}/application/v7/app_slash_commands/{current['command_id']}",
                token=token,
                body=body,
            )
            logger.info("updated slash command /%s", name)
        else:
            _request(
                "POST",
                f"{_API}/application/v7/app_slash_commands",
                token=token,
                body=body,
            )
            logger.info("created slash command /%s", name)

    for name, item in by_name.items():
        if name in desired or not item.get("command_id"):
            continue
        _request(
            "DELETE",
            f"{_API}/application/v7/app_slash_commands/{item['command_id']}",
            token=token,
        )
        logger.info("removed slash command /%s", name)
