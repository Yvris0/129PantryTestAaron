"""Thin Telegram Bot API client over httpx. Every call is a safe no-op without a token.

Chat ids below SIM_MAX belong to the in-app simulator: messages to them are only logged.
"""

import logging
import os
from typing import Optional

import httpx

from lib.db import db
from models.pantry import TelegramLogEntry, TgButton

logger = logging.getLogger(__name__)
API = "https://api.telegram.org"
SIM_MAX = 1000

Buttons = list[list[tuple[str, str]]]


def token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def is_sim(chat_id: int) -> bool:
    return 0 < chat_id < SIM_MAX


async def call(method: str, payload: Optional[dict] = None) -> Optional[dict]:
    tok = token()
    if not tok:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            res = await http.post(f"{API}/bot{tok}/{method}", json=payload or {})
            return res.json()
    except Exception as exc:  # network failures must never break an order
        logger.warning("telegram %s failed: %s", method, exc)
        return None


async def log(chat_id: int, direction: str, text: str, buttons: Optional[Buttons] = None, has_photo: bool = False) -> None:
    entry = TelegramLogEntry(
        chat_id=chat_id,
        direction=direction,  # type: ignore[arg-type]
        text=text,
        buttons=[[TgButton(text=t, data=d) for t, d in row] for row in (buttons or [])],
        has_photo=has_photo,
    )
    await db.telegram_log.insert_one(entry.model_dump())


async def send(chat_id: Optional[int], text: str, buttons: Optional[Buttons] = None) -> bool:
    """Send an HTML message. Returns True when Telegram accepted it."""
    if not chat_id:
        return False
    await log(chat_id, "out", text, buttons)
    if is_sim(chat_id):
        return True
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]
        }
    res = await call("sendMessage", payload)
    return bool(res and res.get("ok"))


async def answer_callback(callback_id: str) -> None:
    if callback_id:
        await call("answerCallbackQuery", {"callback_query_id": callback_id})


async def download_file(file_id: str) -> Optional[tuple[bytes, str]]:
    info = await call("getFile", {"file_id": file_id})
    if not info or not info.get("ok"):
        return None
    path = info["result"]["file_path"]
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            res = await http.get(f"{API}/file/bot{token()}/{path}")
            res.raise_for_status()
    except Exception as exc:
        logger.warning("telegram download failed: %s", exc)
        return None
    ext = path.rsplit(".", 1)[-1].lower()
    ctype = {"png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}.get(ext, "image/jpeg")
    return res.content, ctype
