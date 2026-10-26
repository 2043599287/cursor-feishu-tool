"""Load runtime configuration from secret_config.yaml and environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Host workspace root (mounted into container as /workspace).
HOST_WORKSPACE = Path(os.environ.get("HOST_WORKSPACE", "/home/uisee/workspace"))
CONTAINER_WORKSPACE = Path(os.environ.get("CONTAINER_WORKSPACE", "/workspace"))


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    agent_bin: str
    cursor_api_key: str
    workspace_root: Path
    host_workspace: Path
    data_dir: Path
    agent_model: str
    agent_timeout: int
    gitlab_url: str
    gitlab_token: str


def _parse_secret_yaml(path: Path) -> dict:
    """Read a flat key: value YAML file. Values may be quoted."""
    data = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("#"):
            value = ""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        data[key] = value
    return data


def load_config() -> Config:
    project_root = Path(__file__).resolve().parent.parent
    secret_path = Path(os.environ.get("SECRET_CONFIG_FILE", project_root / "secret_config.yaml"))
    secrets = _parse_secret_yaml(secret_path)

    app_id = (os.environ.get("FEISHU_APP_ID") or secrets.get("feishu_app_id") or "").strip()
    app_secret = (os.environ.get("FEISHU_APP_SECRET") or secrets.get("feishu_app_secret") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError(
            "缺少飞书凭证：请在 "
            f"{secret_path} 中填写 feishu_app_id / feishu_app_secret"
        )

    # Use the host path when it is mounted, so the agent sees the same
    # directories as local Cursor (/home/uisee/workspace), not only /workspace.
    if HOST_WORKSPACE.is_dir():
        workspace_root = HOST_WORKSPACE
    elif CONTAINER_WORKSPACE.is_dir():
        workspace_root = CONTAINER_WORKSPACE
    else:
        workspace_root = HOST_WORKSPACE

    data_dir = Path(os.environ.get("DATA_DIR", project_root / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    gitlab_url = (secrets.get("gitlab_url") or "").strip().rstrip("/")
    gitlab_token = (secrets.get("gitlab_token") or "").strip()
    agent_model = (secrets.get("cursor_agent_model") or "auto").strip()
    agent_timeout = int(secrets.get("cursor_agent_timeout") or "1800")

    return Config(
        app_id=app_id,
        app_secret=app_secret,
        agent_bin=os.environ.get("CURSOR_AGENT_BIN", "agent"),
        cursor_api_key=(secrets.get("cursor_api_key") or "").strip(),
        workspace_root=workspace_root.resolve(),
        host_workspace=HOST_WORKSPACE.resolve(),
        data_dir=data_dir,
        agent_model=agent_model,
        agent_timeout=agent_timeout,
        gitlab_url=gitlab_url,
        gitlab_token=gitlab_token,
    )


def resolve_workspace_path(raw: str, cfg: Config) -> Path:
    """Map user bind path to a real directory under the workspace root."""
    raw = raw.strip().strip("'\"")
    if not raw:
        raise ValueError("工作文件夹路径不能为空")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cfg.workspace_root / path
    else:
        resolved = path.resolve()
        # /workspace/... is the container mount of the host workspace.
        try:
            rel = resolved.relative_to(CONTAINER_WORKSPACE)
            path = cfg.host_workspace / rel
        except ValueError:
            path = resolved

    path = path.resolve()
    try:
        path.relative_to(cfg.workspace_root)
    except ValueError as exc:
        raise ValueError(
            f"只能绑定 {cfg.workspace_root} 下的目录，收到: {path}"
        ) from exc

    if not path.is_dir():
        raise ValueError(f"目录不存在: {path}")
    return path
