"""Query Cursor account usage for /cursor_used."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

from .config import Config

_EXCHANGE_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
_USAGE_URL = "https://api2.cursor.sh/auth/usage-summary"


def _post_json(url: str, token: str, body: Dict[str, Any]) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "User-Agent": "feishu-cursor",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "feishu-cursor",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cents(value) -> str:
    try:
        return f"${float(value) / 100:.2f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _ts(value) -> str:
    if value is None or value == "":
        return "未知"
    text = str(value).strip()
    try:
        if text.isdigit():
            moment = datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return text


def fetch_cursor_usage(cfg: Config) -> str:
    if not cfg.cursor_api_key:
        return "未配置 cursor_api_key，无法查询额度。"
    try:
        exchanged = _post_json(
            _EXCHANGE_URL,
            cfg.cursor_api_key,
            {"apiKey": cfg.cursor_api_key},
        )
        access = str(exchanged.get("accessToken") or "")
        if not access:
            return "交换 Cursor 访问令牌失败。"
        data = _get_json(_USAGE_URL, access)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return f"查询额度失败 HTTP {exc.code}: {detail}"
    except Exception as exc:
        return f"查询额度失败: {exc}"

    individual = (data.get("individualUsage") or {}).get("plan") or {}
    team_od = (data.get("teamUsage") or {}).get("onDemand") or {}
    individual_od = (data.get("individualUsage") or {}).get("onDemand") or {}

    lines = [
        "Cursor 额度使用情况",
        f"套餐: {data.get('membershipType') or '未知'}",
        f"周期: {_ts(data.get('billingCycleStart'))} ~ {_ts(data.get('billingCycleEnd'))}",
        f"额度重置时间: {_ts(data.get('billingCycleEnd'))}",
        "",
        "个人计划额度:",
        f"  总额度使用: {_pct(individual.get('totalPercentUsed'))}",
        f"  Auto/Cursor 模型: {_pct(individual.get('autoPercentUsed'))}",
        f"  API/其它模型: {_pct(individual.get('apiPercentUsed'))}",
    ]
    breakdown = individual.get("breakdown") or {}
    if breakdown:
        lines.append(
            "  花费: included={inc} bonus={bonus} total={total}".format(
                inc=_cents(breakdown.get("included")),
                bonus=_cents(breakdown.get("bonus")),
                total=_cents(breakdown.get("total")),
            )
        )
    if data.get("autoModelSelectedDisplayMessage"):
        lines.append(f"  说明: {data.get('autoModelSelectedDisplayMessage')}")
    if data.get("namedModelSelectedDisplayMessage"):
        lines.append(f"  API 说明: {data.get('namedModelSelectedDisplayMessage')}")

    if team_od.get("enabled"):
        lines.extend(
            [
                "",
                "团队按需额度:",
                f"  已用 {_cents(team_od.get('used'))} / 限额 {_cents(team_od.get('limit'))}",
                f"  剩余 {_cents(team_od.get('remaining'))}",
            ]
        )
    elif individual_od.get("enabled"):
        lines.extend(
            [
                "",
                "个人按需额度:",
                f"  已用 {_cents(individual_od.get('used'))} / 限额 {_cents(individual_od.get('limit'))}",
                f"  剩余 {_cents(individual_od.get('remaining'))}",
            ]
        )

    return "\n".join(lines)
