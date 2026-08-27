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

# Hinweis: IB Gateway liefert ueber reqExecutions() ausschliesslich Ausfuehrungen
# des aktuellen Handelstages - eine 3-Monats-Rueckschau ist darueber technisch
# nicht moeglich (feste API-Beschraenkung, keine Frage der Konfiguration).
# Stattdessen wird ib.positions() genutzt: das zeigt den tatsaechlichen, aktuellen
# Kontostand direkt vom Broker, unabhaengig davon, wann eine Position eroeffnet
# wurde - deckt damit jeden beliebigen Zeitraum ab, auch mehrere Monate zurueck.


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


def count_recent_log_fills(symbol, days_back=90):
    """
    Zusaetzliche Referenz-Zaehlung ausschliesslich aus dem lokalen Bot-Log
    (trades_log.txt), da IBKR selbst keine 90-Tage-Execution-Abfrage erlaubt.
    Zaehlt alle Log-Eintraege mit "Order platziert" fuer das Symbol, deren
    Datum innerhalb der letzten 'days_back' Tage liegt. Dient nur zu
    Informationszwecken, nicht zur Limit-Pruefung.
    """
    if not os.path.exists(LOG_FILE):
        return 0

    cutoff_date = datetime.date.today() - datetime.timedelta(days=days_back)
    count = 0
    with open(LOG_FILE, "r") as f:
        for line in f:
            if line.startswith("[") and "Order platziert" in line and symbol in line:
                try:
                    log_date_str = line[1:11]  # "YYYY-MM-DD"
                    log_date = datetime.datetime.strptime(log_date_str, "%Y-%m-%d").date()
                    if log_date >= cutoff_date:
                        count += 1
                except ValueError:
                    continue
    return count


def sync_open_orders(ib):
    ib.reqAllOpenOrders()
    ib.sleep(2)


def count_active_trades(ib, symbol):
    """
    Definition:
    - "Orders aufgegeben": Orders mit Status 'Submitted' (noch nicht ausgefuehrt).
    - "Aktive Trades": tatsaechlich bestehende, offene Positionen (Status Filled,
      noch nicht geschlossen/abgelaufen) - ermittelt ueber ib.positions(), da dies
      unabhaengig vom Alter der Position immer den aktuellen, korrekten Stand zeigt.
    """
    sync_open_orders(ib)

    # --- Orders aufgegeben (Status = Submitted) ---
    open_trades = ib.openTrades()
    orders_aufgegeben_list = []
    for t in open_trades:
        if t.contract.symbol == symbol and t.orderStatus.status == 'Submitted':
            orders_aufgegeben_list.append(t)
    orders_aufgegeben_count = len(orders_aufgegeben_list)

    # --- Aktive Trades (tatsaechlich offene Positionen, unabhaengig vom Alter) ---
    positions = ib.positions()
    aktive_trades_list = []
    for p in positions:
        if p.contract.symbol == symbol and p.position != 0:
            aktive_trades_list.append(p)
    aktive_trades_count = len(aktive_trades_list)

    total_active = orders_aufgegeben_count + aktive_trades_count

    # Referenzwert: wie viele Orders wurden laut lokalem Log in den letzten 90 Tagen platziert
    recent_log_fills = count_recent_log_fills(symbol, days_back=90)

    print("[DEBUG] Orders aufgegeben (Status Submitted) fuer " + symbol + ":")
    if orders_aufgegeben_list:
        for t in orders_aufgegeben_list:
            strike = getattr(t.contract, 'strike', '-')
            right = getattr(t.contract, 'right', '')
            print("        OrderId " + str(t.order.orderId) + " | clientId " + str(t.order.clientId) + " | Status: " + t.orderStatus.status + " | " + str(strike) + str(right))
    else:
        print("        (keine)")

    print("[DEBUG] Aktive Trades (offene Positionen) fuer " + symbol + ":")
    if aktive_trades_list:
        for p in aktive_trades_list:
            strike = getattr(p.contract, 'strike', '-')
            right = getattr(p.contract, 'right', '')
            print("        Position: " + str(p.position) + " | " + str(strike) + str(right) + " | AvgCost: " + format(p.avgCost, '.2f'))
    else:
        print("        (keine)")

    print("[INFO] Orders aufgegeben: " + str(orders_aufgegeben_count) + " | Aktive Trades: " + str(aktive_trades_count) + " | Gesamt aktiv: " + str(total_active) + "/" + str(MAX_CONCURRENT_TRADES))
    print("[INFO] Referenz aus lokalem Log: " + str(recent_log_fills) + " Order(s) in den letzten 90 Tagen platziert (nur informativ, kein Limit-Check).")
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
