"""Enregistre (ou supprime) le webhook du bot auprès de Telegram.

    python manage.py set_telegram_webhook --base-url https://api.zagada.example
    python manage.py set_telegram_webhook --info
    python manage.py set_telegram_webhook --delete

URL finale : `<base-url>/api/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>/`.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from telegrambot import api

BOT_COMMANDS = [
    {"command": "start", "description": "Démarrer / menu principal"},
    {"command": "depot", "description": "Faire un dépôt"},
    {"command": "retrait", "description": "Faire un retrait"},
    {"command": "historique", "description": "Mes dernières opérations"},
    {"command": "support", "description": "Contacter le support"},
    {"command": "cancel", "description": "Annuler l'opération en cours"},
]


class Command(BaseCommand):
    help = "Configure le webhook Telegram du bot."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", help="URL publique de base. Défaut : TELEGRAM_WEBHOOK_BASE_URL.")
        parser.add_argument("--delete", action="store_true")
        parser.add_argument("--info", action="store_true")

    def handle(self, *args, **opts):
        if not (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""):
            raise CommandError("TELEGRAM_BOT_TOKEN n'est pas défini.")
        if opts["info"]:
            self.stdout.write(self.style.SUCCESS(str(api.get_webhook_info())))
            return
        if opts["delete"]:
            self.stdout.write(self.style.SUCCESS(f"Webhook supprimé : {api.delete_webhook()}"))
            return
        secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or ""
        if not secret:
            raise CommandError("TELEGRAM_WEBHOOK_SECRET n'est pas défini.")
        base = (opts.get("base_url") or getattr(settings, "TELEGRAM_WEBHOOK_BASE_URL", "") or "").rstrip("/")
        if not base:
            raise CommandError("Fournissez --base-url ou définissez TELEGRAM_WEBHOOK_BASE_URL.")
        url = f"{base}/api/telegram/webhook/{secret}/"
        if not api.set_webhook(url, secret_token=secret):
            raise CommandError("Échec de l'enregistrement du webhook (voir logs).")
        self.stdout.write(self.style.SUCCESS(f"Webhook enregistré sur {url}"))
        if api.set_my_commands(BOT_COMMANDS):
            self.stdout.write(self.style.SUCCESS("Commandes du menu enregistrées : "
                                                 + ", ".join("/" + c["command"] for c in BOT_COMMANDS)))
        else:
            self.stdout.write(self.style.WARNING("Webhook OK mais l'enregistrement des commandes a échoué."))
