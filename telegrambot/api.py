"""Client HTTP minimal vers l'API Bot de Telegram (aucune librairie tierce).

Toutes les fonctions sont tolérantes : un échec réseau est logué et renvoie
None — un message non envoyé ne doit jamais casser le traitement d'un webhook
ni un save en base.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests
from django.conf import settings

logger = logging.getLogger("telegrambot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 15


def _token() -> str:
    return getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""


def _call(method: str, payload: dict) -> Optional[dict]:
    token = _token()
    if not token:
        logger.error("Telegram API : TELEGRAM_BOT_TOKEN absent — appel '%s' ignoré.", method)
        return None
    try:
        resp = requests.post(API_BASE.format(token=token, method=method), json=payload, timeout=_TIMEOUT)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Telegram API '%s' échec transport : %s", method, exc)
        return None
    if not data.get("ok"):
        logger.warning("Telegram API '%s' a renvoyé une erreur : %s", method, data)
        return None
    return data.get("result")


def inline_keyboard(rows: list[list[dict]]) -> dict:
    """`rows` : lignes de boutons `{"text", "callback_data"}` ou `{"text", "url"}`."""
    return {"inline_keyboard": rows}


def reply_keyboard(rows: list[list[str]], placeholder: str = "") -> dict:
    """Clavier PERMANENT affiché sous la zone de saisie (gros boutons toujours
    visibles). Un appui envoie le libellé comme message texte : `flows` le
    reconnaît comme une commande."""
    markup: dict = {
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    }
    if placeholder:
        markup["input_field_placeholder"] = placeholder
    return markup


def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None,
                 parse_mode: Optional[str] = "HTML") -> Optional[dict]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    return _call("sendMessage", payload)


def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None,
                      parse_mode: Optional[str] = "HTML") -> Optional[dict]:
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text,
                               "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    payload["reply_markup"] = json.dumps(reply_markup or {"inline_keyboard": []})
    return _call("editMessageText", payload)


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False) -> Optional[dict]:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    return _call("answerCallbackQuery", payload)


def set_webhook(url: str, secret_token: str) -> Optional[dict]:
    return _call("setWebhook", {
        "url": url, "secret_token": secret_token,
        "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True,
    })


def set_my_commands(commands: list[dict]) -> Optional[dict]:
    return _call("setMyCommands", {"commands": commands})


def delete_webhook() -> Optional[dict]:
    return _call("deleteWebhook", {"drop_pending_updates": False})


def get_webhook_info() -> Optional[dict]:
    return _call("getWebhookInfo", {})
