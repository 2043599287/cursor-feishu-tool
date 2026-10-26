"""Wrap the local Cursor Agent CLI (`agent`)."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    ok: bool
    text: str
    chat_id: Optional[str] = None
    returncode: int = 0


class CursorAgent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _base_env(self) -> dict:
        env = os.environ.copy()
        if self.cfg.cursor_api_key:
            env["CURSOR_API_KEY"] = self.cfg.cursor_api_key
        # Non-interactive: avoid browser login prompts inside container.
        env.setdefault("NO_OPEN_BROWSER", "1")
        return env

    def _run(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        extra_env: Optional[dict] = None,
    ) -> subprocess.CompletedProcess:
        cmd = [self.cfg.agent_bin, *args]
        logger.info("run agent: %s (cwd=%s)", " ".join(cmd), cwd)
        env = self._base_env()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.cfg.agent_timeout,
        )

    def create_chat(self) -> str:
        proc = self._run(["create-chat"])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"创建 Cursor 会话失败: {err or proc.returncode}")
        chat_id = (proc.stdout or "").strip().splitlines()[-1].strip()
        if not chat_id:
            raise RuntimeError("创建 Cursor 会话失败: 空 chat id")
        return chat_id

    def prompt(
        self,
        prompt: str,
        *,
        workspace: Optional[Path],
        chat_id: Optional[str],
        mcp_workspace: Optional[Path] = None,
    ) -> AgentResult:
        cwd = workspace if workspace and workspace.is_dir() else self.cfg.workspace_root
        mcp_on = bool(
            mcp_workspace
            and mcp_workspace.is_dir()
            and mcp_workspace.resolve() == cwd.resolve()
            and self.cfg.gitlab_token
            and self.cfg.gitlab_url
        )
        args: List[str] = [
            "--print",
            "--output-format",
            "text",
            "--model",
            self.cfg.agent_model,
            "--trust",
            "--force",
            "--sandbox",
            "disabled",
            "--workspace",
            str(cwd),
        ]
        if mcp_on:
            args.append("--approve-mcps")
        if chat_id:
            args.extend(["--resume", chat_id])
        if mcp_on:
            rule = (
                f"GitLab MCP 已允许，且只能操作当前绑定目录 {cwd}。"
                "提交、建仓库、建分支只能调用 gitlab-bound 工具。"
                "其它路径或其它项目直接拒绝，不要用 git 或 API 绕过。"
            )
        else:
            rule = (
                "GitLab MCP 未允许。禁止创建仓库、创建分支、提交或推送代码。"
                "若用户提出这些操作，直接回复：不允许，请先 /bind 目标目录并执行 /allow_mcp。"
            )
        args.append(
            f"工作目录是本机真实路径 {cwd}，与宿主机同一份文件，不要使用沙箱或虚拟环境。\n"
            f"{rule}\n\n{prompt}"
        )

        try:
            proc = self._run(args, cwd=cwd)
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, text=f"Cursor Agent 超时（>{self.cfg.agent_timeout}s）", chat_id=chat_id)
        except FileNotFoundError:
            return AgentResult(
                ok=False,
                text=(
                    f"未找到 Cursor Agent 可执行文件: {self.cfg.agent_bin}\n"
                    "请安装 agent CLI，或设置 CURSOR_AGENT_BIN / 挂载 host agent。"
                ),
                chat_id=chat_id,
            )

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            detail = stderr or stdout or f"exit={proc.returncode}"
            return AgentResult(ok=False, text=f"Cursor Agent 执行失败:\n{detail}", chat_id=chat_id, returncode=proc.returncode)

        text = stdout or "(Cursor Agent 无文本输出)"
        return AgentResult(ok=True, text=text, chat_id=chat_id, returncode=0)
