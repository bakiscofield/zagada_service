# Zagada Service — bot Telegram de dépôt / retrait

Bot Telegram permettant aux clients de **déposer et retirer** chez leurs bookmakers
par **mobile money (Flooz, Mixx by Yas)**, avec un **admin Django** où l'équipe traite
les demandes. Le client est prévenu dans Telegram à chaque étape.

Inspiré du bot client de Waris Business, mais projet **autonome** : sa propre base,
aucun lien avec un autre backend.

## Parcours

**Dépôt** — bookmaker → ID joueur (mémorisé pour la prochaine fois) → montant
(boutons ou libre, minimum par bookmaker) → réseau → numéro → récapitulatif →
le bot affiche le **numéro marchand** à payer → le client envoie la **référence**
du transfert → la demande passe « À traiter » → l'équipe vérifie la réception,
crédite le compte joueur et marque **Réussie** → le client reçoit « ✅ Dépôt effectué ».

**Retrait** — bookmaker → ID joueur → **code de retrait** → réseau → numéro →
récapitulatif → « À traiter » → l'équipe encaisse le code chez le bookmaker,
envoie l'argent au client et marque **Réussie** (montant payé) → « ✅ Retrait payé ».

Un **clavier permanent** en bas du chat (💰 Dépôt · 💸 Retrait · 📜 Mes opérations · ☎️ Support)
lance tout d'un appui, sans commande à taper. Aussi : annulation d'un dépôt non payé, support
(WhatsApp / Telegram), commandes `/depot`, `/retrait`, `/historique`, `/support`,
`/cancel`, et `/id` (identifiant de chat, pour enregistrer un membre de l'équipe).

Garde-fous : doublon de dépôt (10 min), code de retrait déjà soumis, client
bloqué, dépôts/retraits fermables depuis l'admin, réseau masqué si aucun numéro
marchand n'est configuré.

## Admin Django (poste de l'équipe)

- **Transactions** : file triée « À traiter » en premier, filtres, recherche par
  ID joueur / code / référence / téléphone, actions **✅ Marquer réussie** et
  **❌ Rejeter** (le client est notifié), note interne et audit (qui / quand).
- **Réglages** : nom, ouverture dépôt/retrait, numéros marchands Flooz / Mixx,
  consigne de paiement, contacts support, textes.
- **Bookmakers** : actif, minimums, ordre.
- **Clients** : historique, IDs joueurs, blocage, note.
- **Chats équipe** : chats Telegram qui reçoivent l'alerte « nouvelle demande »
  (avec bouton vers l'admin si `ADMIN_BASE_URL` est renseigné).

## Installation

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env            # remplir TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET…
venv/bin/python manage.py migrate
venv/bin/python manage.py seed              # réglages + bookmakers courants
venv/bin/python manage.py createsuperuser
venv/bin/python manage.py runserver
```

Puis dans l'admin (`/admin/`) → **Réglages** : renseigner les numéros marchands
Flooz / Mixx (sans numéro, le réseau n'est pas proposé au client).

## Mise en ligne du bot

1. Créer le bot chez **@BotFather**, copier le token dans `.env`.
2. Exposer l'API en HTTPS (`TELEGRAM_WEBHOOK_BASE_URL=https://api.exemple.com`).
3. Enregistrer le webhook et le menu de commandes :
   ```bash
   venv/bin/python manage.py set_telegram_webhook      # --info / --delete
   ```
4. Chaque membre de l'équipe envoie `/id` au bot et l'admin ajoute l'identifiant
   dans **Chats équipe** pour recevoir les alertes.

## Structure

```
config/        settings, urls (admin, /api/health/, /api/telegram/)
core/          modèles (SiteSettings, Bookmaker, Client, PlayerProfile, Transaction, AdminChat),
               services.py (règles métier), admin.py, commande seed
telegrambot/   api.py (client HTTP Telegram), models.py (session = machine à états),
               flows.py (parcours), notify.py + signals.py (notifications),
               views.py/urls.py (webhook), commande set_telegram_webhook, tests.py
```

```bash
venv/bin/python manage.py test     # 17 tests
```

## Déploiement serveur (`deploy.sh`)

Domaine : **zagbot.warizone.com** (le DNS doit pointer vers le serveur avant de lancer).
Gunicorn écoute sur le **port 3036** derrière nginx, HTTPS Let's Encrypt via certbot
(installé et lancé par le script), service systemd `zagada-service`, PostgreSQL
provisionné automatiquement.

```bash
cd /home && git clone <url-du-repo> zagada_service
TELEGRAM_BOT_TOKEN=xxx ADMIN_PASSWORD=xxx sudo -E bash /home/zagada_service/deploy.sh
sudo bash /home/zagada_service/deploy.sh --update     # mises à jour suivantes
```

Avec `DOMAIN=` vide, le service tourne sur le port 3036 mais le webhook n'est pas
enregistré : Telegram exige une URL HTTPS. Options : `PORT`, `BIND=0.0.0.0`,
`SKIP_DB=1` (SQLite), `SKIP_NGINX=1`, `SKIP_CERTBOT=1`, `CERTBOT_EMAIL`.
