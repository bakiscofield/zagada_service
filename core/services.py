"""Règles métier — la seule porte d'entrée pour créer/faire évoluer une
transaction. Le bot ET l'admin passent par ici : un changement de règle vaut
pour les deux."""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from .models import Bookmaker, Client, PlayerProfile, SiteSettings, Transaction

logger = logging.getLogger("core")

# Un même dépôt (client, bookmaker, joueur, montant) redemandé dans ce délai
# est considéré comme un doublon (double clic, réseau lent).
DUPLICATE_WINDOW = timedelta(minutes=10)


class ServiceError(Exception):
    """Erreur métier lisible, à afficher telle quelle au client."""


class DuplicateError(ServiceError):
    pass


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
def get_or_create_client(tg_user: dict) -> Client | None:
    """`tg_user` = dict `from` d'un update Telegram. None si données insuffisantes."""
    tg_id = tg_user.get("id")
    if not tg_id:
        return None
    fields = {
        "username": (tg_user.get("username") or "")[:64],
        "first_name": (tg_user.get("first_name") or "")[:64],
        "last_name": (tg_user.get("last_name") or "")[:64],
    }
    client, created = Client.objects.get_or_create(telegram_id=tg_id, defaults=fields)
    if created:
        logger.info("Nouveau client Telegram : %s", client.display_name)
    elif any(getattr(client, k) != v for k, v in fields.items()):
        for k, v in fields.items():
            setattr(client, k, v)
        client.save(update_fields=[*fields, "updated_at"])
    return client


def remember_profile(client: Client, bookmaker: Bookmaker, player_id: str, player_name: str = "") -> None:
    profile, _ = PlayerProfile.objects.get_or_create(
        client=client, bookmaker=bookmaker, player_id=player_id,
        defaults={"player_name": player_name},
    )
    profile.last_used_at = timezone.now()
    if player_name and not profile.player_name:
        profile.player_name = player_name
    profile.save(update_fields=["last_used_at", "player_name"])


def remember_phone(client: Client, phone: str) -> None:
    if phone and client.phone != phone:
        client.phone = phone[:32]
        client.save(update_fields=["phone", "updated_at"])


# --------------------------------------------------------------------------- #
# Dépôt
# --------------------------------------------------------------------------- #
def _check_client(client: Client) -> None:
    if client.is_blocked:
        raise ServiceError("Votre compte est bloqué. Contactez le support.")


def create_deposit(
    *, client: Client, bookmaker: Bookmaker, player_id: str, amount: Decimal,
    network: str, phone: str, player_name: str = "",
) -> Transaction:
    """Ouvre un dépôt : le client doit maintenant payer et donner sa référence."""
    _check_client(client)
    conf = SiteSettings.load()
    if not conf.deposit_enabled:
        raise ServiceError(conf.closed_message)
    if not bookmaker.is_active:
        raise ServiceError("Ce bookmaker n'est plus disponible.")
    amount = Decimal(amount)
    if amount < bookmaker.min_deposit:
        raise ServiceError(f"Le dépôt minimum chez {bookmaker.name} est de {bookmaker.min_deposit:.0f} F.")
    if network not in Transaction.Network.values:
        raise ServiceError("Réseau de paiement invalide.")
    number, _ = conf.merchant_for(network)
    if not number:
        raise ServiceError(f"Les dépôts par {Transaction.Network(network).label} ne sont pas disponibles pour le moment.")

    recent = Transaction.objects.filter(
        client=client, bookmaker=bookmaker, kind=Transaction.Kind.DEPOSIT,
        player_id=player_id, amount=amount, status__in=Transaction.OPEN_STATUSES,
        created_at__gte=timezone.now() - DUPLICATE_WINDOW,
    ).first()
    if recent:
        raise DuplicateError(
            f"Un dépôt identique (#{recent.id}) est déjà en cours. Terminez-le ou annulez-le d'abord."
        )

    with db_transaction.atomic():
        tx = Transaction.objects.create(
            client=client, bookmaker=bookmaker, kind=Transaction.Kind.DEPOSIT,
            status=Transaction.Status.AWAITING_PAYMENT, player_id=player_id,
            player_name=player_name, amount=amount, network=network, phone=phone,
        )
        remember_profile(client, bookmaker, player_id, player_name)
        remember_phone(client, phone)
    logger.info("DEPOSIT[new] tx=%s client=%s %s F %s", tx.id, client.display_name, amount, bookmaker.code)
    return tx


def submit_deposit_reference(tx: Transaction, reference: str) -> Transaction:
    """Le client a payé : la demande passe dans la file de l'équipe."""
    reference = (reference or "").strip()[:64]
    if not reference:
        raise ServiceError("Référence vide.")
    with db_transaction.atomic():
        tx = Transaction.objects.select_for_update().get(pk=tx.pk)
        if tx.status != Transaction.Status.AWAITING_PAYMENT:
            raise ServiceError(f"Ce dépôt n'attend plus de paiement ({tx.get_status_display()}).")
        tx.payment_reference = reference
        tx.status = Transaction.Status.PENDING
        tx.save(update_fields=["payment_reference", "status", "updated_at"])
    logger.info("DEPOSIT[submitted] tx=%s ref=%s", tx.id, reference)
    return tx


# --------------------------------------------------------------------------- #
# Retrait
# --------------------------------------------------------------------------- #
def create_payout(
    *, client: Client, bookmaker: Bookmaker, player_id: str, code: str,
    network: str, phone: str, player_name: str = "",
) -> Transaction:
    _check_client(client)
    conf = SiteSettings.load()
    if not conf.payout_enabled:
        raise ServiceError(conf.closed_message)
    if not bookmaker.is_active:
        raise ServiceError("Ce bookmaker n'est plus disponible.")
    code = (code or "").strip()
    if not code:
        raise ServiceError("Code de retrait vide.")
    if network not in Transaction.Network.values:
        raise ServiceError("Réseau de paiement invalide.")

    existing = Transaction.objects.filter(
        bookmaker=bookmaker, kind=Transaction.Kind.PAYOUT, payout_code__iexact=code,
        status__in=[*Transaction.OPEN_STATUSES, Transaction.Status.SUCCESS],
    ).first()
    if existing:
        raise DuplicateError(f"Ce code de retrait a déjà été soumis (#{existing.id}).")

    with db_transaction.atomic():
        tx = Transaction.objects.create(
            client=client, bookmaker=bookmaker, kind=Transaction.Kind.PAYOUT,
            status=Transaction.Status.PENDING, player_id=player_id, player_name=player_name,
            payout_code=code, network=network, phone=phone,
        )
        remember_profile(client, bookmaker, player_id, player_name)
        remember_phone(client, phone)
    logger.info("PAYOUT[new] tx=%s client=%s %s", tx.id, client.display_name, bookmaker.code)
    return tx


# --------------------------------------------------------------------------- #
# Décisions (client ou équipe)
# --------------------------------------------------------------------------- #
def cancel_by_client(tx: Transaction) -> Transaction:
    """Le client annule un dépôt qu'il n'a pas encore payé."""
    with db_transaction.atomic():
        tx = Transaction.objects.select_for_update().get(pk=tx.pk)
        if tx.status != Transaction.Status.AWAITING_PAYMENT:
            raise ServiceError("Cette demande ne peut plus être annulée : elle est déjà en traitement.")
        tx.status = Transaction.Status.CANCELLED
        tx.admin_note = "Annulé par le client"
        tx.processed_at = timezone.now()
        tx.save(update_fields=["status", "admin_note", "processed_at", "updated_at"])
    return tx


def _decide(tx: Transaction, admin, status: str, note: str, amount=None) -> Transaction:
    with db_transaction.atomic():
        tx = Transaction.objects.select_for_update().get(pk=tx.pk)
        if not tx.is_open:
            raise ServiceError(f"La demande #{tx.id} est déjà close ({tx.get_status_display()}).")
        if amount is not None:
            tx.amount = Decimal(amount)
        tx.status = status
        tx.admin_note = (note or "")[:255]
        tx.processed_by = admin
        tx.processed_at = timezone.now()
        tx.save()
    logger.info("TX[%s] tx=%s by=%s note=%s", status, tx.id, getattr(admin, "username", "?"), note)
    return tx


def mark_success(tx: Transaction, admin, note: str = "", amount=None) -> Transaction:
    """Dépôt : compte joueur crédité. Retrait : argent envoyé au client
    (`amount` = montant réellement payé si connu)."""
    return _decide(tx, admin, Transaction.Status.SUCCESS, note, amount)


def mark_rejected(tx: Transaction, admin, note: str = "") -> Transaction:
    return _decide(tx, admin, Transaction.Status.REJECTED, note or "Demande refusée")
