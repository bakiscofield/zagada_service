from django.apps import AppConfig


class TelegrambotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "telegrambot"
    verbose_name = "Bot Telegram"

    def ready(self):
        # Notifie le client quand sa demande est traitée, et l'équipe quand
        # une nouvelle demande arrive. Import paresseux (modèles chargés).
        from . import signals  # noqa: F401
