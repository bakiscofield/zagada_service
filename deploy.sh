#!/usr/bin/env bash
# ============================================================================
# ZAGADA SERVICE — déploiement COMPLET sur un serveur Linux (Debian/Ubuntu).
# Paquets système, PostgreSQL, service systemd (gunicorn sur le port 3036),
# nginx + HTTPS (certbot) et enregistrement du webhook Telegram.
# Idempotent : relançable sans danger.
#
# Première installation (depuis un clone du repo) :
#     cd /home && git clone <url-du-repo> zagada_service
#     TELEGRAM_BOT_TOKEN=xxx sudo -E bash /home/zagada_service/deploy.sh
#     (le DNS de zagbot.warizone.com doit déjà pointer vers ce serveur)
#
# Mise à jour (git pull + dépendances + migrations + redémarrage) :
#     sudo bash /home/zagada_service/deploy.sh --update
#
# Variables (optionnelles, sauf le token à la 1re installation) :
#   TELEGRAM_BOT_TOKEN=...   token @BotFather (ou déjà dans .env)
#   DOMAIN=zagbot.warizone.com   domaine public (défaut) → nginx + certbot HTTPS + webhook.
#                            DOMAIN= (vide) : pas de nginx ni de webhook
#                            (Telegram exige HTTPS ; le port 3036 seul ne suffit pas).
#   CERTBOT_EMAIL=...        e-mail Let's Encrypt (sinon certbot en --register-unsafely-without-email)
#   PORT=3036                port d'écoute de gunicorn
#   BIND=127.0.0.1           interface d'écoute (0.0.0.0 pour exposer le port directement)
#   ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL   crée le super-admin Django
#   DB_PASSWORD=...          mot de passe Postgres (sinon généré)
#   SKIP_APT=1 SKIP_DB=1 SKIP_NGINX=1 SKIP_CERTBOT=1 SKIP_WEBHOOK=1
#   SKIP_DB=1 → SQLite (db.sqlite3) au lieu de PostgreSQL.
# ============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SERVICE="zagada-service"
VENV="${VENV:-venv}"
PORT="${PORT:-3036}"
BIND="${BIND:-127.0.0.1}"
DB_NAME="${DB_NAME:-zagada}"
DB_USER="${DB_USER:-zagada}"
DOMAIN="${DOMAIN:-zagbot.warizone.com}"
MODE="${1:-install}"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

C_STEP="\033[1;36m"; C_OK="\033[1;32m"; C_WARN="\033[1;33m"; C_ERR="\033[1;31m"; C_OFF="\033[0m"
step() { printf "\n${C_STEP}▸ %s${C_OFF}\n" "$1"; }
ok()   { printf "  ${C_OK}✓ %s${C_OFF}\n" "$1"; }
warn() { printf "  ${C_WARN}! %s${C_OFF}\n" "$1"; }
die()  { printf "  ${C_ERR}✗ %s${C_OFF}\n" "$1"; exit 1; }

[ -f "$APP_DIR/manage.py" ] || die "manage.py introuvable — lance le script depuis le repo cloné."
cd "$APP_DIR"

# Lit une clé dans .env (vide si absente).
env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }
# Écrit/remplace une clé dans .env.
env_set() {
    if grep -qE "^$1=" .env; then
        sed -i "s|^$1=.*|$1=$2|" .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

# ============================================================================
# Mode --update : pull, dépendances, migrations, statics, redémarrage.
# ============================================================================
if [ "$MODE" = "--update" ] || [ "$MODE" = "update" ]; then
    step "Mise à jour du code"
    if [ -d .git ]; then
        git pull --ff-only && ok "git pull" || warn "git pull a échoué — code local conservé"
    else
        warn "pas de dépôt git : code local utilisé tel quel"
    fi
    step "Dépendances, migrations, statics"
    "$VENV/bin/pip" install -q -r requirements.txt
    "$VENV/bin/python" manage.py migrate --noinput
    "$VENV/bin/python" manage.py collectstatic --noinput >/dev/null
    step "Redémarrage"
    $SUDO systemctl restart "$SERVICE"
    sleep 2
    $SUDO systemctl is-active "$SERVICE" >/dev/null && ok "$SERVICE redémarré" \
        || { $SUDO journalctl -u "$SERVICE" -n 30 --no-pager; die "$SERVICE n'a pas redémarré"; }
    exit 0
fi

# ============================================================================
# 1. Paquets système
# ============================================================================
if [ "${SKIP_APT:-0}" != "1" ]; then
    step "Paquets système (python, postgres, nginx, certbot)"
    # nginx + certbot sont installés dès qu'un domaine est défini : le webhook
    # Telegram exige HTTPS, ce n'est pas optionnel en production.
    export DEBIAN_FRONTEND=noninteractive
    $SUDO apt-get update -qq
    PKGS="python3 python3-venv python3-pip build-essential curl openssl"
    [ "${SKIP_DB:-0}" != "1" ] && PKGS="$PKGS postgresql postgresql-contrib libpq-dev"
    [ "${SKIP_NGINX:-0}" != "1" ] && [ -n "$DOMAIN" ] && PKGS="$PKGS nginx"
    [ "${SKIP_CERTBOT:-0}" != "1" ] && [ -n "$DOMAIN" ] && PKGS="$PKGS certbot python3-certbot-nginx"
    # shellcheck disable=SC2086
    $SUDO apt-get install -y -qq $PKGS
    [ "${SKIP_DB:-0}" != "1" ] && $SUDO systemctl enable --now postgresql
    ok "paquets installés"
else
    warn "SKIP_APT=1 — paquets système ignorés"
fi

# ============================================================================
# 2. Virtualenv + dépendances
# ============================================================================
step "Virtualenv + dépendances"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements.txt
ok "venv prêt"

# ============================================================================
# 3. .env (secrets générés, token et domaine renseignés)
# ============================================================================
step ".env"
if [ ! -f .env ]; then
    cp .env.example .env
    env_set SECRET_KEY "$("$VENV/bin/python" -c 'import secrets;print(secrets.token_urlsafe(50))')"
    env_set DEBUG False
    ok ".env créé (SECRET_KEY générée)"
else
    ok ".env déjà présent — seules les valeurs manquantes sont complétées"
fi

TOKEN="${TELEGRAM_BOT_TOKEN:-$(env_get TELEGRAM_BOT_TOKEN)}"
if [ -z "$TOKEN" ] && [ -t 0 ]; then
    read -r -p "  Token du bot (@BotFather) : " TOKEN
fi
[ -n "$TOKEN" ] || die "TELEGRAM_BOT_TOKEN manquant (variable d'environnement ou .env)."
env_set TELEGRAM_BOT_TOKEN "$TOKEN"

[ -n "$(env_get TELEGRAM_WEBHOOK_SECRET)" ] || env_set TELEGRAM_WEBHOOK_SECRET "$(openssl rand -hex 32)"

HOSTS="127.0.0.1,localhost"
[ -n "$DOMAIN" ] && HOSTS="${DOMAIN},${HOSTS}"
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$SERVER_IP" ] && HOSTS="${HOSTS},${SERVER_IP}"
env_set ALLOWED_HOSTS "$HOSTS"
if [ -n "$DOMAIN" ]; then
    env_set TELEGRAM_WEBHOOK_BASE_URL "https://${DOMAIN}"
    env_set ADMIN_BASE_URL "https://${DOMAIN}"
fi
ok "token, secret webhook, hôtes et URLs écrits"

# ============================================================================
# 4. PostgreSQL (idempotent) — ou SQLite si SKIP_DB=1
# ============================================================================
if [ "${SKIP_DB:-0}" != "1" ]; then
    step "PostgreSQL : base '${DB_NAME}'"
    if [ -n "$(env_get DATABASE_URL)" ]; then
        ok "DATABASE_URL déjà configuré — provisioning ignoré"
    else
        DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-24)}"
        if [ "$(id -u)" -eq 0 ]; then PG=(runuser -u postgres --); else PG=(sudo -u postgres); fi
        psql_q() { "${PG[@]}" psql -v ON_ERROR_STOP=1 -tAc "$1"; }
        if [ "$(psql_q "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'")" = "1" ]; then
            psql_q "ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';"
        else
            psql_q "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';"
        fi
        if [ "$(psql_q "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")" != "1" ]; then
            psql_q "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8' TEMPLATE template0;"
        fi
        psql_q "ALTER DATABASE ${DB_NAME} OWNER TO ${DB_USER};"
        psql_q "ALTER ROLE ${DB_USER} SET client_encoding TO 'utf8';"
        psql_q "ALTER ROLE ${DB_USER} SET default_transaction_isolation TO 'read committed';"
        env_set DATABASE_URL "postgres://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"
        ok "base provisionnée + DATABASE_URL écrit dans .env"
    fi
else
    warn "SKIP_DB=1 — SQLite (db.sqlite3) utilisé"
fi

# ============================================================================
# 5. Migrations, seed, statics, super-admin
# ============================================================================
step "Migrations, seed, statics"
"$VENV/bin/python" manage.py migrate --noinput
"$VENV/bin/python" manage.py seed || warn "seed a échoué (non bloquant)"
"$VENV/bin/python" manage.py collectstatic --noinput >/dev/null
ok "schéma à jour + statics collectés"

if [ -n "${ADMIN_PASSWORD:-}" ]; then
    step "Super-admin Django"
    DJANGO_SUPERUSER_USERNAME="${ADMIN_USERNAME:-admin}" \
    DJANGO_SUPERUSER_EMAIL="${ADMIN_EMAIL:-admin@${DOMAIN:-localhost}}" \
    DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASSWORD" \
        "$VENV/bin/python" manage.py createsuperuser --noinput 2>/dev/null \
        && ok "super-admin '${ADMIN_USERNAME:-admin}' créé" || ok "super-admin déjà existant"
fi

# ============================================================================
# 6. Service systemd — gunicorn sur ${BIND}:${PORT}
# ============================================================================
step "Service systemd ${SERVICE} (port ${PORT})"
$SUDO tee /etc/systemd/system/${SERVICE}.service >/dev/null <<UNIT
[Unit]
Description=Zagada Service — bot Telegram (Django/Gunicorn)
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/${VENV}/bin/gunicorn --workers 3 --bind ${BIND}:${PORT} --access-logfile - --error-logfile - --timeout 60 --graceful-timeout 30 config.wsgi:application
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE"
$SUDO systemctl restart "$SERVICE"
sleep 2
if $SUDO systemctl is-active "$SERVICE" >/dev/null; then
    ok "$SERVICE actif"
else
    $SUDO journalctl -u "$SERVICE" -n 30 --no-pager
    die "$SERVICE n'a pas démarré"
fi
if curl -fsS "http://127.0.0.1:${PORT}/api/health/" >/dev/null 2>&1; then
    ok "réponse OK sur http://127.0.0.1:${PORT}/api/health/"
else
    warn "pas de réponse sur le port ${PORT} (vérifie : journalctl -u ${SERVICE} -f)"
fi

# ============================================================================
# 7. Nginx + HTTPS (uniquement avec un domaine)
# ============================================================================
if [ -z "$DOMAIN" ]; then
    warn "DOMAIN non fourni — nginx, HTTPS et webhook ignorés."
    warn "Telegram n'accepte que des webhooks HTTPS : relance avec DOMAIN=bot.mondomaine.com"
elif [ "${SKIP_NGINX:-0}" != "1" ]; then
    step "Nginx → ${DOMAIN} ⇒ 127.0.0.1:${PORT}"
    $SUDO tee /etc/nginx/sites-available/${SERVICE}.conf >/dev/null <<NGX
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    client_max_body_size 10M;

    location /static/ {
        alias ${APP_DIR}/staticfiles/;
        expires 30d;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
NGX
    $SUDO ln -sf /etc/nginx/sites-available/${SERVICE}.conf /etc/nginx/sites-enabled/${SERVICE}.conf
    [ -e /etc/nginx/sites-enabled/default ] && $SUDO rm -f /etc/nginx/sites-enabled/default
    $SUDO nginx -t && $SUDO systemctl enable --now nginx && $SUDO systemctl reload nginx
    ok "nginx configuré"

    if [ "${SKIP_CERTBOT:-0}" != "1" ]; then
        step "HTTPS (Let's Encrypt) pour ${DOMAIN}"
        if [ -d "/etc/letsencrypt/live/${DOMAIN}" ]; then
            ok "certificat déjà présent"
        else
            if [ -n "${CERTBOT_EMAIL:-}" ]; then MAIL=(--email "$CERTBOT_EMAIL"); else MAIL=(--register-unsafely-without-email); fi
            if $SUDO certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${MAIL[@]}"; then
                ok "certificat obtenu, redirection HTTP→HTTPS active"
            else
                warn "certbot a échoué (le DNS de ${DOMAIN} pointe-t-il vers ce serveur ?)."
                warn "Relance plus tard : ${SUDO} certbot --nginx -d ${DOMAIN}"
            fi
        fi
    fi
else
    warn "SKIP_NGINX=1 — nginx ignoré"
fi

# ============================================================================
# 8. Webhook Telegram
# ============================================================================
if [ -n "$DOMAIN" ] && [ "${SKIP_WEBHOOK:-0}" != "1" ]; then
    step "Webhook Telegram"
    if curl -fsSI "https://${DOMAIN}/api/health/" >/dev/null 2>&1; then
        "$VENV/bin/python" manage.py set_telegram_webhook && ok "webhook enregistré" \
            || warn "échec set_telegram_webhook — relance : ${VENV}/bin/python manage.py set_telegram_webhook"
    else
        warn "https://${DOMAIN} ne répond pas encore — webhook non enregistré."
        warn "Quand le HTTPS est en place : ${VENV}/bin/python manage.py set_telegram_webhook"
    fi
fi

# ============================================================================
# 9. Récapitulatif
# ============================================================================
step "Terminé"
BOT_USERNAME="$("$VENV/bin/python" -c "
from decouple import config; import requests
try: print(requests.get('https://api.telegram.org/bot'+config('TELEGRAM_BOT_TOKEN')+'/getMe', timeout=10).json()['result']['username'])
except Exception: print('?')
")"
cat <<EOT
  Dossier   : ${APP_DIR}
  Service   : ${SERVICE}  (gunicorn ${BIND}:${PORT})
  Admin     : ${DOMAIN:+https://${DOMAIN}/admin/}${DOMAIN:-http://${SERVER_IP:-IP}:${PORT}/admin/}
  Bot       : t.me/${BOT_USERNAME}

  Étapes restantes :
    • Super-admin (si non créé) : ${VENV}/bin/python manage.py createsuperuser
    • Admin → Réglages : numéros marchands Flooz / Mixx, contacts support
    • Chaque membre de l'équipe envoie /id au bot → Admin → Chats équipe
    • Logs : journalctl -u ${SERVICE} -f

  Mise à jour : ${SUDO} bash ${APP_DIR}/deploy.sh --update
EOT
