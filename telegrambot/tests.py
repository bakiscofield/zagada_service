"""Tests du parcours conversationnel et des notifications.

On simule des updates Telegram et on capture les envois en moquant les
fonctions réseau de `telegrambot.api`. Les services métier sont réels.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core import services
from core.models import AdminChat, Bookmaker, Client, PlayerProfile, SiteSettings, Transaction
from telegrambot import api, flows
from telegrambot.models import TelegramSession

CHAT_ID = 555001
TEAM_CHAT = 999001


def _msg(text, chat_id=CHAT_ID):
    return {"message": {"chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id, "first_name": "Théo", "username": "theo"}, "text": text}}


def _cb(data, chat_id=CHAT_ID):
    return {"callback_query": {"id": "cb1", "data": data,
                               "from": {"id": chat_id, "first_name": "Théo", "username": "theo"},
                               "message": {"chat": {"id": chat_id}, "message_id": 7}}}


class BotTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.bm = Bookmaker.objects.create(code="1xbet", name="1xBet", min_deposit=Decimal("500"))
        cls.bm2 = Bookmaker.objects.create(code="melbet", name="Melbet", order=2)
        conf = SiteSettings.load()
        conf.flooz_number = "97000000"
        conf.flooz_account_name = "ZAGADA"
        conf.tmoney_number = "70000000"
        conf.support_whatsapp = "+22890000000"
        conf.save()
        AdminChat.objects.create(chat_id=TEAM_CHAT, label="Boss")
        cls.admin = get_user_model().objects.create_superuser("admin", "a@a.tg", "x")

    def setUp(self):
        self.sent = mock.patch.object(api, "send_message", return_value={"message_id": 1}).start()
        mock.patch.object(api, "answer_callback_query", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def texts(self, chat_id=CHAT_ID):
        return [c.kwargs.get("text", c.args[1]) for c in self.sent.call_args_list if c.args[0] == chat_id]

    def buttons(self, chat_id=CHAT_ID):
        out = []
        for c in self.sent.call_args_list:
            if c.args[0] != chat_id:
                continue
            for row in (c.kwargs.get("reply_markup") or {}).get("inline_keyboard", []):
                out += [b.get("callback_data") or b.get("url") for b in row]
        return out

    def session(self):
        return TelegramSession.objects.get(chat_id=CHAT_ID)

    def run_deposit_until_confirm(self):
        flows.handle_update(_msg("/start"))
        flows.handle_update(_cb("act:deposit"))
        flows.handle_update(_cb("bm:1xbet"))
        flows.handle_update(_msg("123456"))
        flows.handle_update(_cb("amt:2000"))
        flows.handle_update(_cb("net:FLOOZ"))
        flows.handle_update(_msg("90 00 00 00"))
        self.assertEqual(self.session().step, TelegramSession.Step.DEP_CONFIRM)


class DepositFlowTests(BotTestCase):
    def test_start_creates_client_and_shows_menu(self):
        flows.handle_update(_msg("Bonjour !"))
        client = Client.objects.get(telegram_id=CHAT_ID)
        self.assertEqual(client.username, "theo")
        kb = self.sent.call_args.kwargs["reply_markup"]
        labels = [b["text"] for row in kb["keyboard"] for b in row]
        self.assertEqual(labels, ["💰 Dépôt", "💸 Retrait", "📜 Mes opérations", "☎️ Support"])
        self.assertTrue(kb["is_persistent"])

    def test_keyboard_labels_start_flows(self):
        flows.handle_update(_msg("/start"))
        flows.handle_update(_msg("💰 Dépôt"))
        self.assertEqual(self.session().step, TelegramSession.Step.DEP_BOOKMAKER)
        flows.handle_update(_msg("💸 Retrait"))
        self.assertEqual(self.session().step, TelegramSession.Step.PAY_BOOKMAKER)
        flows.handle_update(_msg("📜 Mes opérations"))
        self.assertIn("Aucune opération", self.texts()[-1])
        flows.handle_update(_msg("☎️ Support"))
        self.assertIn("Support", self.texts()[-1])

    def test_full_deposit_then_reference_alerts_team(self):
        self.run_deposit_until_confirm()
        flows.handle_update(_cb("act:confirm"))

        tx = Transaction.objects.get()
        self.assertEqual(tx.status, Transaction.Status.AWAITING_PAYMENT)
        self.assertEqual(tx.amount, Decimal("2000"))
        self.assertEqual(tx.phone, "90000000")
        self.assertEqual(tx.network, "FLOOZ")
        # Consignes : numéro marchand affiché, pas encore d'alerte équipe.
        self.assertTrue(any("97000000" in t and "ZAGADA" in t for t in self.texts()))
        self.assertEqual(self.texts(TEAM_CHAT), [])
        self.assertEqual(self.session().step, TelegramSession.Step.DEP_REFERENCE)

        flows.handle_update(_msg("TX-ABC-123"))
        tx.refresh_from_db()
        self.assertEqual(tx.status, Transaction.Status.PENDING)
        self.assertEqual(tx.payment_reference, "TX-ABC-123")
        self.assertEqual(self.session().step, TelegramSession.Step.IDLE)
        team = self.texts(TEAM_CHAT)
        self.assertEqual(len(team), 1)
        self.assertIn("TX-ABC-123", team[0])
        self.assertIn("2 000 F", team[0])
        # Profil joueur et numéro mémorisés
        self.assertTrue(PlayerProfile.objects.filter(client__telegram_id=CHAT_ID, player_id="123456").exists())
        self.assertEqual(Client.objects.get(telegram_id=CHAT_ID).phone, "90000000")

    def test_saved_profile_and_last_phone_are_proposed(self):
        self.run_deposit_until_confirm()
        flows.handle_update(_cb("act:confirm"))
        flows.handle_update(_msg("REF1"))
        self.sent.reset_mock()
        flows.handle_update(_cb("act:deposit"))
        flows.handle_update(_cb("bm:1xbet"))
        self.assertIn("pid:0", self.buttons())
        flows.handle_update(_cb("pid:0"))
        self.assertEqual(self.session().data["player_id"], "123456")
        flows.handle_update(_cb("amt:5000"))
        flows.handle_update(_cb("net:FLOOZ"))
        self.assertIn("phone:last", self.buttons())
        flows.handle_update(_cb("phone:last"))
        self.assertEqual(self.session().data["phone"], "90000000")

    def test_amount_below_min_rejected(self):
        flows.handle_update(_msg("/start"))
        flows.handle_update(_cb("act:deposit"))
        flows.handle_update(_cb("bm:1xbet"))
        flows.handle_update(_msg("123456"))
        flows.handle_update(_cb("amt:new"))
        flows.handle_update(_msg("100"))
        self.assertEqual(self.session().step, TelegramSession.Step.DEP_AMOUNT)
        self.assertTrue(any("minimum" in t for t in self.texts()))
        flows.handle_update(_msg("1 500"))
        self.assertEqual(self.session().data["amount"], "1500")

    def test_client_can_cancel_unpaid_deposit(self):
        self.run_deposit_until_confirm()
        flows.handle_update(_cb("act:confirm"))
        tx = Transaction.objects.get()
        flows.handle_update(_cb(f"txcancel:{tx.id}"))
        tx.refresh_from_db()
        self.assertEqual(tx.status, Transaction.Status.CANCELLED)
        self.assertEqual(self.session().step, TelegramSession.Step.IDLE)
        # Un dépôt déjà transmis ne s'annule plus
        self.run_deposit_until_confirm()
        flows.handle_update(_cb("act:confirm"))
        tx2 = Transaction.objects.exclude(pk=tx.pk).get()
        flows.handle_update(_msg("REF2"))
        flows.handle_update(_cb(f"txcancel:{tx2.id}"))
        tx2.refresh_from_db()
        self.assertEqual(tx2.status, Transaction.Status.PENDING)

    def test_duplicate_deposit_refused(self):
        self.run_deposit_until_confirm()
        flows.handle_update(_cb("act:confirm"))
        self.run_deposit_until_confirm()
        flows.handle_update(_cb("act:confirm"))
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertTrue(any("déjà en cours" in t for t in self.texts()))

    def test_network_without_merchant_number_hidden(self):
        conf = SiteSettings.load()
        conf.tmoney_number = ""
        conf.save()
        flows.handle_update(_msg("/start"))
        flows.handle_update(_cb("act:deposit"))
        flows.handle_update(_cb("bm:1xbet"))
        flows.handle_update(_msg("1"))
        flows.handle_update(_cb("amt:1000"))
        self.assertIn("net:FLOOZ", self.buttons())
        self.assertNotIn("net:TMONEY", self.buttons())

    def test_deposit_closed(self):
        conf = SiteSettings.load()
        conf.deposit_enabled = False
        conf.save()
        flows.handle_update(_msg("/depot"))
        self.assertEqual(self.session().step, TelegramSession.Step.IDLE)
        self.assertTrue(any("indisponible" in t for t in self.texts()))

    def test_blocked_client(self):
        flows.handle_update(_msg("/start"))
        Client.objects.filter(telegram_id=CHAT_ID).update(is_blocked=True)
        flows.handle_update(_cb("act:deposit"))
        self.assertTrue(any("bloqué" in t for t in self.texts()))
        self.assertEqual(self.session().step, TelegramSession.Step.IDLE)

    def test_cancel_command_resets(self):
        self.run_deposit_until_confirm()
        flows.handle_update(_msg("/cancel"))
        self.assertEqual(self.session().step, TelegramSession.Step.IDLE)
        self.assertEqual(Transaction.objects.count(), 0)


class PayoutFlowTests(BotTestCase):
    def test_full_payout(self):
        flows.handle_update(_msg("/retrait"))
        flows.handle_update(_cb("bm:melbet"))
        flows.handle_update(_msg("999"))
        flows.handle_update(_msg("CODE123"))
        flows.handle_update(_cb("net:TMONEY"))
        flows.handle_update(_msg("91111111"))
        self.assertEqual(self.session().step, TelegramSession.Step.PAY_CONFIRM)
        flows.handle_update(_cb("act:confirm"))
        tx = Transaction.objects.get()
        self.assertEqual(tx.kind, Transaction.Kind.PAYOUT)
        self.assertEqual(tx.status, Transaction.Status.PENDING)
        self.assertEqual(tx.payout_code, "CODE123")
        self.assertEqual(tx.network, "TMONEY")
        self.assertIn("CODE123", self.texts(TEAM_CHAT)[0])

    def test_duplicate_code_refused(self):
        for _ in range(2):
            flows.handle_update(_msg("/retrait"))
            flows.handle_update(_cb("bm:melbet"))
            flows.handle_update(_msg("999"))
            flows.handle_update(_msg("code123"))
            flows.handle_update(_cb("net:TMONEY"))
            flows.handle_update(_msg("91111111"))
            flows.handle_update(_cb("act:confirm"))
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertTrue(any("déjà été soumis" in t for t in self.texts()))


class NotificationTests(BotTestCase):
    def _pending_payout(self):
        client = services.get_or_create_client({"id": CHAT_ID, "first_name": "Théo"})
        return services.create_payout(client=client, bookmaker=self.bm, player_id="1",
                                      code="C1", network="FLOOZ", phone="90000000")

    def test_client_notified_on_success_and_rejection(self):
        tx = self._pending_payout()
        self.sent.reset_mock()
        services.mark_success(tx, self.admin, amount=Decimal("12000"))
        self.assertTrue(any("payé" in t and "12 000 F" in t for t in self.texts()))
        client = Client.objects.get(telegram_id=CHAT_ID)
        dep = services.create_deposit(client=client, bookmaker=self.bm, player_id="1",
                                      amount=Decimal("1000"), network="FLOOZ", phone="90000000")
        services.submit_deposit_reference(dep, "R")
        self.sent.reset_mock()
        services.mark_rejected(dep, self.admin, note="Paiement non reçu")
        self.assertTrue(any("refusé" in t and "Paiement non reçu" in t for t in self.texts()))

    def test_closed_transaction_cannot_be_decided_twice(self):
        tx = self._pending_payout()
        services.mark_success(tx, self.admin)
        with self.assertRaises(services.ServiceError):
            services.mark_rejected(tx, self.admin)

    def test_history_and_id_command(self):
        self._pending_payout()
        flows.handle_update(_msg("/historique"))
        self.assertIn("À traiter", self.texts()[-1])
        flows.handle_update(_msg("/id"))
        self.assertIn(str(CHAT_ID), self.texts()[-1])

    def test_support_links(self):
        flows.handle_update(_msg("/support"))
        self.assertIn("https://wa.me/22890000000", self.buttons())


@override_settings(TELEGRAM_WEBHOOK_SECRET="s3cret")
class WebhookTests(BotTestCase):
    def test_secret_required_in_path_and_header(self):
        c = APIClient()
        self.assertEqual(c.post("/api/telegram/webhook/wrong/", {}, format="json",
                                HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="s3cret").status_code, 403)
        self.assertEqual(c.post("/api/telegram/webhook/s3cret/", {}, format="json").status_code, 403)
        r = c.post("/api/telegram/webhook/s3cret/", _msg("/start"), format="json",
                   HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="s3cret")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Client.objects.filter(telegram_id=CHAT_ID).exists())
