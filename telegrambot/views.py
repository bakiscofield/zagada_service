"""Webhook public recevant les updates Telegram.

Sécurité : le secret figure dans le chemin (`/webhook/<secret>/`) ET dans
l'en-tête `X-Telegram-Bot-Api-Secret-Token` posé par Telegram (via
setWebhook). On répond toujours 200 une fois le secret validé : le traitement
est tolérant aux erreurs, et un 200 évite que Telegram renvoie l'update en
boucle.
"""
from __future__ import annotations

import hmac
import logging

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .flows import handle_update

logger = logging.getLogger("telegrambot")


class TelegramWebhookView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def post(self, request, secret: str):
        expected = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or ""
        if not expected:
            logger.error("Webhook reçu mais TELEGRAM_WEBHOOK_SECRET non défini.")
            return Response(status=status.HTTP_503_SERVICE_UNAVAILABLE)
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not (hmac.compare_digest(secret, expected) and hmac.compare_digest(header_secret, expected)):
            logger.warning("Webhook : secret invalide.")
            return Response(status=status.HTTP_403_FORBIDDEN)
        update = request.data if isinstance(request.data, dict) else {}
        handle_update(update)
        return Response({"ok": True})
