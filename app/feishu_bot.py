"""Feishu IM helpers: send / reply text (chunked)."""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

logger = logging.getLogger(__name__)

# Feishu text message soft limit; keep margin for JSON wrapping.
MAX_CHUNK = 3500


def chunk_text(text: str, size: int = MAX_CHUNK) -> list:
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]


class FeishuMessenger:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

    def reply_text(self, message_id: str, text: str) -> None:
        chunks = chunk_text(text)
        for i, part in enumerate(chunks):
            prefix = f"({i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
            body = (
                ReplyMessageRequestBody.builder()
                .content(json.dumps({"text": prefix + part}, ensure_ascii=False))
                .msg_type("text")
                .build()
            )
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            resp = self.client.im.v1.message.reply(req)
            if not resp.success():
                logger.error(
                    "reply failed code=%s msg=%s log_id=%s",
                    resp.code,
                    resp.msg,
                    resp.get_log_id(),
                )
            if i + 1 < len(chunks):
                time.sleep(0.2)

    def send_text(self, chat_id: str, text: str) -> None:
        chunks = chunk_text(text)
        for i, part in enumerate(chunks):
            prefix = f"({i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": prefix + part}, ensure_ascii=False))
                .build()
            )
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(body)
                .build()
            )
            resp = self.client.im.v1.message.create(req)
            if not resp.success():
                logger.error(
                    "send failed code=%s msg=%s log_id=%s",
                    resp.code,
                    resp.msg,
                    resp.get_log_id(),
                )
            if i + 1 < len(chunks):
                time.sleep(0.2)


def extract_message_text(content: Optional[str], message_type: Optional[str]) -> str:
    if not content:
        return ""
    if message_type and message_type != "text":
        return f"[暂不支持的消息类型: {message_type}]"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(data, dict):
        return str(data.get("text") or "")
    return content
