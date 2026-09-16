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


BOT_TIMEZONE = zoneinfo.ZoneInfo("Europe/Berlin")

# IB Gateway laeuft lokal im Docker-Container.
# Der Host-Port ist laut docker ps: 127.0.0.1:4001 -> Container-Port 4003.
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4001"))
IB_CLIENT_ID = random.randint(100, 999)

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

TAKE_PROFIT_FRACTION = 0.5
CLOSE_ORDER_WAIT_SECONDS = 20
MAX_STRIKE_FALLBACKS = 5
MIN_VALID_BID_PRICE = 0.06

MONATE_KURZ = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "Mai",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Okt",
    11: "Nov",
    12: "Dez",
}


def format_expiry(expiry_str):
    try:
        d = datetime.datetime.strptime(expiry_str, "%Y%m%d").date()
        return str(d.year) + "-" + MONATE_KURZ[d.month] + "-" + format(d.day, "02d")
    except (ValueError, TypeError):
        return expiry_str


def is_weekend():
    return datetime.datetime.now(BOT_TIMEZONE).date().weekday() >= 5


def log_cron_event(details):
    now_str = datetime.datetime.now(BOT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    with open(CRON_LOG_FILE, "a", encoding="utf-8") as f:
        f.write("[" + now_str + "] " + details + "\n")


def log_cron_run():
    run_type = "MANUELL" if sys.stdout.isatty() else "CRON"

    if len(sys.argv) > 1:
        args_info = " (Args: " + " ".join(sys.argv[1:]) + ")"
    else:
        args_info = ""

    log_cron_event("Skript gestartet [" + run_type + "]" + args_info)


def count_orders_this_month():
    if not os.path.exists(LOG_FILE):
        return 0

    current_month_prefix = datetime.datetime.now(BOT_TIMEZONE).strftime("%Y-%m")
    count = 0

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("[" + current_month_prefix) and "ORDER_PLACED" in line:
                count += 1

    return count


def can_open_new_trade(ib):
    if is_weekend():
        log_cron_event("Abbruch: Wochenende, kein Handel moeglich.")
        return False

    orders_month = count_orders_this_month()

    if orders_month >= MAX_ORDERS_PER_MONTH:
        log_cron_event(
            "Abbruch: Monatslimit erreicht ("
            + str(orders_month)
            + "/"
            + str(MAX_ORDERS_PER_MONTH)
            + ")."
        )
        return False

    open_positions = [
        position
        for position in ib.positions()
        if position.contract.symbol == SYMBOL and position.position != 0
    ]

    if len(open_positions) >= MAX_CONCURRENT_TRADES:
        log_cron_event(
            "Abbruch: Maximale Positionen erreicht ("
            + str(len(open_positions))
            + "/"
            + str(MAX_CONCURRENT_TRADES)
            + ")."
        )
        return False

    return True


def get_current_price(ib, stock):
    ticker = ib.reqMktData(stock, "", False, False)
    ib.sleep(2)

    current_price = ticker.marketPrice()

    if not current_price or math.isnan(current_price):
        current_price = ticker.close

    if not current_price or math.isnan(current_price):
        return None

    return current_price


def select_target_contract(ib):
    stock = Stock(SYMBOL, EXCHANGE, CURRENCY)
    qualified_stock = ib.qualifyContracts(stock)

    if not qualified_stock:
        log_cron_event("Fehler: Basiswert-Kontrakt konnte nicht qualifiziert werden.")
        return None, None

    current_price = get_current_price(ib, stock)

    if current_price is None:
        log_cron_event("Fehler: Aktueller Basiswert-Kurs konnte nicht ermittelt werden.")
        return None, None

    chains = ib.reqSecDefOptParams(
        stock.symbol,
        "",
        stock.secType,
        stock.conId,
    )

    if not chains:
        log_cron_event("Fehler: Keine Optionsketten fuer das Symbol erhalten.")
        return None, None

    chain = next(
        (
            item
            for item in chains
            if item.exchange == PRIMARY_EXCHANGE
        ),
        chains[0],
    )

    today = datetime.datetime.now(BOT_TIMEZONE).date()

    valid_expirations = [
        expiry
        for expiry in chain.expirations
        if MIN_DAYS
        <= (datetime.datetime.strptime(expiry, "%Y%m%d").date() - today).days
        <= MAX_DAYS
    ]

    if not valid_expirations:
        log_cron_event("Abbruch: Kein Verfallstag innerhalb des DTE-Fensters gefunden.")
        return None, None

    target_expiry = sorted(valid_expirations)[0]
    ideal_strike = current_price * (1.0 - DISCOUNT)

    strikes_below_target = sorted(
        [strike for strike in chain.strikes if strike <= ideal_strike],
        reverse=True,
    )

    if not strikes_below_target:
        log_cron_event("Abbruch: Keine Strikes unterhalb des Ziel-Discounts verfuegbar.")
        return None, None

    for strike in strikes_below_target[:MAX_STRIKE_FALLBACKS]:
        option_contract = Option(
            SYMBOL,
            target_expiry,
            strike,
            "P",
            EXCHANGE,
            currency=CURRENCY,
        )

        qualified_option = ib.qualifyContracts(option_contract)

        if not qualified_option:
            log_cron_event(
                "Strike nicht qualifizierbar: "
                + str(strike)
                + " / "
                + format_expiry(target_expiry)
            )
            continue

        option_ticker = ib.reqMktData(option_contract, "", False, False)
        ib.sleep(1)

        bid = option_ticker.bid

        if bid and not math.isnan(bid) and bid >= MIN_VALID_BID_PRICE:
            log_cron_event(
                "Kontrakt gefunden: "
                + option_contract.localSymbol
                + " | Preis Basiswert: "
                + format(current_price, ".2f")
                + " | Ziel-Strike: "
                + format(ideal_strike, ".2f")
                + " | Gewaehlter Strike: "
                + str(strike)
                + " | Bid: "
                + format(bid, ".2f")
            )
            return option_contract, bid

    log_cron_event("Abbruch: Kein Strike erfuellt die Mindest-Bid-Kriterien.")
    return None, None


def place_short_put_with_tp(ib, contract, bid_price):
    limit_price = round(bid_price, 2)

    sell_order = LimitOrder("SELL", 1, limit_price)
    trade = ib.placeOrder(contract, sell_order)

    elapsed = 0

    while not trade.isDone() and elapsed < CLOSE_ORDER_WAIT_SECONDS:
        ib.sleep(1)
        elapsed += 1

    if not trade.isDone():
        ib.cancelOrder(sell_order)
        ib.sleep(1)

        log_cron_event(
            "Order nicht innerhalb von "
            + str(CLOSE_ORDER_WAIT_SECONDS)
            + " Sekunden ausgefuehrt. Storniert."
        )
        return False

    fill_price = trade.orderStatus.avgFillPrice

    if not fill_price or math.isnan(fill_price):
        fill_price = limit_price

    now_str = datetime.datetime.now(BOT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    formatted_expiry = format_expiry(contract.lastTradeDateOrContractMonth)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(
            "["
            + now_str
            + "] ORDER_PLACED "
            + contract.localSymbol
            + " Expiry: "
            + formatted_expiry
            + " Strike: "
            + str(contract.strike)
            + " Fill: "
            + format(fill_price, ".2f")
            + "\n"
        )

    log_cron_event(
        "Ausgefuehrt: Short Put "
        + contract.localSymbol
        + " zu "
        + format(fill_price, ".2f")
        + " USD"
    )

    tp_price = round(fill_price * TAKE_PROFIT_FRACTION, 2)

    if tp_price <= 0:
        log_cron_event("Fehler: Take-Profit-Preis ist ungueltig.")
        return False

    take_profit_order = LimitOrder("BUY", 1, tp_price)
    ib.placeOrder(contract, take_profit_o
