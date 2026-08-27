import math
import os
import datetime
import sys
import random
# WORKAROUND: Zeitzonen-Absicherung
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
os.environ['TZ'] = 'Europe/Berlin'
from ib_insync import IB, Stock, Option, LimitOrder

# --- KONFIGURATION & SICHERHEIT ---
MAX_ORDERS_PER_MONTH = 10     # Max. 10 neue Orders pro Kalendermonat
MAX_CONCURRENT_TRADES = 3     # Max. 3 Trades gleichzeitig aktiv (offene Orders + Positionen)
LOG_FILE = "trades_log.txt"
SYMBOL = "EEM"
CURRENCY = "USD"
EXCHANGE = "SMART"
PRIMARY_EXCHANGE = "ARCA"

# Strategie-Parameter
DISCOUNT = 0.093              # 9,3 % Abstand zum aktuellen Kurs
MIN_DAYS = 30                 # Laufzeit 30 bis 41 Tage
MAX_DAYS = 41


def already_traded_this_month():
    """
    Prueft, ob im aktuellen Kalendermonat bereits das Limit an neuen Orders
    (MAX_ORDERS_PER_MONTH) erreicht wurde. Zaehlt alle Log-Eintraege mit
    "Order platziert", deren Datum im selben Jahr/Monat wie heute liegt.
    """
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
        print(f"[SICHERHEITSHINWEIS] Das Monatslimit von {MAX_ORDERS_PER_MONTH} Orders wurde erreicht ({trades_this_month_count}/{MAX_ORDERS_PER_MONTH}).")
        return True

    print(f"[INFO] Bisherige Orders diesen Monat: {trades_this_month_count} von maximal {MAX_ORDERS_PER_MONTH}.")
    return False


def sync_open_orders(ib):
    """
    Erzwingt einen vollstaendigen Sync ALLER offenen Orders ueber alle
    Client-IDs hinweg. Notwendig, weil der Bot bei jedem Lauf mit einer
    zufaelligen clientId verbindet - ohne diesen expliziten Call sieht
    ib.openTrades() unter Umstaenden nur die Orders der aktuellen Session
    und nicht die aus frueheren Laeufen mit anderer clientId.

    Wichtig: In IB Gateway muss unter Configure -> API -> Settings die
    Option "Download open orders on connection" aktiviert sein, damit
    dieser Sync zuverlaessig alle Orders liefert.
    """
    ib.reqAllOpenOrders()
    ib.sleep(2)  # Zeit fuer eingehende openOrder/orderStatus-Callbacks


def count_active_trades(ib, symbol):
    """
    Zaehlt alle aktuell 'laufenden' Trades fuer das gegebene Symbol:
    - offene/unausgefuehrte Orders (noch nicht gefuellt/storniert)
    - bestehende Positionen (bereits ausgefuehrte, noch offene Trades)
    """
    sync_open_orders(ib)

    open_trades = ib.openTrades()
    matching_trades = [
        t for t in open_trades
        if t.contract.symbol == symbol and t.orderStatus.status not in ('Filled', 'Cancelled', 'ApiCancelled')
    ]
    open_orders_count = len(matching_trades)

    positions = ib.positions()
    open_positions_count = sum(
        1 for p in positions
        if p.contract.symbol == symbol and p.position != 0
    )

    total_active = open_orders_count + open_positions_count

    # Diagnose-Ausgabe: zeigt genau, welche Orders erkannt wurden
    print(f"[DEBUG] Gefundene offene Orders fuer {symbol}:")
    if matching_trades:
        for t in matching_trades:
            print(f"        OrderId {t.order.orderId} | clientId {t.order.clientId} | Status: {t.orderStatus.status} | Strike/Right: {getattr(t.contract, 'strike', '-')}{getattr(t.contract, 'right', '')}")
    else:
        print("        (keine)")

    print(f"[INFO] Aktive Orders: {open_orders_count} | Offene Positionen: {open_positions_count} | Gesamt aktiv: {total_active}/{MAX_CONCURRENT_TRADES}")
    return total_active


def log_trade(details):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{now_str}] {details}\n")


def run_bot():
    ib = IB()
    try:
        print(f"=== SHORT PUT BOT: EMERGING MARKETS ETF ({SYMBOL}) ===")

        # 1. Sicherheits-Check: Monats-Sperre
        if already_traded_this_month():
            return

        # 2. Verbindung herstellen (Standard TWS: 7496/7497, Gateway: 4001/4002)
        # Zufaellige clientId, damit blockierte/haengende alte Sessions eine
        # neue Verbindung nicht mehr verhindern koennen.
        client_id = random.randint(1000, 9999)
        connected = False
        for attempt in range(1, 4):
            try:
                ib.connect('127.0.0.1', 4002, clientId=client_id, timeout=20)
                connected = True
                break
            except Exception as conn_err:
                print(f"[WARNUNG] Verbindungsversuch {attempt} mit clientId {client_id} fehlgeschlagen: {conn_err}")
                client_id = random.randint(1000, 9999)
                ib.sleep(2)

        if not connected:
            print("[FEHLER] Verbindung zum IB Gateway konnte nach mehreren Versuchen nicht aufgebaut werden.")
            return

        print(f"[INFO] Erfolgreich mit IB Gateway verbunden (clientId={client_id}).")

        # Verzögerte Marktdaten explizit anfordern
        ib.reqMarketDataType(3)

        # 3. Stock qualifizieren
        stock = Stock(SYMBOL, EXCHANGE, CURRENCY, primaryExchange=PRIMARY_EXCHANGE)
        ib.qualifyContracts(stock)

        # 3b. Gleichzeitigkeits-Check: max. 3 Trades parallel aktiv
        active_count = count_active_trades(ib, SYMBOL)
        if active_count >= MAX_CONCURRENT_TRADES:
            print(f"[SICHERHEITSHINWEIS] Limit von {MAX_CONCURRENT_TRADES} gleichzeitig laufenden Trades erreicht ({active_count}/{MAX_CONCURRENT_TRADES}). Keine neue Order.")
            return

        # 4. Schlusskurs & Zielpreis (-9,6%) berechnen
        bars = ib.reqHistoricalData(
            stock, endDateTime='', durationStr='2 D',
            barSizeSetting='1 day', whatToShow='TRADES', useRTH=True
        )
        if not bars:
            print("[FEHLER] Keine Kursdaten empfangen.")
            return

        last_close = bars[-1].close
        calculated_target = last_close * (1 - DISCOUNT)

        # 5. Laufzeit 25-38 Tage finden
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
            print(f"[FEHLER] Keine Option im Zeitfenster ({MIN_DAYS}-{MAX_DAYS} Tage) gefunden.")
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

        # 6. Marktdaten abfragen & präzise Warten auf Ticker
        print("[INFO] Frage verzögerte Marktdaten ab...")
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
            print("[FEHLER] Keine validen Preisdaten (Bid/Ask/Close) empfangen. Order-Platzierung abgebrochen.")
            return

        print("\n--- ZUSAMMENFASSUNG ---")
        print(f"ETF: iShares MSCI Emerging Markets ({SYMBOL}) | Letzter Kurs: {last_close:.2f} USD")
        print(f"Berechneter Zielpreis (-9.6%): {calculated_target:.2f} USD")
        print(f"Gewählter Strike: {target_strike} USD | Verfallsdatum: {expiry}")
        print(f"Option: {put_option.localSymbol} ({put_option.exchange})")
        print(f"Aktive Trades vor dieser Order: {active_count}/{MAX_CONCURRENT_TRADES}")

        if bid > 0 and ask > 0:
            print(f"Verzögerte Marktdaten: Bid {bid:.2f} USD | Ask {ask:.2f} USD")
        else:
            print("⚠️ Warnung: Keine aktiven Bid/Ask-Kurse. Historischer Close/Last als Fallback genutzt!")

        print(f"-> Berechnete Ziel-Prämie (Limit): {target_price:.2f} USD")

        if is_fallback_used:
            print("⚠️ ACHTUNG: Der Preis basiert auf historischen Daten. Limit manuell prüfen!")

        # 8. Bestätigung abfragen
        auto_confirm = os.getenv("AUTO_CONFIRM", "false").lower() == "true"

        if sys.stdin.isatty() and not auto_confirm:
            user_input = input("\nSoll der Short Put zu diesem Mid-Preis platziert werden? (j/n): ").strip()
        else:
            user_input = "j" if auto_confirm else "n"
            print(f"\n[AUTO] Keine interaktive Eingabe möglich. Automatische Antwort: '{user_input}'")

        if user_input.lower() == 'j':
            order = LimitOrder('SELL', 1, target_price)
            trade = ib.placeOrder(put_option, order)

            ib.sleep(2)
            print(f"\n[OK] Status: {trade.orderStatus.status}")
            log_trade(f"Order platziert: {SYMBOL} Strike {target_strike} Expiry {expiry} Limit {target_price:.2f} USD | Status: {trade.orderStatus.status}")
        else:
            print("\n[ABBRUCH] Es wurde keine Order platziert.")

    except Exception as e:
        print(f"\n[CRITICAL ERROR] Ein unerwarteter Fehler ist aufgetreten: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("\nVerbindung getrennt.")


if __name__ == "__main__":
    run_bot()
