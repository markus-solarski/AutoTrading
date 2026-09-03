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

# Hinweis: IB Gateway liefert ueber reqExecutions() ausschliesslich Ausfuehrungen
# des aktuellen Handelstages - eine 3-Monats-Rueckschau ist darueber technisch
# nicht moeglich (feste API-Beschraenkung, keine Frage der Konfiguration).
# Stattdessen wird ib.positions() genutzt: das zeigt den tatsaechlichen, aktuellen
# Kontostand direkt vom Broker, unabhaengig davon, wann eine Position eroeffnet
# wurde - deckt damit jeden beliebigen Zeitraum ab, auch mehrere Monate zurueck.


def is_weekend():
    """Samstag = 5, Sonntag = 6 (Montag = 0). Keine Orders am Wochenende."""
    return datetime.date.today().weekday() >= 5


def log_cron_event(details):
    """
    Schreibt eine Zeile mit Zeitstempel in cron.log. Wird sowohl beim reinen
    Skriptstart (log_cron_run) als auch bei bestimmten Abbruch-Ereignissen
    (z.B. zu niedriger Bid-Preis, Wochenende) genutzt, damit im Cron-Log
    nachvollziehbar ist, WARUM ein Lauf keine Order platziert hat.
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CRON_LOG_FILE, "a") as f:
        f.write("[" + now_str + "] " + details + "\n")


def log_cron_run():
    """
    Schreibt bei JEDEM Skriptstart sofort einen Eintrag in cron.log - inklusive
    Kennzeichnung, ob es sich um einen automatischen Cron-Lauf oder einen
    manuellen Start im Terminal handelt. Erkennung ueber sys.stdin.isatty():
    Cron-Jobs haben kein TTY, ein manueller Start im Terminal schon.
    """
    lauf_typ = "manueller Lauf" if sys.stdin.isatty() else "Cron-Lauf"
    log_cron_event("Bot-Lauf gestartet (" + lauf_typ + ")")


def log_ib_error(reqId, errorCode, errorString, contract):
    """
    Protokolliert die vollen IB-Fehlermeldungen (z.B. 'No security definition has
    been found'), damit man bei Abbruechen genau sieht, WELCHER Kontrakt / Request
    das Problem verursacht hat, statt nur den generischen Skript-Abbruch zu sehen.
    """
    if errorCode in (200, 321, 322, 354, 10225):
        print("[IB-FEHLER] Code " + str(errorCode) + ": " + errorString
              + (" | Kontrakt: " + str(contract) if contract else ""))


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

    def order_richtung_label(order):
        """SELL Put = Short Put (Praemie einnehmen), BUY Put = Long Put (Kauf/Rueckkauf)."""
        right = getattr(order.contract, 'right', '')
        if order.order.action == 'SELL':
            return "Short Put" if right == 'P' else "Short Call"
        elif order.order.action == 'BUY':
            return "Long Put" if right == 'P' else "Long Call"
        return "Unbekannt"

    def position_richtung_label(position):
        """Negative Positionsgroesse = Short Put (verkauft), positive = Long Put (gekauft)."""
        right = getattr(position.contract, 'right', '')
        if position.position < 0:
            return "Short Put" if right == 'P' else "Short Call"
        elif position.position > 0:
            return "Long Put" if right == 'P' else "Long Call"
        return "Unbekannt"

    print("[DEBUG] Orders aufgegeben (Status Submitted) fuer " + symbol + ":")
    if orders_aufgegeben_list:
        for t in orders_aufgegeben_list:
            strike = getattr(t.contract, 'strike', '-')
            right = getattr(t.contract, 'right', '')
            richtung = order_richtung_label(t)
            print("        OrderId " + str(t.order.orderId) + " | clientId " + str(t.order.clientId) + " | Status: " + t.orderStatus.status + " | " + str(strike) + str(right) + " | " + richtung)
    else:
        print("        (keine)")

    print("[DEBUG] Aktive Trades (offene Positionen) fuer " + symbol + ":")
    if aktive_trades_list:
        for p in aktive_trades_list:
            strike = getattr(p.contract, 'strike', '-')
            right = getattr(p.contract, 'right', '')
            richtung = position_richtung_label(p)
            print("        Position: " + str(p.position) + " | " + str(strike) + str(right) + " | AvgCost: " + format(p.avgCost, '.2f') + " | " + richtung)
    else:
        print("        (keine)")

    print("[INFO] Orders aufgegeben: " + str(orders_aufgegeben_count) + " | Aktive Trades: " + str(aktive_trades_count))
    print("[INFO] Referenz aus lokalem Log: " + str(recent_log_fills) + " Order(s) in den letzten 90 Tagen platziert (nur informativ, kein Limit-Check).")
    return total_active


def log_trade(details):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write("[" + now_str + "] " + details + "\n")


def has_open_closing_order(ib, contract):
    """
    Prueft, ob fuer diesen Options-Kontrakt bereits eine offene BUY-Order
    (Rueckkauf / Take-Profit) existiert, um Doppel-Platzierungen zu vermeiden.
    """
    sync_open_orders(ib)
    for t in ib.openTrades():
        if (t.contract.conId == contract.conId
                and t.order.action == 'BUY'
                and t.orderStatus.status in ('PreSubmitted', 'Submitted', 'PendingSubmit', 'ApiPending')):
            return True
    return False


def place_closing_order(ib, contract, premium_per_share, quantity):
    """
    Platziert die Long-Put-Rueckkauf-Order (BUY, GoodTillCancel) zum
    halben Praemienpreis des urspruenglichen Short Puts.
    """
    take_profit_price = round(premium_per_share * TAKE_PROFIT_FRACTION, 2)

    closing_order = LimitOrder('BUY', quantity, take_profit_price)
    closing_order.tif = 'GTC'

    closing_trade = ib.placeOrder(contract, closing_order)
    ib.sleep(2)

    print("[OK] Take-Profit-Rueckkauf-Order (GTC) platziert: " + contract.localSymbol
          + " | Limit " + format(take_profit_price, '.2f') + " USD"
          + " | Status: " + closing_trade.orderStatus.status)

    log_trade("Closing Order (Take-Profit GTC) platziert: " + contract.localSymbol
               + " Limit " + format(take_profit_price, '.2f') + " USD"
               + " (50% von Praemie " + format(premium_per_share, '.2f') + " USD)"
               + " | Status: " + closing_trade.orderStatus.status)

    return closing_trade


def ensure_closing_orders_for_open_positions(ib, symbol):
    """
    Laeuft bei jedem Bot-Start: sucht bestehende offene Short-Put-Positionen
    fuer 'symbol', fuer die noch KEINE Rueckkauf-Order (Take-Profit, GTC)
    existiert, und legt diese nach - basierend auf dem tatsaechlichen
    durchschnittlichen Ausfuehrungspreis (avgCost) der Position.
    Das fängt auch Faelle ab, in denen die Short-Put-Order erst NACH dem
    vorherigen Skript-Lauf gefuellt wurde.
    """
    positions = ib.positions()
    for p in positions:
        if p.contract.symbol != symbol:
            continue
        if p.position >= 0:
            continue  # nur Short-Positionen (negative Stueckzahl) betreffen uns

        contract = p.contract
        ib.qualifyContracts(contract)

        if has_open_closing_order(ib, contract):
            print("[INFO] Rueckkauf-Order fuer " + contract.localSymbol + " existiert bereits - keine neue Order.")
            continue

        multiplier = float(getattr(contract, 'multiplier', '100') or '100')
        premium_per_share = abs(p.avgCost) / multiplier
        quantity = abs(p.position)

        print("[INFO] Offene Short-Put-Position ohne Rueckkauf-Order gefunden: " + contract.localSymbol
              + " | Praemie (avgCost-basiert): " + format(premium_per_share, '.2f') + " USD")

        place_closing_order(ib, contract, premium_per_share, quantity)


def find_valid_put_contract(ib, symbol, currency, chain, calculated_target, min_exp, max_exp):
    """
    reqSecDefOptParams liefert eine AGGREGIERTE Liste aller Strikes/Expiries ueber
    alle Boersenplaetze - nicht jede Kombination ist tatsaechlich als Kontrakt
    gelistet. Statt blind die erste Kombination zu qualifizieren (was bei einer
    ungueltigen Kombination zu "No security definition has been found" fuehrt),
    wird hier mit reqContractDetails() ueber mehrere Strikes/Expiries geprueft,
    bis ein tatsaechlich existierender Kontrakt gefunden wird.
    """
    candidate_expiries = []
    for exp_str in sorted(chain.expirations):
        exp_date = datetime.datetime.strptime(exp_str, '%Y%m%d').date()
        if min_exp <= exp_date <= max_exp:
            candidate_expiries.append(exp_str)

    if not candidate_expiries:
        print("[FEHLER] Keine Option im Zeitfenster gefunden.")
        return None, None, None

    candidate_strikes = sorted(
        [float(s) for s in chain.strikes if float(s) <= calculated_target],
        reverse=True
    )[:MAX_STRIKE_FALLBACKS]

    if not candidate_strikes:
        print("[FEHLER] Kein passender Strike unterhalb des Ziel-Preises vorhanden.")
        return None, None, None

    for expiry in candidate_expiries:
        for strike in candidate_strikes:
            probe = Option(symbol, expiry, strike, 'P', 'SMART', currency=currency)
            details = ib.reqContractDetails(probe)
            if details:
                qualified_contract = details[0].contract
                print("[INFO] Gueltiger Kontrakt gefunden: Strike " + str(strike) + " | Expiry " + expiry)
                return qualified_contract, expiry, strike
            else:
                print("[DEBUG] Kombination ungueltig (kein Kontrakt bei IB gelistet): Strike "
                      + str(strike) + " | Expiry " + expiry)

    print("[FEHLER] Keine gueltige Strike/Expiry-Kombination im Zeitfenster gefunden.")
    return None, None, None


def run_bot():
    log_cron_run()

    if is_weekend():
        print("[INFO] Heute ist Wochenende - keine Orders werden aufgegeben.")
        log_cron_event("Kein Bot-Lauf: Wochenende (Samstag/Sonntag)")
        return

    ib = IB()
    ib.errorEvent += log_ib_error
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

        # Zuerst pruefen, ob bereits gefuellte Short Puts noch eine
        # Rueckkauf-Order (Take-Profit, GTC) brauchen - unabhaengig vom
        # Monats-/Concurrent-Limit fuer NEUE Orders.
        ensure_closing_orders_for_open_positions(ib, SYMBOL)

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

        put_option, expiry, target_strike = find_valid_put_contract(
            ib, SYMBOL, CURRENCY, chain, calculated_target, min_exp, max_exp
        )
        if put_option is None:
            return

        print("[INFO] Frage verzoegerte Marktdaten ab...")
        ticker = ib.reqMktData(put_option, '', False, False)

        for _ in range(10):
            ib.sleep(1)
            if ticker.bid > 0 or ticker.ask > 0:
                break

        bid = ticker.bid
        ask = ticker.ask

        if 0 < bid < MIN_VALID_BID_PRICE:
            print("[SICHERHEITSHINWEIS] Bid-Preis von " + format(bid, '.2f') + " USD ist zu niedrig (< " + format(MIN_VALID_BID_PRICE, '.2f') + " USD) - Order wird nicht platziert.")
            log_cron_event("Order NICHT platziert: " + SYMBOL + " Strike " + str(target_strike) + " Expiry " + expiry
                            + " - Bid-Preis zu niedrig: " + format(bid, '.2f') + " USD (< " + format(MIN_VALID_BID_PRICE, '.2f') + " USD)")
            return

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

        auto_confirm = os.getenv("AUTO_CONFIRM", "true").lower() == "true"

        if sys.stdin.isatty():
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

            # Kurz warten, ob die Short-Put-Order noch im selben Lauf gefuellt wird.
            # Falls ja: sofort die Take-Profit-Rueckkauf-Order (50% Praemie, GTC) platzieren.
            # Falls nein: das erledigt ensure_closing_orders_for_open_positions() beim
            # naechsten Bot-Start automatisch, sobald der Fill vorliegt.
            waited = 0
            while waited < CLOSE_ORDER_WAIT_SECONDS and trade.orderStatus.status != 'Filled':
                ib.sleep(2)
                waited += 2

            if trade.orderStatus.status == 'Filled':
                filled_qty = abs(trade.orderStatus.filled) or 1
                fill_price = trade.orderStatus.avgFillPrice
                print("[INFO] Short Put wurde im selben Lauf gefuellt @ " + format(fill_price, '.2f') + " USD.")
                place_closing_order(ib, put_option, fill_price, filled_qty)
            else:
                print("[INFO] Short Put noch nicht gefuellt (Status: " + trade.orderStatus.status + "). "
                      "Die Rueckkauf-Order wird beim naechsten Bot-Lauf automatisch nachgetragen, sobald ein Fill vorliegt.")
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
