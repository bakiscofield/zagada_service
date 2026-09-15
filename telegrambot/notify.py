"""Envois best-effort : au client (résultat de sa demande) et à l'équipe
(nouvelle demande à traiter). Toute erreur est avalée."""
from __future__ import annotations

import logging

from django.conf import settings

from core.models import AdminChat, Transaction

from . import api
from .texts import esc, fmt_amount

logger = logging.getLogger("telegrambot")


def notify_client(tx: Transaction) -> None:
    try:
        bm = esc(tx.bookmaker.name)
        amount = fmt_amount(tx.amount)
        if tx.kind == Transaction.Kind.DEPOSIT:
            if tx.status == Transaction.Status.SUCCESS:
                msg = f"✅ <b>Dépôt #{tx.id} effectué</b>\n{amount} crédités sur votre compte {bm} (ID {esc(tx.player_id)})."
            elif tx.status == Transaction.Status.REJECTED:
                msg = f"❌ <b>Dépôt #{tx.id} refusé</b> — {bm}.\n{esc(tx.admin_note or 'Contactez le support.')}"
            else:
                return
        else:
            if tx.status == Transaction.Status.SUCCESS:
                amt = f"{amount} " if tx.amount else ""
                msg = f"✅ <b>Retrait #{tx.id} payé</b>\n{amt}envoyés au {esc(tx.phone)} ({esc(tx.get_network_display())})."
            elif tx.status == Transaction.Status.REJECTED:
                msg = f"❌ <b>Retrait #{tx.id} refusé</b> — {bm}.\n{esc(tx.admin_note or 'Contactez le support.')}"
            else:
                return
        api.send_message(tx.client.telegram_id, msg)
    except Exception:
        logger.exception("Notification client échouée pour tx=%s", getattr(tx, "id", "?"))


def notify_team(tx: Transaction) -> None:
    """Nouvelle demande à traiter → tous les chats équipe actifs."""
    try:
        chats = list(AdminChat.objects.filter(is_active=True).values_list("chat_id", flat=True))
        if not chats:
            return
        icon = "💰" if tx.kind == Transaction.Kind.DEPOSIT else "💸"
        lines = [
            f"🔔 {icon} <b>{esc(tx.get_kind_display())} #{tx.id} à traiter</b> — {esc(tx.bookmaker.name)}",
            f"• Client : {esc(tx.client.display_name)}",
            f"• Joueur : {esc(tx.player_name or '—')} (<code>{esc(tx.player_id)}</code>)",
        ]
        if tx.kind == Transaction.Kind.DEPOSIT:
            lines.append(f"• Montant : <b>{fmt_amount(tx.amount)}</b>")
            lines.append(f"• Payé via {esc(tx.get_network_display())} depuis <code>{esc(tx.phone)}</code>")
            lines.append(f"• Référence : <code>{esc(tx.payment_reference or '—')}</code>")
        else:
            lines.append(f"• Code : <code>{esc(tx.payout_code)}</code>")
            lines.append(f"• À payer sur {esc(tx.get_network_display())} <code>{esc(tx.phone)}</code>")
        base = (getattr(settings, "ADMIN_BASE_URL", "") or "").rstrip("/")
        markup = None
        if base:
            markup = api.inline_keyboard([[{"text": "🛠 Traiter dans l'admin",
                                           "url": f"{base}/admin/core/transaction/{tx.id}/change/"}]])
        text = "\n".join(lines)
        for chat_id in chats:
            api.send_message(chat_id, text, reply_markup=markup)
    except Exception:
        logger.exception("Alerte équipe échouée pour tx=%s", getattr(tx, "id", "?"))
