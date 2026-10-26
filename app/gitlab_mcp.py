"""GitLab MCP limited to the workspace enabled by /allow_mcp.

Stdio JSON-RPC server started by Cursor Agent. Cursor launches this process
with only HOME and PATH, so the grant is a file under data/, not the environment.
The GitLab token stays in secret_config.yaml and is never written to mcp.json.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def deny(reason: str) -> None:
    raise PermissionError(reason)


def _allow_state_paths() -> list:
    """Locations the bot and the MCP process can both see.

    In the container, /app/data and the project data/ directory are the same
    bind mount. Cursor does not forward DATA_DIR into this process.
    """
    script_data = Path(__file__).resolve().parent.parent / "data" / "mcp_allow.json"
    raw = [
        script_data,
        Path("/home/uisee/workspace/test/feishu_cursor/data/mcp_allow.json"),
        Path("/app/data/mcp_allow.json"),
    ]
    paths = []
    seen = set()
    for path in raw:
        key = os.path.normpath(str(path))
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _read_grant() -> Optional[dict]:
    for path in _allow_state_paths():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict) and data.get("enabled") and data.get("workspace"):
            return data
        return None
    if _env("GITLAB_MCP_ENABLED") == "1" and _env("BOUND_WORKSPACE"):
        return {
            "enabled": True,
            "workspace": _env("BOUND_WORKSPACE"),
            "host_workspace": _env("HOST_WORKSPACE") or "/home/uisee/workspace",
        }
    return None


def write_mcp_allow(workspace: Path, host_workspace: Path) -> None:
    payload = json.dumps(
        {
            "enabled": True,
            "workspace": str(workspace.resolve()),
            "host_workspace": str(host_workspace.resolve()),
        },
        ensure_ascii=False,
    )
    written = False
    for path in _allow_state_paths():
        if not path.parent.is_dir():
            continue
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        written = True
    if not written:
        path = Path(__file__).resolve().parent.parent / "data" / "mcp_allow.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        os.chmod(path, 0o600)


def clear_mcp_allow() -> None:
    for path in _allow_state_paths():
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue


def bound_root() -> Path:
    grant = _read_grant()
    if not grant:
        deny("不允许：尚未执行 /allow_mcp，已禁用 GitLab 提交、建仓库和建分支。")
    raw = str(grant.get("workspace") or "").strip()
    if not raw:
        deny("不允许：未绑定工作文件夹。请先 /bind，再 /allow_mcp。")
    root = Path(raw).resolve()
    host = Path(str(grant.get("host_workspace") or "/home/uisee/workspace")).resolve()
    try:
        root.relative_to(host)
    except ValueError:
        deny(f"不允许：{root} 不在 {host} 内。")
    if not root.is_dir():
        deny(f"不允许：绑定目录不存在 {root}")
    return root


def _parse_flat_yaml(path: Path) -> dict:
    data = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("---") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        data[key.strip()] = value
    return data


def _load_secrets() -> dict:
    candidates = []
    configured = _env("SECRET_CONFIG_FILE")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path("/app/secret_config.yaml"),
            Path(__file__).resolve().parent.parent / "secret_config.yaml",
            Path("/home/uisee/workspace/test/feishu_cursor/secret_config.yaml"),
        ]
    )
    seen = set()
    for path in candidates:
        key = os.path.normpath(str(path))
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        data = _parse_flat_yaml(path)
        if data.get("gitlab_url") or data.get("gitlab_token") or data.get("git_author_name"):
            return data
    return {}


def _gitlab_settings() -> tuple:
    data = _load_secrets()
    url = (data.get("gitlab_url") or _env("GITLAB_URL")).strip().rstrip("/")
    token = (data.get("gitlab_token") or _env("GITLAB_TOKEN")).strip()
    return url, token


def _git_author() -> tuple:
    data = _load_secrets()
    name = (data.get("git_author_name") or "").strip()
    email = (data.get("git_author_email") or "").strip()
    if not name or "@" not in email:
        deny("不允许：请在 secret_config.yaml 配置 git_author_name 和 git_author_email。")
    return name, email


def gitlab_api_base() -> str:
    url, _token_value = _gitlab_settings()
    if not url:
        deny("不允许：未配置 GitLab 地址。")
    if url.endswith("/api/v4"):
        return url
    return url + "/api/v4"


def gitlab_host() -> str:
    url, _token_value = _gitlab_settings()
    if url.endswith("/api/v4"):
        url = url[: -len("/api/v4")]
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc


def _token() -> str:
    _url, token = _gitlab_settings()
    if not token:
        deny("不允许：未配置 GitLab 访问令牌。")
    return token


def redact(text: str) -> str:
    _url, token = _gitlab_settings()
    if token:
        text = text.replace(token, "***")
        text = text.replace(urllib.parse.quote(token, safe=""), "***")
    return re.sub(r"://[^/\s:]+:[^@\s/]+@", "://***@", text)


def api(method: str, path: str, body: Optional[dict] = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        gitlab_api_base() + path,
        data=data,
        method=method,
        headers={
            "PRIVATE-TOKEN": _token(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        deny(f"GitLab API {exc.code}: {redact(detail)[:500]}")
    if not raw:
        return {}
    payload = json.loads(raw)
    if isinstance(payload, dict):
        return payload
    return {"items": payload}


def git(root: Path, args: list, check: bool = True, http_auth: bool = True) -> subprocess.CompletedProcess:
    author_name, author_email = _git_author()
    cmd = [
        "git",
        "-C",
        str(root),
        "-c",
        "safe.directory={0}".format(root),
        "-c",
        "user.name={0}".format(author_name),
        "-c",
        "user.email={0}".format(author_email),
        "-c",
        "credential.helper=",
    ]
    if http_auth:
        cmd.extend(["-c", "http.extraHeader=Authorization: Bearer {0}".format(_token())])
    cmd.extend(args)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    if check and proc.returncode != 0:
        detail = redact((proc.stderr or proc.stdout or "").strip())
        deny(detail or redact("git {0} 失败".format(" ".join(args))))
    return proc


def git_root(root: Path) -> Path:
    proc = git(root, ["rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        deny("不允许：绑定目录还不是 git 仓库。请先建立代码仓库。")
    top = Path((proc.stdout or "").strip()).resolve()
    if top != root:
        deny(f"不允许：git 仓库根是 {top}，与当前绑定目录 {root} 不一致。")
    return top


def origin_url(root: Path) -> str:
    proc = git(root, ["remote", "get-url", "origin"], check=False)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def assert_origin(root: Path) -> str:
    url = origin_url(root)
    if not url:
        deny("不允许：绑定目录没有 origin。请先建立代码仓库。")
    host = gitlab_host()
    if host not in url:
        deny(f"不允许：origin 不属于 {host}，拒绝操作。")
    return url


def https_push_url(origin: str) -> str:
    """Turn SSH or HTTPS origin into an HTTPS URL. The token is not embedded."""
    host = gitlab_host()
    text = origin.strip()
    path = ""
    ssh_sc = "git@" + host + ":"
    ssh_url = "ssh://git@" + host + "/"
    if text.startswith(ssh_sc):
        path = text[len(ssh_sc):].lstrip("/")
    elif text.startswith(ssh_url):
        path = text[len(ssh_url):].lstrip("/")
    else:
        parsed = urllib.parse.urlparse(text)
        if parsed.hostname != host or parsed.scheme not in ("http", "https"):
            deny("不允许：无法把 origin 转成该 GitLab 的 HTTPS 地址。")
        path = parsed.path.lstrip("/")
    if not path or ".." in path.split("/"):
        deny("不允许：origin 路径无效。")
    return "https://{0}/{1}".format(host, path)


def _gitlab_username() -> str:
    name = (_load_secrets().get("gitlab_username") or "").strip()
    return name or "oauth2"


def authed_push_url(origin: str) -> str:
    """HTTPS URL that includes the GitLab username and token.

    Ubuntu 20.04 Git prompts for Username when the URL has neither, and it
    ignores Authorization headers until that prompt is answered.
    """
    base = urllib.parse.urlsplit(https_push_url(origin))
    host = base.hostname or ""
    if base.port:
        host = "{0}:{1}".format(host, base.port)
    user = urllib.parse.quote(_gitlab_username(), safe="")
    netloc = "{0}:{1}@{2}".format(user, urllib.parse.quote(_token(), safe=""), host)
    return urllib.parse.urlunsplit((base.scheme, netloc, base.path, "", ""))


def push_branch(root: Path, branch: str) -> str:
    """Push HEAD with the GitLab token. Do not change the saved origin URL."""
    url = authed_push_url(assert_origin(root))
    proc = git(root, ["push", url, "HEAD:refs/heads/{0}".format(branch)], http_auth=False)
    detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    if "everything up-to-date" in detail.lower():
        return "up-to-date"
    git(
        root,
        ["fetch", url, "+refs/heads/{0}:refs/remotes/origin/{0}".format(branch)],
        check=False,
        http_auth=False,
    )
    return "pushed"


def tool_status() -> str:
    root = bound_root()
    lines = [f"绑定目录: {root}"]
    proc = git(root, ["rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        lines.append("git: 尚未初始化")
        return "\n".join(lines)
    top = Path(proc.stdout.strip()).resolve()
    lines.append(f"git 根: {top}")
    if top != root:
        lines.append("不允许在此目录提交：git 根与绑定目录不一致。")
        return "\n".join(lines)
    branch = git(root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    branch_name = (branch.stdout or "").strip()
    lines.append("当前分支: " + branch_name)
    lines.append("origin: " + (origin_url(root) or "（无）"))
    if branch_name:
        ahead = git(root, ["rev-list", "--count", "origin/{0}..HEAD".format(branch_name)], check=False)
        if ahead.returncode == 0:
            lines.append("未推送到 origin 的提交: " + (ahead.stdout or "0").strip())
    status = git(root, ["status", "--short"], check=False)
    lines.append("变更:\n" + ((status.stdout or "").strip() or "（干净）"))
    return "\n".join(lines)


def tool_create_repository(name: str) -> str:
    root = bound_root()
    if git(root, ["rev-parse", "--show-toplevel"], check=False).returncode == 0:
        git_root(root)
        if origin_url(root):
            deny("不允许：该绑定目录已经有远程仓库，不能再建一个。")
    project_name = (name or root.name).strip()
    if not project_name or "/" in project_name or project_name in (".", ".."):
        deny("不允许：仓库名无效。")
    created = api(
        "POST",
        "/projects",
        {"name": project_name, "path": project_name, "visibility": "private"},
    )
    http_url = str(created.get("http_url_to_repo") or "")
    if gitlab_host() not in http_url:
        deny("不允许：GitLab 返回的仓库地址与配置主机不一致。")
    if git(root, ["rev-parse", "--is-inside-work-tree"], check=False).returncode != 0:
        git(root, ["init", "-b", "main"])
    if not origin_url(root):
        git(root, ["remote", "add", "origin", http_url])
    else:
        deny("不允许：origin 已存在。")
    return f"已为绑定目录建立仓库: {created.get('path_with_namespace')}\n{http_url}"


def tool_create_branch(branch: str, ref: str) -> str:
    root = bound_root()
    git_root(root)
    assert_origin(root)
    branch = branch.strip()
    if not branch or any(ch in branch for ch in " \n\t~^:?*[\\"):
        deny("不允许：分支名无效。")
    exists = git(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
    if exists.returncode == 0:
        deny(f"不允许：本地分支已存在 {branch}")
    start = ref.strip() or "HEAD"
    git(root, ["checkout", "-b", branch, start])
    state = push_branch(root, branch)
    if state == "up-to-date":
        return f"已在绑定目录创建分支 {branch}，远程已有同名提交。"
    return f"已在绑定目录创建并推送分支: {branch}"


def tool_commit_push(message: str, branch: str) -> str:
    root = bound_root()
    git_root(root)
    assert_origin(root)
    if branch.strip():
        current = git(root, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        if current != branch.strip():
            deny(f"不允许：当前分支是 {current}，与指定分支 {branch.strip()} 不一致。")
    status = git(root, ["status", "--porcelain"]).stdout
    committed = False
    if status.strip():
        message = message.strip()
        if not message:
            deny("不允许：提交说明不能为空。")
        git(root, ["add", "-A"])
        git(root, ["commit", "-m", message])
        committed = True
    head = git(root, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    try:
        state = push_branch(root, head)
    except PermissionError as exc:
        if committed:
            deny(f"本地提交已完成，但推送失败：{exc}")
        raise
    if committed and state == "pushed":
        return f"已提交并推送绑定目录 {root} 的分支 {head}"
    if committed:
        return f"已提交绑定目录 {root} 的分支 {head}。远程已包含该提交。"
    if state == "pushed":
        return f"没有需要提交的新修改。已将本地分支 {head} 推送到远程。"
    return f"没有需要提交的修改，远程分支 {head} 已是最新。"


TOOLS = {
    "gitlab_status": {
        "description": "查看当前 /allow_mcp 绑定目录的 git 与 GitLab 远程状态。其它目录不可用。",
        "schema": {"type": "object", "properties": {}},
        "run": lambda args: tool_status(),
    },
    "gitlab_create_repository": {
        "description": "仅为当前绑定目录在 GitLab 建立仓库并设置 origin。目录已有远程仓库时拒绝。",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "仓库名，默认使用目录名"},
            },
        },
        "run": lambda args: tool_create_repository(str(args.get("name") or "")),
    },
    "gitlab_create_branch": {
        "description": "仅为当前绑定目录的仓库创建并推送分支。",
        "schema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "ref": {"type": "string", "description": "起点，默认 HEAD"},
            },
            "required": ["branch"],
        },
        "run": lambda args: tool_create_branch(str(args.get("branch") or ""), str(args.get("ref") or "")),
    },
    "gitlab_commit_push": {
        "description": "提交并推送当前绑定目录。工作区干净时仍会推送尚未送到远程的提交。推送使用 GitLab HTTPS 和访问令牌，不走 SSH，也不修改 origin。",
        "schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "branch": {"type": "string", "description": "可选，必须等于当前分支"},
            },
            "required": ["message"],
        },
        "run": lambda args: tool_commit_push(str(args.get("message") or ""), str(args.get("branch") or "")),
    },
}


# Cursor Agent stdio MCP uses newline-delimited JSON. Keep Content-Length as fallback.
_FRAMING = "ndjson"


def read_message() -> Optional[dict]:
    global _FRAMING
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        # Newline-delimited JSON-RPC (Cursor Agent).
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            _FRAMING = "ndjson"
            return json.loads(stripped.decode("utf-8"))
        # Content-Length framing.
        if b":" not in line:
            continue
        headers = {}
        key, value = line.decode("utf-8", errors="replace").split(":", 1)
        headers[key.strip().lower()] = value.strip()
        while True:
            next_line = sys.stdin.buffer.readline()
            if not next_line:
                return None
            if next_line in (b"\r\n", b"\n"):
                break
            if b":" not in next_line:
                continue
            key, value = next_line.decode("utf-8", errors="replace").split(":", 1)
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or "0")
        if length <= 0:
            return None
        body = sys.stdin.buffer.read(length)
        _FRAMING = "content-length"
        return json.loads(body.decode("utf-8"))


def write_message(payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if _FRAMING == "content-length":
        sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(data)
    else:
        sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def tool_result(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(message: dict) -> Optional[dict]:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "notifications/initialized" or msg_id is None and method and method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = message.get("params") or {}
        version = params.get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "gitlab-bound", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        items = []
        for name, spec in TOOLS.items():
            items.append(
                {
                    "name": name,
                    "description": spec["description"],
                    "inputSchema": spec["schema"],
                }
            )
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": items}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            result = tool_result(f"不允许：未知工具 {name}", True)
        else:
            try:
                result = tool_result(spec["run"](args), False)
            except PermissionError as exc:
                result = tool_result(str(exc), True)
            except Exception as exc:
                result = tool_result(redact(str(exc)), True)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if msg_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        response = handle(message)
        if response is not None:
            write_message(response)


def ensure_cursor_mcp_entry() -> None:
    """Register this server with Cursor. The token is not written to disk here."""
    script = Path("/home/uisee/workspace/test/feishu_cursor/app/gitlab_mcp.py")
    if not script.is_file():
        script = Path(__file__).resolve()
    path = Path.home() / ".cursor" / "mcp.json"
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["gitlab-bound"] = {"command": "python3", "args": [str(script)]}
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    serve()
