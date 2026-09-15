"""Petits formateurs partagés par les flux et les notifications."""
from __future__ import annotations

import html
from decimal import Decimal, InvalidOperation


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def fmt_amount(value) -> str:
    """5000 → « 5 000 F »."""
    try:
        d = Decimal(str(value if value is not None else 0))
    except InvalidOperation:
        return f"{value} F"
    return f"{int(d):,}".replace(",", " ") + " F"
