from django.db import models


class TelegramSession(models.Model):
    """État de la conversation d'un chat avec le bot (machine à états).

    Le webhook est sans état : on persiste l'étape (`step`) et le contexte
    accumulé (`data` : bookmaker, ID joueur, montant, réseau…) entre deux
    updates. Une session = un chat privé Telegram.
    """

    class Step(models.TextChoices):
        IDLE = "idle", "Au repos"

        DEP_BOOKMAKER = "dep_bookmaker", "Dépôt — bookmaker"
        DEP_PLAYER = "dep_player", "Dépôt — ID joueur"
        DEP_AMOUNT = "dep_amount", "Dépôt — montant"
        DEP_NETWORK = "dep_network", "Dépôt — réseau"
        DEP_PHONE = "dep_phone", "Dépôt — numéro"
        DEP_CONFIRM = "dep_confirm", "Dépôt — confirmation"
        DEP_REFERENCE = "dep_reference", "Dépôt — référence du paiement"

        PAY_BOOKMAKER = "pay_bookmaker", "Retrait — bookmaker"
        PAY_PLAYER = "pay_player", "Retrait — ID joueur"
        PAY_CODE = "pay_code", "Retrait — code"
        PAY_NETWORK = "pay_network", "Retrait — réseau"
        PAY_PHONE = "pay_phone", "Retrait — numéro"
        PAY_CONFIRM = "pay_confirm", "Retrait — confirmation"

    chat_id = models.BigIntegerField(unique=True, db_index=True)
    step = models.CharField(max_length=32, choices=Step.choices, default=Step.IDLE)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"TelegramSession(chat={self.chat_id}, step={self.step})"

    def reset(self) -> None:
        self.step = self.Step.IDLE
        self.data = {}
        self.save(update_fields=["step", "data", "updated_at"])
