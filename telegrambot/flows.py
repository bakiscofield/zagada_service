"""Machine à états du bot conversationnel de Zagada Service.

Le bot pose des questions ; le client répond en cliquant des boutons ou, là
où c'est inévitable (ID joueur, montant libre, code, numéro, référence de
paiement), en tapant. Chaque parcours aboutit à une VRAIE demande en base
via `core.services` — l'équipe la traite dans l'admin, le client est notifié.

Données de callback (≤ 64 octets) : `<action>:<valeur>`.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from core import services
from core.models import Bookmaker, PlayerProfile, SiteSettings, Transaction

from . import api
from .models import TelegramSession
from .texts import esc, fmt_amount

logger = logging.getLogger("telegrambot")

AMOUNT_PRESETS = [500, 1000, 2000, 5000, 10000, 25000]

GREETINGS = {
    "bonjour", "bonsoir", "salut", "coucou", "cc",
    "hello", "hi", "hey", "menu", "/menu", "start",
}

MENU_BTN = {"text": "🏠 Menu", "callback_data": "act:menu"}
CANCEL_BTN = {"text": "❌ Annuler", "callback_data": "act:cancel"}

# Libellés du clavier permanent (bas du chat). Un appui envoie ce texte.
KB_DEPOSIT, KB_PAYOUT = "💰 Dépôt", "💸 Retrait"
KB_HISTORY, KB_SUPPORT = "📜 Mes opérations", "☎️ Support"
MAIN_KEYBOARD = [[KB_DEPOSIT, KB_PAYOUT], [KB_HISTORY, KB_SUPPORT]]


def main_keyboard() -> dict:
    return api.reply_keyboard(MAIN_KEYBOARD, placeholder="Choisissez une action…")


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #
def handle_update(update: dict) -> None:
    """Traite un update Telegram. Ne propage jamais : le webhook répond 200."""
    try:
        if "callback_query" in update:
            _handle_callback(update["callback_query"])
        elif "message" in update:
            _handle_message(update["message"])
    except Exception:
        logger.exception("Bot Telegram : échec de traitement d'un update.")


def _session_for(chat_id: int) -> TelegramSession:
    session, _ = TelegramSession.objects.get_or_create(chat_id=chat_id)
    return session


# --------------------------------------------------------------------------- #
# Messages texte
# --------------------------------------------------------------------------- #
def _handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    tg_user = message.get("from") or {}
    text = (message.get("text") or "").strip()
    if chat_id is None:
        return
    if chat.get("type") not in (None, "private"):
        return  # le bot ne travaille qu'en chat privé

    low = text.lower().strip(" !.,?")

    # /id : identifiant du chat, pour enregistrer un membre de l'équipe
    # (admin → Chats équipe). Répond avant toute création de client.
    if low == "/id":
        api.send_message(chat_id, f"Identifiant de ce chat : <code>{chat_id}</code>")
        return

    client = services.get_or_create_client(tg_user)
    if client is None:
        api.send_message(chat_id, "Compte indisponible. Contactez le support.")
        return
    session = _session_for(chat_id)

    if low.startswith("/start") or low in GREETINGS:
        session.reset()
        _send_main_menu(chat_id, client)
    elif low in ("/cancel", "/annuler", "annuler"):
        session.reset()
        api.send_message(chat_id, "Opération annulée.", reply_markup=api.inline_keyboard([[MENU_BTN]]))
    elif low in ("/depot", "/deposit", "dépôt", "depot", KB_DEPOSIT.lower()):
        _start_flow(chat_id, client, session, "deposit")
    elif low in ("/retrait", "/payout", "retrait", KB_PAYOUT.lower()):
        _start_flow(chat_id, client, session, "payout")
    elif low in ("/historique", "/history", "historique", KB_HISTORY.lower()):
        session.reset()
        _show_history(chat_id, client)
    elif low in ("/support", "support", "/aide", "aide", "/help", KB_SUPPORT.lower()):
        session.reset()
        _show_support(chat_id)
    else:
        _handle_text_for_step(chat_id, client, session, text)


def _handle_text_for_step(chat_id, client, session, text) -> None:
    S = TelegramSession.Step
    step = session.step

    # Un ID ou un montant tapé est accepté même quand des boutons sont
    # proposés : le client n'est pas obligé de cliquer « Autre ».
    if step in (S.DEP_PLAYER, S.PAY_PLAYER):
        _set_player_and_continue(chat_id, client, session, text)
    elif step == S.DEP_AMOUNT:
        _set_amount_and_continue(chat_id, session, text)
    elif step == S.PAY_CODE:
        _set_code_and_continue(chat_id, session, text)
    elif step in (S.DEP_PHONE, S.PAY_PHONE):
        _set_phone_and_confirm(chat_id, session, text)
    elif step == S.DEP_REFERENCE:
        _set_reference(chat_id, client, session, text)
    else:
        api.send_message(
            chat_id,
            "Je n'attendais pas de texte ici 🙂. Utilisez les boutons, ou tapez /start.",
            reply_markup=api.inline_keyboard([[MENU_BTN]]),
        )


# --------------------------------------------------------------------------- #
# Clics boutons
# --------------------------------------------------------------------------- #
def _handle_callback(cb: dict) -> None:
    cb_id = cb.get("id")
    data = cb.get("data") or ""
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    tg_user = cb.get("from") or {}
    if chat_id is None:
        api.answer_callback_query(cb_id)
        return

    client = services.get_or_create_client(tg_user)
    if client is None:
        api.answer_callback_query(cb_id, "Compte indisponible.", show_alert=True)
        return
    session = _session_for(chat_id)
    api.answer_callback_query(cb_id)

    action, _, value = data.partition(":")
    if action == "act":
        _handle_action(chat_id, client, session, value)
    elif action == "bm":
        _set_bookmaker_and_continue(chat_id, client, session, value)
    elif action == "pid":
        _handle_player_pick(chat_id, client, session, value)
    elif action == "amt":
        _handle_amount_pick(chat_id, session, value)
    elif action == "net":
        _set_network_and_continue(chat_id, session, value)
    elif action == "phone" and value == "last":
        _set_phone_and_confirm(chat_id, session, client.phone)
    elif action == "txcancel":
        _cancel_open_deposit(chat_id, client, session, value)


def _handle_action(chat_id, client, session, value) -> None:
    if value == "deposit":
        _start_flow(chat_id, client, session, "deposit")
    elif value == "payout":
        _start_flow(chat_id, client, session, "payout")
    elif value == "history":
        session.reset()
        _show_history(chat_id, client)
    elif value == "support":
        session.reset()
        _show_support(chat_id)
    elif value == "menu":
        session.reset()
        _send_main_menu(chat_id, client)
    elif value == "cancel":
        session.reset()
        api.send_message(chat_id, "Opération annulée.", reply_markup=api.inline_keyboard([[MENU_BTN]]))
    elif value == "confirm":
        _do_confirm(chat_id, client, session)


# --------------------------------------------------------------------------- #
# Menu principal, historique, support
# --------------------------------------------------------------------------- #
def _send_main_menu(chat_id, client) -> None:
    conf = SiteSettings.load()
    name = (client.first_name or client.username or "").strip()
    hello = f"Bonjour {esc(name)} 👋" if name else "Bonjour 👋"
    intro = f"\n{esc(conf.welcome_text)}" if conf.welcome_text else ""
    closed = []
    if not conf.deposit_enabled:
        closed.append("dépôts")
    if not conf.payout_enabled:
        closed.append("retraits")
    notice = f"\n⛔ {' et '.join(closed)} momentanément fermés." if closed else ""
    # Le menu = clavier permanent en bas du chat : les boutons restent
    # visibles pendant toute la conversation, sans retaper de commande.
    api.send_message(
        chat_id,
        f"{hello}\n<b>{esc(conf.brand_name)}</b>{intro}{notice}\n\n"
        "Utilisez les boutons ci-dessous 👇",
        reply_markup=main_keyboard(),
    )


def _show_history(chat_id, client) -> None:
    txs = list(client.transactions.select_related("bookmaker").order_by("-created_at")[:5])
    if not txs:
        api.send_message(chat_id, "Aucune opération pour le moment.",
                         reply_markup=api.inline_keyboard([[MENU_BTN]]))
        return
    lines = ["📜 <b>Vos dernières opérations</b>", ""]
    rows = []
    for tx in txs:
        icon = "💰" if tx.kind == Transaction.Kind.DEPOSIT else "💸"
        amount = fmt_amount(tx.amount) if tx.amount else "—"
        when = timezone.localtime(tx.created_at).strftime("%d/%m %H:%M")
        lines.append(f"{icon} #{tx.id} · {esc(tx.bookmaker.name)} · {amount} · "
                     f"<b>{esc(tx.get_status_display())}</b> · {when}")
        if tx.status == Transaction.Status.AWAITING_PAYMENT:
            rows.append([{"text": f"❌ Annuler le dépôt #{tx.id}", "callback_data": f"txcancel:{tx.id}"}])
    rows.append([MENU_BTN])
    api.send_message(chat_id, "\n".join(lines), reply_markup=api.inline_keyboard(rows))


def _show_support(chat_id) -> None:
    conf = SiteSettings.load()
    rows = []
    if conf.support_whatsapp:
        digits = "".join(c for c in conf.support_whatsapp if c.isdigit())
        rows.append([{"text": "💬 WhatsApp", "url": f"https://wa.me/{digits}"}])
    if conf.support_telegram:
        rows.append([{"text": "✈️ Telegram", "url": f"https://t.me/{conf.support_telegram.lstrip('@')}"}])
    rows.append([MENU_BTN])
    text = "☎️ <b>Support</b>\nNotre équipe vous répond ici :" if len(rows) > 1 else \
        "☎️ <b>Support</b>\nAucun contact configuré pour le moment."
    api.send_message(chat_id, text, reply_markup=api.inline_keyboard(rows))


# --------------------------------------------------------------------------- #
# Démarrage d'un parcours + étape bookmaker
# --------------------------------------------------------------------------- #
def _start_flow(chat_id, client, session, flow: str) -> None:
    conf = SiteSettings.load()
    enabled = conf.deposit_enabled if flow == "deposit" else conf.payout_enabled
    if not enabled:
        session.reset()
        api.send_message(chat_id, f"⛔ {esc(conf.closed_message)}",
                         reply_markup=api.inline_keyboard([[MENU_BTN]]))
        return
    if client.is_blocked:
        session.reset()
        api.send_message(chat_id, "⛔ Votre compte est bloqué. Contactez le support.",
                         reply_markup=api.inline_keyboard([[{"text": "☎️ Support", "callback_data": "act:support"}]]))
        return
    session.step = (TelegramSession.Step.DEP_BOOKMAKER if flow == "deposit"
                    else TelegramSession.Step.PAY_BOOKMAKER)
    session.data = {"flow": flow}
    session.save()
    title = "💰 <b>Dépôt</b> — choisissez un bookmaker :" if flow == "deposit" \
        else "💸 <b>Retrait</b> — choisissez un bookmaker :"
    bookmakers = list(Bookmaker.objects.filter(is_active=True))
    if not bookmakers:
        session.reset()
        api.send_message(chat_id, "Aucun bookmaker disponible pour le moment.",
                         reply_markup=api.inline_keyboard([[MENU_BTN]]))
        return
    rows, line = [], []
    for bm in bookmakers:
        line.append({"text": bm.name, "callback_data": f"bm:{bm.code}"})
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([CANCEL_BTN])
    api.send_message(chat_id, title, reply_markup=api.inline_keyboard(rows))


def _set_bookmaker_and_continue(chat_id, client, session, code) -> None:
    if session.step not in (TelegramSession.Step.DEP_BOOKMAKER, TelegramSession.Step.PAY_BOOKMAKER):
        _send_main_menu(chat_id, client)
        return
    bm = Bookmaker.objects.filter(code=code, is_active=True).first()
    if bm is None:
        session.reset()
        api.send_message(chat_id, "Ce bookmaker n'est plus disponible. Tapez /start.")
        return
    session.data.update({
        "bookmaker_code": bm.code, "bookmaker_name": bm.name,
        "min_deposit": str(bm.min_deposit), "min_payout": str(bm.min_payout),
    })
    session.save()
    _ask_player(chat_id, client, session)


# --------------------------------------------------------------------------- #
# Étape : ID joueur
# --------------------------------------------------------------------------- #
def _ask_player(chat_id, client, session) -> None:
    flow = session.data["flow"]
    session.step = (TelegramSession.Step.DEP_PLAYER if flow == "deposit"
                    else TelegramSession.Step.PAY_PLAYER)
    profiles = list(PlayerProfile.objects.filter(
        client=client, bookmaker__code=session.data["bookmaker_code"]).order_by("-last_used_at")[:6])
    session.data["player_ids"] = [{"id": p.player_id, "name": p.player_name} for p in profiles]
    if profiles:
        session.data["await_text"] = False
        session.save()
        rows = [[{"text": f"👤 {p.player_name or p.player_id} ({p.player_id})" if p.player_name
                  else f"👤 {p.player_id}", "callback_data": f"pid:{i}"}] for i, p in enumerate(profiles)]
        rows.append([{"text": "➕ Autre ID", "callback_data": "pid:new"}])
        rows.append([CANCEL_BTN])
        api.send_message(chat_id, "Choisissez votre <b>ID joueur</b> ou ajoutez-en un :",
                         reply_markup=api.inline_keyboard(rows))
    else:
        session.data["await_text"] = True
        session.save()
        api.send_message(chat_id, f"Entrez votre <b>ID joueur</b> chez {esc(session.data['bookmaker_name'])} :",
                         reply_markup=api.inline_keyboard([[CANCEL_BTN]]))


def _handle_player_pick(chat_id, client, session, value) -> None:
    if value == "new":
        session.data["await_text"] = True
        session.save()
        api.send_message(chat_id, "Entrez votre <b>ID joueur</b> :",
                         reply_markup=api.inline_keyboard([[CANCEL_BTN]]))
        return
    try:
        chosen = session.data.get("player_ids", [])[int(value)]
    except (ValueError, IndexError):
        session.reset()
        api.send_message(chat_id, "Sélection invalide. Tapez /start.")
        return
    _set_player_and_continue(chat_id, client, session, chosen["id"], known_name=chosen.get("name") or "")


def _set_player_and_continue(chat_id, client, session, raw_id, known_name="") -> None:
    player_id = (raw_id or "").strip()
    if not player_id or len(player_id) > 64:
        api.send_message(chat_id, "ID invalide. Entrez votre ID joueur :")
        return
    session.data.update({"player_id": player_id, "player_name": known_name, "await_text": False})
    session.save()
    api.send_message(chat_id, f"✅ ID joueur : <b>{esc(player_id)}</b>")
    if session.data["flow"] == "deposit":
        _ask_amount(chat_id, session)
    else:
        _ask_code(chat_id, session)


# --------------------------------------------------------------------------- #
# Étape : montant (dépôt)
# --------------------------------------------------------------------------- #
def _min_deposit(session) -> Decimal:
    try:
        return Decimal(session.data.get("min_deposit") or "0")
    except InvalidOperation:
        return Decimal("0")


def _ask_amount(chat_id, session) -> None:
    session.step = TelegramSession.Step.DEP_AMOUNT
    session.data["await_text"] = False
    session.save()
    min_dep = _min_deposit(session)
    rows, line = [], []
    for amt in [a for a in AMOUNT_PRESETS if Decimal(a) >= min_dep]:
        line.append({"text": fmt_amount(amt), "callback_data": f"amt:{amt}"})
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([{"text": "✏️ Autre montant", "callback_data": "amt:new"}])
    rows.append([CANCEL_BTN])
    min_txt = f" (minimum {fmt_amount(min_dep)})" if min_dep > 0 else ""
    api.send_message(chat_id, f"Quel <b>montant</b> souhaitez-vous déposer{min_txt} ?",
                     reply_markup=api.inline_keyboard(rows))


def _handle_amount_pick(chat_id, session, value) -> None:
    if session.step != TelegramSession.Step.DEP_AMOUNT:
        return
    if value == "new":
        session.data["await_text"] = True
        session.save()
        api.send_message(chat_id, "Entrez le <b>montant</b> du dépôt (en FCFA) :")
        return
    _set_amount_and_continue(chat_id, session, value)


def _set_amount_and_continue(chat_id, session, raw) -> None:
    raw = (raw or "").strip().replace(" ", "").replace(",", ".").rstrip("fF")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        api.send_message(chat_id, "Montant invalide. Entrez un nombre, ex : 2000")
        return
    if amount <= 0 or amount != amount.to_integral_value():
        api.send_message(chat_id, "Le montant doit être un nombre entier positif. Réessayez :")
        return
    min_dep = _min_deposit(session)
    if min_dep > 0 and amount < min_dep:
        api.send_message(chat_id, f"Le dépôt minimum est de {fmt_amount(min_dep)}. Réessayez :")
        return
    session.data["amount"] = str(int(amount))
    session.data["await_text"] = False
    session.save()
    _ask_network(chat_id, session)


# --------------------------------------------------------------------------- #
# Étape : code de retrait
# --------------------------------------------------------------------------- #
def _ask_code(chat_id, session) -> None:
    session.step = TelegramSession.Step.PAY_CODE
    session.save()
    api.send_message(chat_id, "Entrez votre <b>code de retrait</b> (fourni par le bookmaker) :",
                     reply_markup=api.inline_keyboard([[CANCEL_BTN]]))


def _set_code_and_continue(chat_id, session, text) -> None:
    code = (text or "").strip()
    if not code or len(code) > 64:
        api.send_message(chat_id, "Code invalide. Entrez votre code de retrait :")
        return
    session.data["code"] = code
    session.save()
    _ask_network(chat_id, session)


# --------------------------------------------------------------------------- #
# Étape : réseau + numéro mobile money
# --------------------------------------------------------------------------- #
def _networks(session) -> list[tuple[str, str]]:
    """Réseaux proposés : pour un dépôt, seulement ceux qui ont un numéro marchand."""
    conf = SiteSettings.load()
    out = []
    for code, label in Transaction.Network.choices:
        if session.data["flow"] == "deposit" and not conf.merchant_for(code)[0]:
            continue
        out.append((code, label))
    return out


def _ask_network(chat_id, session) -> None:
    flow = session.data["flow"]
    session.step = (TelegramSession.Step.DEP_NETWORK if flow == "deposit"
                    else TelegramSession.Step.PAY_NETWORK)
    session.save()
    nets = _networks(session)
    if not nets:
        session.reset()
        api.send_message(chat_id, "Aucun moyen de paiement disponible pour le moment.",
                         reply_markup=api.inline_keyboard([[MENU_BTN]]))
        return
    rows = [[{"text": label, "callback_data": f"net:{code}"}] for code, label in nets]
    rows.append([CANCEL_BTN])
    what = "avec lequel vous allez payer" if flow == "deposit" else "sur lequel vous recevrez l'argent"
    api.send_message(chat_id, f"Choisissez le <b>réseau</b> {what} :", reply_markup=api.inline_keyboard(rows))


def _set_network_and_continue(chat_id, session, value) -> None:
    if session.step not in (TelegramSession.Step.DEP_NETWORK, TelegramSession.Step.PAY_NETWORK):
        return
    if value not in dict(_networks(session)):
        session.reset()
        api.send_message(chat_id, "Réseau invalide. Tapez /start.")
        return
    session.data["network"] = value
    session.save()
    _ask_phone(chat_id, session)


def _ask_phone(chat_id, session) -> None:
    flow = session.data["flow"]
    session.step = (TelegramSession.Step.DEP_PHONE if flow == "deposit"
                    else TelegramSession.Step.PAY_PHONE)
    session.save()
    from core.models import Client

    client = Client.objects.filter(telegram_id=chat_id).only("phone").first()
    rows = []
    if client and client.phone:
        rows.append([{"text": f"📱 Réutiliser {client.phone}", "callback_data": "phone:last"}])
    rows.append([CANCEL_BTN])
    what = "depuis lequel vous payez" if flow == "deposit" else "qui recevra l'argent"
    api.send_message(chat_id, f"Entrez le <b>numéro</b> mobile money {what} :",
                     reply_markup=api.inline_keyboard(rows))


def _set_phone_and_confirm(chat_id, session, text) -> None:
    phone = "".join(c for c in (text or "") if c.isdigit() or c == "+")
    if len(phone.lstrip("+")) < 8 or len(phone) > 16:
        api.send_message(chat_id, "Numéro invalide. Entrez votre numéro mobile money (ex : 90000000) :")
        return
    session.data["phone"] = phone
    session.step = (TelegramSession.Step.DEP_CONFIRM if session.data["flow"] == "deposit"
                    else TelegramSession.Step.PAY_CONFIRM)
    session.save()
    _ask_confirm(chat_id, session)


# --------------------------------------------------------------------------- #
# Récapitulatif + confirmation
# --------------------------------------------------------------------------- #
def _ask_confirm(chat_id, session) -> None:
    d = session.data
    network = Transaction.Network(d["network"]).label
    if d["flow"] == "deposit":
        lines = [
            "🧾 <b>Récapitulatif du dépôt</b>",
            f"• Bookmaker : {esc(d['bookmaker_name'])}",
            f"• ID joueur : {esc(d['player_id'])}",
            f"• Montant : {fmt_amount(d['amount'])}",
            f"• Paiement : {esc(network)} depuis {esc(d['phone'])}",
        ]
    else:
        lines = [
            "🧾 <b>Récapitulatif du retrait</b>",
            f"• Bookmaker : {esc(d['bookmaker_name'])}",
            f"• ID joueur : {esc(d['player_id'])}",
            f"• Code : {esc(d['code'])}",
            f"• Réception : {esc(network)} au {esc(d['phone'])}",
        ]
    rows = [[{"text": "✅ Confirmer", "callback_data": "act:confirm"}], [CANCEL_BTN]]
    api.send_message(chat_id, "\n".join(lines), reply_markup=api.inline_keyboard(rows))


def _do_confirm(chat_id, client, session) -> None:
    S = TelegramSession.Step
    if session.step == S.DEP_CONFIRM:
        _confirm_deposit(chat_id, client, session)
    elif session.step == S.PAY_CONFIRM:
        _confirm_payout(chat_id, client, session)
    else:
        session.reset()
        api.send_message(chat_id, "Rien à confirmer. Tapez /start.")


def _confirm_deposit(chat_id, client, session) -> None:
    d = session.data
    bm = Bookmaker.objects.filter(code=d["bookmaker_code"]).first()
    try:
        if bm is None:
            raise services.ServiceError("Ce bookmaker n'est plus disponible.")
        tx = services.create_deposit(
            client=client, bookmaker=bm, player_id=d["player_id"], player_name=d.get("player_name", ""),
            amount=Decimal(d["amount"]), network=d["network"], phone=d["phone"],
        )
    except services.ServiceError as exc:
        session.reset()
        api.send_message(chat_id, f"⚠️ {esc(exc)}", reply_markup=api.inline_keyboard([[MENU_BTN]]))
        return

    # Consignes de paiement, puis on attend la référence du transfert.
    session.step = TelegramSession.Step.DEP_REFERENCE
    session.data = {"flow": "deposit", "tx_id": tx.id}
    session.save()
    conf = SiteSettings.load()
    number, account = conf.merchant_for(tx.network)
    lines = [
        f"📲 <b>Dépôt #{tx.id} — payez maintenant</b>",
        f"Envoyez <b>{fmt_amount(tx.amount)}</b> par {esc(tx.get_network_display())} au numéro :",
        f"👉 <code>{esc(number)}</code>" + (f" ({esc(account)})" if account else ""),
    ]
    if conf.payment_instructions:
        lines.append(esc(conf.payment_instructions))
    lines += ["", "Puis envoyez ici la <b>référence</b> (ID de transaction) du transfert reçue par SMS."]
    api.send_message(chat_id, "\n".join(lines), reply_markup=api.inline_keyboard([
        [{"text": "❌ Annuler ce dépôt", "callback_data": f"txcancel:{tx.id}"}],
    ]))


def _set_reference(chat_id, client, session, text) -> None:
    tx = Transaction.objects.filter(pk=session.data.get("tx_id"), client=client).first()
    if tx is None:
        session.reset()
        api.send_message(chat_id, "Dépôt introuvable. Tapez /start.")
        return
    try:
        tx = services.submit_deposit_reference(tx, text)
    except services.ServiceError as exc:
        api.send_message(chat_id, f"⚠️ {esc(exc)}")
        return
    session.reset()
    api.send_message(
        chat_id,
        f"✅ <b>Dépôt #{tx.id} transmis à l'équipe.</b>\nRéférence : <code>{esc(tx.payment_reference)}</code>\n"
        "Vous serez prévenu ici dès que votre compte joueur est crédité.",
        reply_markup=api.inline_keyboard([[MENU_BTN]]),
    )


def _cancel_open_deposit(chat_id, client, session, value) -> None:
    tx = Transaction.objects.filter(pk=value, client=client).first()
    if tx is None:
        api.send_message(chat_id, "Dépôt introuvable.")
        return
    try:
        services.cancel_by_client(tx)
    except services.ServiceError as exc:
        api.send_message(chat_id, f"⚠️ {esc(exc)}")
        return
    if session.data.get("tx_id") == tx.id:
        session.reset()
    api.send_message(chat_id, f"❌ Dépôt #{tx.id} annulé.", reply_markup=api.inline_keyboard([[MENU_BTN]]))


def _confirm_payout(chat_id, client, session) -> None:
    d = session.data
    bm = Bookmaker.objects.filter(code=d["bookmaker_code"]).first()
    try:
        if bm is None:
            raise services.ServiceError("Ce bookmaker n'est plus disponible.")
        tx = services.create_payout(
            client=client, bookmaker=bm, player_id=d["player_id"], player_name=d.get("player_name", ""),
            code=d["code"], network=d["network"], phone=d["phone"],
        )
    except services.ServiceError as exc:
        session.reset()
        api.send_message(chat_id, f"⚠️ {esc(exc)}", reply_markup=api.inline_keyboard([[MENU_BTN]]))
        return
    session.reset()
    api.send_message(
        chat_id,
        f"✅ <b>Retrait #{tx.id} enregistré.</b>\nUn agent vérifie votre code et vous envoie l'argent "
        f"au {esc(tx.phone)}. Vous serez prévenu ici.",
        reply_markup=api.inline_keyboard([[MENU_BTN]]),
    )
