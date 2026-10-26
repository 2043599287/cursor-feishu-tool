#!/usr/bin/env bash
set -euo pipefail

# Prefer the real install under ~/.local/share (bin/ 上的 symlink 被 docker 挂成文件后会丢 node 旁路).
if [[ -z "${CURSOR_AGENT_BIN:-}" || ! -x "${CURSOR_AGENT_BIN}" || ! -x "$(dirname "${CURSOR_AGENT_BIN}")/node" ]]; then
  CANDIDATE="$(ls -dt /home/uisee/.local/share/cursor-agent/versions/*/cursor-agent 2>/dev/null | head -n1 || true)"
  if [[ -n "${CANDIDATE}" && -x "${CANDIDATE}" ]]; then
    export CURSOR_AGENT_BIN="${CANDIDATE}"
  fi
fi

AGENT_BIN="${CURSOR_AGENT_BIN:-agent}"
if [[ ! -x "${AGENT_BIN}" ]] && ! command -v "${AGENT_BIN}" >/dev/null 2>&1; then
  echo "ERROR: Cursor Agent 不可用: ${AGENT_BIN}" >&2
  echo "请挂载主机目录: ~/.local/share/cursor-agent -> /home/uisee/.local/share/cursor-agent" >&2
  exit 1
fi

NODE_BIN="$(dirname "$(readlink -f "${AGENT_BIN}" 2>/dev/null || echo "${AGENT_BIN}")")/node"
if [[ ! -x "${NODE_BIN}" ]]; then
  echo "ERROR: 未找到 Cursor Agent 自带 node: ${NODE_BIN}" >&2
  exit 1
fi

if [[ ! -f "${SECRET_CONFIG_FILE:-/app/secret_config.yaml}" ]]; then
  echo "ERROR: 未找到 ${SECRET_CONFIG_FILE:-/app/secret_config.yaml}" >&2
  exit 1
fi

echo "Using Cursor Agent: ${AGENT_BIN}"
export CURSOR_AGENT_BIN="${AGENT_BIN}"
exec python3 -m app
