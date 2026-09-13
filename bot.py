import math
import os
import datetime
import sys
import random

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

# Zeitzone fuer Log-Eintraege und Datumspruefungen festlegen
BOT_TIMEZONE = zoneinfo.ZoneInfo("Europe/Berlin")

from ib_insync import IB, Stock, Option, LimitOrder

MAX_ORDERS_PER_MONTH = 19
MAX_CONCURRENT_TRADES = 5
LOG_FILE = "trades_log.txt"
CRON_LOG_FILE = "cron.log"
SYMBOL = "EEM"
CURRENCY = "USD"
EXCHANGE = "SMART"
PRIMARY_EXCHANGE = "ARCA"

DISCOUNT = 0.093
MIN_DAYS = 30
MAX_DAYS = 41

TAKE_PROFIT_FRACTION = 0.5          # 50% der erhaltenen Praemie
CLOSE_ORDER_WAIT_SECONDS = 20       # wie lange nach Order-Platzierung auf Fill gewartet wird
MAX_STRIKE_FALLBACKS = 5            # wie viele tiefere Strikes probiert werden, falls ein Strike ungueltig ist
MIN_VALID_BID_PRICE = 0.06          # Bid-Preise UNTER diesem Wert gelten als zu niedrig/illiquide

MONATE_KURZ = {
    1: "Jan", 2: "Feb", 3: "Mär", 4: "Apr", 5: "Mai", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Okt", 11: "Nov", 12: "Dez"
}


def format_expiry(expiry_str):
    """
    Wandelt ein IB-Verfallsdatum im Format 'YYYYMMDD' (z.B. '20261009') in das
    Anzeigeformat 'YYYY-Mon-DD' mit deutschem 3-Buchstaben-Monat um
    (z.B. '2026-Okt-09'). Wird NUR fuer Terminal-/Log-Ausgaben genutzt - die
    IB-API selbst braucht weiterhin das rohe 'YYYYMMDD'-Format.
    """
    try:
        d = datetime.datetime.strptime(expiry_str, '%Y%m%d').date()
        return str(d.year) + "-" + MONATE_KURZ[d.month] + "-" + format(d.day, '02d')
    except (ValueError, TypeError):
        return expiry_str


def is_weekend():
    """Samstag = 5, Sonntag = 6 (Montag = 0). Keine Orders am Wochenende."""
    return datetime.datetime.now(BOT_TIMEZONE).date().weekday() >= 5


def log_cron_event(details):
    """
    Schreibt eine Zeile mit Zeitstempel in cron.log. Nutzt die konfigurierte
    Zeitzone (BOT_TIMEZONE), unabhaengig von der Systemzeitzone des Host-Servers.
    """
    now_str = datetime.datetime.now(BOT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    with open(CRON_LOG_FILE, "a") as f:
        f.write("[" + now_str + "] " + details + "\n")


def log_cron_run():
    """
    Schreibt bei JEDEM Skriptstart sofort einen Eintrag in cron.log - inklusive
    Kennzeichnung, ob es sich um einen automatischen Cron-Lauf oder einen
    manue
