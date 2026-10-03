import os
import sys
import time
import datetime
import random

os.environ['TZ'] = 'Europe/Berlin'
try:
    time.tzset()
except AttributeError:
    pass  # Windows

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

TAKE_PROFIT_FRACTION = 0.5
CLOSE_ORDER_WAIT_SECONDS = 20
MAX_STRIKE_FALLBACKS = 5
MIN_CLOSE_PRICE = 0.01

ORDER_LOG_MARKER = "Order platziert"


def log_ib_error(reqId, errorCode, errorString, contract):
    if errorCode in (200, 321, 322, 354, 10225):
        print("[IB-FEHLER] Code " + str(errorCode) + ": " + errorString
              + (" | Kontrakt: " + str(contract) if contract else ""))


def already_traded_this_month():
    if not os.path.exists(LOG_FILE):
        return False

    current_month_str = datetime.date.today().strftime("%Y-%m")
    count = 0
    with open(LOG_FILE, "r") as f:
        for line in f:
            if line.startswith("[") and ORDER_LOG_MARKER in line and not line.startswith("[", 1):
                if line[1:8] == current_month_str and "Closing Order" not in line:
                    count += 1

    if count >= MAX_ORDERS_PER_MONTH:
        print("[SICHERHEITSHINWEIS] Monatslimit erreicht: " + str(count) + "/" + str(MAX_ORDERS_PER_MONTH))
        return True

    print("[INFO] Bisherige Orders diesen Monat: " + str(count) + " von maximal " + str(MAX_ORDERS_PER_MONTH))
    return False


def count_recent_log_fills(symbol, days_back=90):
    if not os.path.exists(LOG_FILE):
        return 0

    cutoff_date = datetime.date.today() - datetime.timedelta(days=days_back)
    count = 0
    with open(LOG_FILE, "r") as f:
        for line in f:
            if line.startswith("[") and ORDER_LOG_MARKER in line and "Closing Order" not in line and symbol in line:
                try:
                    log_date = datetime.datetime.strptime(line[1:11], "%Y-%m-%d").date()
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
    Zaehlt NUR eroeffnende Short-Put-Trades:
    - offene SELL-Orders (Submitted/PreSubmitted) = noch nicht gefuellt
    - Short-Positionen (position < 0) = gefuellt, noch offen
    Take-Profit-BUY-Orders werden bewusst NICHT mitgezaehlt (sonst Doppelzaehlung).
    """
    sync_open_orders(ib)

    orders_list = [
        t for t in ib.openTrades()
        if t.contract.symbol == symbol
        and t.order.action == 'SELL'
        and t.orderStatus.status in ('Submitted', 'PreSubmitted')
    ]
    positions_list = [
        p for p in ib.positions()
        if p.contract.symbol == symbol
        and getattr(p.contract, 'right', '') == 'P'
        and p.position < 0
    ]

    orders_count = len(orders_list)
    positions_count = len(positions_list)
    total_active = orders_count + positions_count

    recent_log_fills = count_recent_log_fills(symbol, days_back=90)

    print("[DEBUG] Offene SELL-Orders (Short Put, noch nicht gefuellt) fuer " + symbol + ":")
    if orders_list:
        for t in orders_list:
            print("        OrderId " + str(t.order.orderId) + " | clientId " + str(t.order.clientId)
                  + " | Status: " + t.orderStatus.status + " | "
                  + str(getattr(t.contract, 'strike', '-')) + str(getattr(t.contract, 'right', '')))
    else:
        print("        (keine)")

    print("[DEBUG] Offene Short-Put-Positionen fuer " + symbol + ":")
    if positions_list:
        for p in positions_list:
            print("        Position: " + str(p.position) + " | "
                  + str(getattr(p.contract, 'strike', '-')) + str(getattr(p.contract, 'right', ''))
                  + " | AvgCost: " + format(p.avgCost, '.2f'))
    else:
        print("        (keine)")

    print("[INFO] Offene SELL-Orders: " + str(orders_count) + " | Short-Positionen: " + str(positions_count)
          + " | Summe: " + str(total_active))
    print("[INFO] Referenz aus lokalem Log: " + str(recent_log_fills)
          + " Order(s) in den letzten 90 Tagen platziert (nur informativ).")
    return total_active


def log_trade(details):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write("[" + now_str + "] " + details + "\n")


def has_open_closing_order(ib, contract):
    sync_open_orders(ib)
    for t in ib.openTrades():
        if (t.contract.conId == contract.conId
                and t.order.action == 'BUY'
                and t.orderStatus.status in ('PreSubmitted', 'Submitted', 'PendingSubmit', 'ApiPending')):
            return True
    return False


def place_closing_order(ib, contract, premium_per_share, quantity):
    take_profit_price = max(MIN_CLOSE_PRICE, round(premium_per_share * TAKE_PROFIT_FRACTION, 2))

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
    for p in ib.positions():
        if p.contract.symbol != symbol:
            continue
        if getattr(p.contract, 'right', '') != 'P':
            continue
        if p.position >= 0:
            continue

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
            probe = Option(symbol, expiry, strike, 'P', 'SMART',
                           multiplier='100', currency=currency, tradingClass=symbol)
            details = ib.reqContractDetails(probe)
            if details:
                qualified_contract = details[0].contract
                print("[INFO] Gueltiger Kontrakt gefunden: Strike " + str(strike) + " | Expiry " + expiry)
                return qualified_contract, expiry, strike
            print("[DEBUG] Kombination ungueltig (kein Kontrakt bei IB gelistet): Strike "
                  + str(strike) + " | Expiry " + expiry)

    print("[FEHLER] Keine gueltige Strike/Expiry-Kombination im Zeitfenster gefunden.")
    return None, None, None


def run_bot():
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
                print("[WARNUNG] Verbindungsversuch " + str(attempt) + " mit clientId " + str(client_id)
                      + " fehlgeschlagen: " + str(conn_err))
                client_id = random.randint(1000, 9999)
                ib.sleep(2)

        if not connected:
            print("[FEHLER] Verbindung zum IB Gateway konnte nach mehreren Versuchen nicht aufgebaut werden.")
            return

        print("[INFO] Erfolgreich mit IB Gateway verbunden (clientId=" + str(client_id) + ")")

        ib.reqMarketDataType(3)

        stock = Stock(SYMBOL, EXCHANGE, CURRENCY, primaryExchange=PRIMARY_EXCHANGE)
        ib.qualifyContracts(stock)

        ensure_closing_orders_for_open_positions(ib, SYMBOL)

        active_count = count_active_trades(ib, SYMBOL)
        if active_count >= MAX_CONCURRENT_TRADES:
            print("[SICHERHEITSHINWEIS] Limit von " + str(MAX_CONCURRENT_TRADES)
                  + " gleichzeitig laufenden Trades erreicht (" + str(active_count) + "/"
                  + str(MAX_CONCURRENT_TRADES) + "). Keine neue Order.")
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
        ib.cancelMktData(put_option)

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

        interactive = sys.stdin.isatty() and os.getenv("AUTO_CONFIRM", "false").lower() != "true"

        if interactive:
            user_input = input("\nSoll der Short Put zu diesem Mid-Preis platziert werden? (j/n): ").strip()
        else:
            if is_fallback_used:
                print("[AUTO] Preis nur aus Close/Last-Fallback - aus Sicherheitsgruenden keine automatische Order.")
                user_input = "n"
            else:
                print("[AUTO] Cron/Non-TTY-Lauf: Order wird ohne Rueckfrage platziert.")
                user_input = "j"

        if user_input.lower() == 'j':
            order = LimitOrder('SELL', 1, target_price)
            trade = ib.placeOrder(put_option, order)

            ib.sleep(2)
            print("[OK] Status: " + trade.orderStatus.status)
            log_trade(ORDER_LOG_MARKER + ": " + SYMBOL + " Strike " + str(target_strike) + " Expiry " + expiry
                      + " Limit " + format(target_price, '.2f') + " USD | Status: " + trade.orderStatus.status)

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
