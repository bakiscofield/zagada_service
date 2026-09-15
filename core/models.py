"""Domaine de Zagada Service : dépôts et retraits bookmakers via mobile money.

Le client passe par le bot Telegram, l'équipe traite dans l'admin Django :
  * DÉPÔT : le client envoie l'argent au numéro marchand (Flooz / Mixx), donne
    la référence du transfert, l'équipe vérifie la réception et crédite le
    compte joueur chez le bookmaker, puis marque la demande réussie ;
  * RETRAIT : le client donne son code de retrait bookmaker, l'équipe encaisse
    le code chez le bookmaker et envoie l'argent en mobile money au client.
Chaque changement d'état notifie le client dans Telegram (telegrambot.signals).
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class SiteSettings(models.Model):
    """Réglages modifiables sans déploiement (singleton, pk=1)."""

    brand_name = models.CharField("Nom de la marque", max_length=64, default="Zagada Service")
    deposit_enabled = models.BooleanField("Dépôts ouverts", default=True)
    payout_enabled = models.BooleanField("Retraits ouverts", default=True)
    # Numéros marchands sur lesquels le client envoie son dépôt.
    flooz_number = models.CharField("Numéro Flooz (réception des dépôts)", max_length=32, blank=True, default="")
    tmoney_number = models.CharField("Numéro Mixx by Yas / T-Money (réception des dépôts)", max_length=32, blank=True, default="")
    flooz_account_name = models.CharField("Nom du compte Flooz", max_length=64, blank=True, default="")
    tmoney_account_name = models.CharField("Nom du compte Mixx", max_length=64, blank=True, default="")
    payment_instructions = models.TextField(
        "Consigne supplémentaire après le dépôt", blank=True, default="",
        help_text="Affichée sous le numéro marchand (ex. « Mettez votre ID joueur en motif »).",
    )
    support_whatsapp = models.CharField(
        "WhatsApp support", max_length=32, blank=True, default="",
        help_text="Format international sans espaces, ex. +22890000000.",
    )
    support_telegram = models.CharField(
        "Telegram support", max_length=64, blank=True, default="",
        help_text="Nom d'utilisateur sans @.",
    )
    welcome_text = models.TextField(
        "Texte d'accueil", blank=True,
        default="Dépôts et retraits rapides chez vos bookmakers, par Flooz et Mixx by Yas.",
    )
    closed_message = models.CharField(
        "Message quand une fonction est fermée", max_length=200,
        default="Ce service est momentanément indisponible. Réessayez plus tard.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Réglages"
        verbose_name_plural = "Réglages"

    def __str__(self) -> str:
        return self.brand_name

    @classmethod
    def load(cls) -> "SiteSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def merchant_for(self, network: str) -> tuple[str, str]:
        """(numéro, nom du compte) à créditer pour un réseau donné."""
        if network == Transaction.Network.FLOOZ:
            return self.flooz_number, self.flooz_account_name
        return self.tmoney_number, self.tmoney_account_name


class Bookmaker(models.Model):
    code = models.SlugField(max_length=32, unique=True, help_text="Clé technique, ex. 1xbet")
    name = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    min_deposit = models.DecimalField(max_digits=12, decimal_places=0, default=Decimal("500"))
    min_payout = models.DecimalField(max_digits=12, decimal_places=0, default=Decimal("1000"))
    order = models.PositiveIntegerField(default=0, help_text="Ordre d'affichage dans le bot")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("order", "name")

    def __str__(self) -> str:
        return self.name


class Client(models.Model):
    """Un utilisateur Telegram qui parle au bot (chat privé : chat_id == telegram_id)."""

    telegram_id = models.BigIntegerField(unique=True, db_index=True)
    username = models.CharField(max_length=64, blank=True, default="")
    first_name = models.CharField(max_length=64, blank=True, default="")
    last_name = models.CharField(max_length=64, blank=True, default="")
    phone = models.CharField("Dernier numéro mobile money", max_length=32, blank=True, default="")
    is_blocked = models.BooleanField(default=False, help_text="Un client bloqué ne peut plus rien demander.")
    note = models.TextField(blank=True, default="", help_text="Note interne de l'équipe.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        if self.username:
            return f"@{self.username}" + (f" ({full})" if full else "")
        return full or f"tg{self.telegram_id}"


class PlayerProfile(models.Model):
    """ID joueur mémorisé pour un client chez un bookmaker (boutons « réutiliser »)."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="profiles")
    bookmaker = models.ForeignKey(Bookmaker, on_delete=models.CASCADE, related_name="profiles")
    player_id = models.CharField(max_length=64)
    player_name = models.CharField(max_length=128, blank=True, default="")
    last_used_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = (("client", "bookmaker", "player_id"),)
        ordering = ("-last_used_at",)

    def __str__(self) -> str:
        return f"{self.player_id} @ {self.bookmaker}"


class Transaction(models.Model):
    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Dépôt"
        PAYOUT = "payout", "Retrait"

    class Status(models.TextChoices):
        # Dépôt créé, on attend que le client paie et envoie sa référence.
        AWAITING_PAYMENT = "awaiting_payment", "En attente du paiement client"
        # Demande complète, à traiter par l'équipe.
        PENDING = "pending", "À traiter"
        SUCCESS = "success", "Réussie"
        REJECTED = "rejected", "Rejetée"
        CANCELLED = "cancelled", "Annulée"

    class Network(models.TextChoices):
        FLOOZ = "FLOOZ", "Flooz"
        TMONEY = "TMONEY", "Mixx by Yas (T-Money)"

    OPEN_STATUSES = ("awaiting_payment", "pending")

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="transactions")
    bookmaker = models.ForeignKey(Bookmaker, on_delete=models.PROTECT, related_name="transactions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    player_id = models.CharField("ID joueur", max_length=64)
    player_name = models.CharField(max_length=128, blank=True, default="")
    amount = models.DecimalField(
        max_digits=12, decimal_places=0, default=0,
        help_text="Montant en FCFA. Pour un retrait : rempli par l'équipe si inconnu.",
    )
    payout_code = models.CharField("Code de retrait", max_length=64, blank=True, default="")
    network = models.CharField(max_length=16, choices=Network.choices)
    phone = models.CharField("Numéro mobile money du client", max_length=32)
    payment_reference = models.CharField(
        "Référence du transfert", max_length=64, blank=True, default="",
        help_text="ID de transaction Flooz/Mixx fourni par le client (dépôt).",
    )

    admin_note = models.CharField(max_length=255, blank=True, default="",
                                  help_text="Motif de rejet ou remarque interne.")
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="processed_transactions",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("kind", "status")),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.get_kind_display()} {self.amount} F — {self.player_id} ({self.get_status_display()})"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES


class AdminChat(models.Model):
    """Chat Telegram d'un membre de l'équipe qui reçoit les alertes (nouvelle
    demande à traiter). Le membre obtient son identifiant en envoyant /id au bot."""

    chat_id = models.BigIntegerField(unique=True)
    label = models.CharField(max_length=64, blank=True, default="", help_text="Prénom / rôle")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Chat équipe (alertes)"
        verbose_name_plural = "Chats équipe (alertes)"

    def __str__(self) -> str:
        return f"{self.label or 'membre'} ({self.chat_id})"
