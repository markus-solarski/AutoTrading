import datetime
import math
import os
import random
import sys

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

from ib_insync import IB, LimitOrder, Option, Stock

# Zeitzone fuer Log-Eintraege und Datumspruefungen festlegen
BOT_TIMEZONE = zoneinfo.ZoneInfo("Europe/Berlin")

# Verbindungsparameter fuer Interactive Brokers (TWS oder IB Gateway)
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))  # 7497 = TWS Paper, 4002 = Gateway Paper
IB_CLIENT_ID = random.randint(100, 999)

# Handels- und Risikoparameter
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
CLOSE_ORDER_WAIT_SECONDS = 20       # Wartezeit auf Fill vor Stornierung
MAX_STRIKE_FALLBACKS = 5            # Fallback-Versuche bei illiquiden Strikes
MIN_VALID_BID_PRICE = 0.06          # Mindest-Bid fuer Ausfuehrung

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
        d = datetime.datetime.strptime(expiry_str, "%Y%m%d").date()
        return str(d.year) + "-" + MONATE_KURZ[d.month] + "-" + format(d.day, "02d")
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
    manuellen Terminalstart handelt.
    """
    run_type = "MANUELL" if sys.stdout.isatty() else "CRON"
    args_info = f" (Args: {' '.join(sys.argv[1:])})" if len(sys.argv) > 1 else ""
    log_cron_event(f"Skript gestartet [{run_type}]{args_info}")


def count_orders_this_month():
    """Zaehlt alle platzierten Orders im aktuellen Kalendermonat anhand von trades_log.txt."""
    if not os.path.exists(LOG_FILE):
        return 0

    current_month_prefix = datetime.datetime.now(BOT_TIMEZONE).strftime("%Y-%m")
    count = 0
    with open(LOG_FILE, "r") as f:
        for line in f:
            if line.startswith(f"[{current_month_prefix}") and "ORDER_PLACED" in line:
                count += 1
    return count


def can_open_new_trade(ib):
    """Prueft Wochenend-Sperre, Monatskontingent und Anzahl offener Positionen."""
    if is_weekend():
        log_cron_event("Abbruch: Wochenende, kein Handel moeglich.")
        return False

    orders_month = count_orders_this_month()
    if orders_month >= MAX_ORDERS_PER_MONTH:
        log_cron_event(f"Abbruch: Monatslimit erreicht ({orders_month}/{MAX_ORDERS_PER_MONTH}).")
        return False

    open_positions = [
        p for p in ib.positions()
        if p.contract.symbol == SYMBOL and p.position != 0
    ]
    if len(open_positions) >= MAX_CONCURRENT_TRADES:
        log_cron_event(f"Abbruch: Maximale Positionen erreicht ({len(open_positions)}/{MAX_CONCURRENT_TRADES}).")
        return False

    return True


def select_target_contract(ib):
    """
    Ermittelt den Ziel-Put gemaess DTE (30-41 Tage), Basispreis-Discount und
    prueft die Verfuegbarkeit von Marktdaten sowie Liquiditaet ueber das Orderbuch.
    """
    stock = Stock(SYMBOL, EXCHANGE, CURRENCY)
    ib.qualifyContracts(stock)

    ticker = ib.reqMktData(stock, "", False, False)
    ib.sleep(2)

    current_price = ticker.marketPrice()
    if not current_price or math.isnan(current_price):
        current_price = ticker.close

    if not current_price or math.isnan(current_price):
        log_cron_event("Fehler: Aktueller Basiswert-Kurs konnte nicht ermittelt werden.")
        return None, None

    chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
    if not chains:
        log_cron_event("Fehler: Keine Optionsketten fuer das Symbol erhalten.")
        return None, None

    chain = next((c for c in chains if c.exchange == EXCHANGE or c.exchange == PRIMARY_EXCHANGE), chains[0])

    today = datetime.datetime.now(BOT_TIMEZONE).date()
    valid_expirations = [
        exp for exp in chain.expirations
        if MIN_DAYS <= (datetime.datetime.strptime(exp, "%Y%m%d").date() - today).days <= MAX_DAYS
    ]

    if not valid_expirations:
        log_cron_event("Abbruch: Kein Verfallstag innerhalb des DTE-Fensters gefunden.")
        return None, None

    target_exp = sorted(valid_expirations)[0]
    ideal_strike = current_price * (1.0 - DISCOUNT)

    strikes_below_target = sorted([s for s in chain.strikes if s <= ideal_strike], reverse=True)
    if not strikes_below_target:
        log_cron_event("Abbruch: Keine Strikes unterhalb des Ziel-Discounts verfuegbar.")
        return None, None

    for strike in strikes_below_target[:MAX_STRIKE_FALLBACKS]:
        opt = Option(SYMBOL, target_exp, strike, "P", EXCHANGE, currency=CURRENCY)
        qualified = ib.qualifyContracts(opt)
        if not qualified:
            continue

        opt_ticker = ib.reqMktData(opt, "", False, False)
        ib.sleep(1)

        bid = opt_ticker.bid
        if bid and not math.isnan(bid) and bid >= MIN_VALID_BID_PRICE:
            return opt, bid

    log_cron_event("Abbruch: Kein Strike erfuellt die Mindest-Bid-Kriterien.")
    return None, None


def place_short_put_with_tp(ib, contract, bid_price):
    """
    Platziert den Short-Put zum aktuellen Bid-Limit und setzt nach erfolgreicher
    Ausfuehrung automatisch eine Rueckkauf-Order zum 50%-Take-Profit.
