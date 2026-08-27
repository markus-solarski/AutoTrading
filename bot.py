import math
import os
import datetime
import sys
import random

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

os.environ['TZ'] = 'Europe/Berlin'
from ib_insync import IB, Stock, Option, LimitOrder

MAX_ORDERS_PER_MONTH = 10
MAX_CONCURRENT_TRADES = 3
LOG_FILE = "trades_log.txt"
SYMBOL = "EEM"
CURRENCY = "USD"
EXCHANGE = "SMART"
PRIMARY_EXCHANGE = "ARCA"

DISCOUNT = 0.093
MIN_DAYS = 30
MAX_DAYS = 41


def already_traded_this_month():
    if not os.path.exists(LOG_FILE):
        return False

    current_month_str = datetime.date.today().strftime("%Y-%m")
    trades_this_month_count = 0
    with open(LOG_FILE, "r") as f:
        for line in f:
            if line.startswith("[") and "Order platziert" in line:
                log_month_str = line[1:8]
                if log_month_str == current_month_str:
                    trades_this_month_count += 1

    if trades_this_month_count >= MAX_ORDERS_PER_MONTH:
        print("[SICHERHEITSHINWEIS] Monatslimit erreicht: " + str(trades_this_month_count) + "/" + str(MAX_ORDERS_PER_MONTH))
        return True

    print("[INFO] Bisherige Orders diesen Monat: " + str(trades_this_month_count) + " von maximal " + str(MAX_ORDERS_PER_MONTH))
    return False


def sync_open_orders(ib):
    ib.reqAllOpenOrders()
    ib.sleep(2)


def count_active_trades(ib, symbol):
    sync_open_orders(ib)

    open_trades = ib.openTrades()
    matching_trades = []
    for t in open_trades:
        if t.contract.symbol == symbol and t.orderStatus.status not in ('Filled', 'Cancelled', 'ApiCancelled'):
            matching_trades.append(t)
    open_orders_count = len(matching_trades)

    positions = ib.positions()
    open_positions_count = 0
    for p in positions:
        if p.contract.symbol == symbol and p.position != 0:
            open_positions_count += 1

    total_active = open_orders_count + open_positions_count

    print("[DEBUG] Gefundene offene Orders fuer " + symbol + ":")
    if matching_trades:
        for t in matching_trades:
            strike = getattr(t.contract, 'strike', '-')
            right = getattr(t.contract, 'right', '')
            print("        OrderId " + str(t.order.orderId) + " | clientId " + str(t.order.clientId) + " | Status: " + t.orderStatus.status + " | " + str(strike) + str(right))
    else:
        print("        (keine)")

    print("[INFO] Aktive Orders: " + str(open_orders_count) + " | Offene Positionen: " + str(open_positions_count) + " | Gesamt aktiv: " + str(total_active) + "/" + str(MAX_CONCURRENT_TRADES))
    return total_active


def log_trade(details):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write("[" + now_str + "] " + details + "\n")


def run_bot():
    ib = IB()
    try:
        print("=== SHORT PUT BOT: EMERGING MARKETS ETF (" + SYMBOL + ") ===")

        if already_traded_this_month():
            return

        client_id = random.randint(1000, 9999)
        connected = False
        for attempt in range(1, 4):
            try:
                ib.connect('127.0.0.1', 4002, clientId=client_id, timeout=20)
                connected = True
                break
            except Exception as conn_err:
                print("[WARNUNG] Verbindungsversuch " + str(attempt) + " mit clientId " + str(client_id) + " fehlgeschlagen: " + str(conn_err))
                client_id = random.randint(1000, 9999)
                ib.sleep(2)

        if not connected:
            print("[FEHLER] Verbindung zum IB Gateway konnte nach mehreren Versuchen nicht aufgebaut werden.")
            return

        print("[INFO] Erfolgreich mit IB Gateway verbunden (clientId=" + str(client_id) + ")")

        ib.reqMarketDataType(3)

        stock = Stock(SYMBOL, EXCHANGE, CURRENCY, primaryExchange=PRIMARY_EXCHANGE)
        ib.qualifyContracts(stock)

        active_count = count_active_trades(ib, SYMBOL)
        if active_count >= MAX_CONCURRENT_TRADES:
            print("[SICHERHEITSHINWEIS] Limit von " + str(MAX_CONCURRENT_TRADES) + " gleichzeitig laufenden Trades erreicht (" + str(active_count) + "/" + str(MAX_CONCURRENT_TRADES) + "). Keine neue Order.")
            return

        bars = ib.reqHistoricalData(
            stock, endDateTime='', durationStr='2 D',
            barSizeSetting='1 day', whatToShow='TRADES', useRTH=True
        )
        if not bars:
            print("[FEHLER] Keine Kursdaten empfangen.")
            return

        last_close = bars[-1].close
        calculated_target = last_close * (1 - DISCOUNT)

        today = datetime.date.today()
        min_exp = today + datetime.timedelta(days=MIN_DAYS)
        max_exp = today + datetime.timedelta(days=MAX_DAYS)

        chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
        if not chains:
            print("[FEHLER] Keine Optionsdaten gefunden.")
            return

        smart_chains = [c for c in chains if c.exchange == 'SMART']
        chain = smart_chains[0] if smart_chains else chains[0]

        expiry = None
        for exp_str in sorted(list(chain.expirations)):
            exp_date = datetime.datetime.strptime(exp_str, '%Y%m%d').date()
            if min_exp <= exp_date <= max_exp:
                expiry = exp_str
                break

        if not expiry:
            print("[FEHLER] Keine Option im Zeitfenster gefunden.")
            return

        valid_strikes = [float(s) for s in chain.strikes if float(s) <= calculated_target]
        if not valid_strikes:
            print("[FEHLER] Kein passender Strike unterhalb des Ziel-Preises vorhanden.")
            return
        target_strike = max(valid_strikes)

        put_option = Option(SYMBOL, expiry, target_strike, 'P', 'SMART', currency=CURRENCY)
        qualified = ib.qualifyContracts(put_option)
        if not qualified:
            print("[FEHLER] Kontrakt konnte nicht qualifiziert werden.")
            return

        print("[INFO] Frage verzoegerte Marktdaten ab...")
        ticker = ib.reqMktData(put_option, '', False, False)

        for _ in range(10):
            ib.sleep(1)
            if ticker.bid > 0 or ticker.ask > 0:
                break

        bid = ticker.bid
        ask = ticker.ask

        is_fallback_used = False
        if bid > 0 and ask > 0:
            target_price = round((bid + ask) / 2, 2)
        elif ticker.close > 0:
            target_price = ticker.close
            is_fallback_used = True
        elif ticker.last > 0:
            target_price = ticker.last
            is_fallback_used = True
        else:
            print("[FEHLER] Keine validen Preisdaten empfangen. Order-Platzierung abgebrochen.")
            return

        print("")
        print("--- ZUSAMMENFASSUNG ---")
        print("ETF: iShares MSCI Emerging Markets (" + SYMBOL + ") | Letzter Kurs: " + format(last_close, '.2f') + " USD")
        print("Berechneter Zielpreis: " + format(calculated_target, '.2f') + " USD")
        print("Gewaehlter Strike: " + str(target_strike) + " USD | Verfallsdatum: " + expiry)
        print("Option: " + put_option.localSymbol + " (" + put_option.exchange + ")")
        print("Aktive Trades vor dieser Order: " + str(active_count) + "/" + str(MAX_CONCURRENT_TRADES))

        if bid > 0 and ask > 0:
            print("Verzoegerte Marktdaten: Bid " + format(bid, '.2f') + " USD | Ask " + format(ask, '.2f') + " USD")
        else:
            print("Warnung: Keine aktiven Bid/Ask-Kurse. Historischer Close/Last als Fallback genutzt!")

        print("-> Berechnete Ziel-Praemie (Limit): " + format(target_price, '.2f') + " USD")

        if is_fallback_used:
            print("ACHTUNG: Der Preis basiert auf historischen Daten. Limit manuell pruefen!")

        auto_confirm = os.getenv("AUTO_CONFIRM", "false").lower() == "true"

        if sys.stdin.isatty() and not auto_confirm:
            user_input = input("\nSoll der Short Put zu diesem Mid-Preis platziert werden? (j/n): ").strip()
        else:
            user_input = "j" if auto_confirm else "n"
            print("[AUTO] Keine interaktive Eingabe moeglich. Automatische Antwort: '" + user_input + "'")

        if user_input.lower() == 'j':
            order = LimitOrder('SELL', 1, target_price)
            trade = ib.placeOrder(put_option, order)

            ib.sleep(2)
            print("[OK] Status: " + trade.orderStatus.status)
            log_trade("Order platziert: " + SYMBOL + " Strike " + str(target_strike) + " Expiry " + expiry + " Limit " + format(target_price, '.2f') + " USD | Status: " + trade.orderStatus.status)
        else:
            print("[ABBRUCH] Es wurde keine Order platziert.")

    except Exception as e:
        print("[CRITICAL ERROR] Ein unerwarteter Fehler ist aufgetreten: " + str(e))
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("Verbindung getrennt.")


if __name__ == "__main__":
    run_bot()
