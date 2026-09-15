"""Transitions de statut → messages Telegram.

Un couple pre_save/post_save détecte les changements de statut :
  * → PENDING : alerte l'équipe (nouvelle demande complète) ;
  * → SUCCESS / REJECTED : prévient le client.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from core.models import Transaction

from .notify import notify_client, notify_team

logger = logging.getLogger("telegrambot")


@receiver(pre_save, sender=Transaction)
def _capture_previous_status(sender, instance: Transaction, **kwargs) -> None:
    instance._previous_status = None
    if instance.pk:
        try:
            instance._previous_status = Transaction.objects.only("status").get(pk=instance.pk).status
        except Transaction.DoesNotExist:
            pass


@receiver(post_save, sender=Transaction)
def _on_status_change(sender, instance: Transaction, created: bool, **kwargs) -> None:
    try:
        prev = getattr(instance, "_previous_status", None)
        if not created and prev == instance.status:
            return
        S = Transaction.Status
        if instance.status == S.PENDING:
            notify_team(instance)
        elif instance.status in (S.SUCCESS, S.REJECTED):
            notify_client(instance)
    except Exception:
        logger.exception("Signal Telegram : échec sur tx=%s", getattr(instance, "id", "?"))
