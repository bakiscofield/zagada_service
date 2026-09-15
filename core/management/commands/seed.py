"""Données de départ : réglages + bookmakers courants (idempotent).

    python manage.py seed
"""
from django.core.management.base import BaseCommand

from core.models import Bookmaker, SiteSettings

BOOKMAKERS = [
    ("1xbet", "1xBet"), ("melbet", "Melbet"), ("betwinner", "Betwinner"),
    ("1win", "1win"), ("premierbet", "Premier Bet"), ("888starz", "888starz"),
    ("linebet", "Linebet"), ("betclic", "Betclic"),
]


class Command(BaseCommand):
    help = "Crée les réglages et les bookmakers par défaut (sans écraser l'existant)."

    def handle(self, *args, **opts):
        SiteSettings.load()
        created = 0
        for order, (code, name) in enumerate(BOOKMAKERS):
            _, was_created = Bookmaker.objects.get_or_create(code=code, defaults={"name": name, "order": order})
            created += was_created
        self.stdout.write(self.style.SUCCESS(
            f"Réglages OK, {created} bookmaker(s) créé(s), {Bookmaker.objects.count()} au total. "
            "Renseignez les numéros marchands dans Admin → Réglages."
        ))
