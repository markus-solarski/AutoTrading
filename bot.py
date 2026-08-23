
import math
import os
import time
import datetime
from ib_insync import IB, Stock, Option, LimitOrder

MAX_ORDERS_PER_DAY = 4
LOG_FILE = "trades_log.txt"
SYMBOL = "EEM"
CURRENCY = "USD"
EXCHANGE = "SMART"
PRIMARY_EXCHANGE = "ARCA"
DISCOUNT = 0.096
MIN_DAYS = 25
MAX_DAYS = 38

os.environ["TZ"] = "Europe/Berlin"
if hasattr(time, "tzset"):
    time.tzset()
else:
    print("[WARNUNG] time.tzset() nicht verfuegbar (z.B. unter Windows). "
          "Lokale Systemzeit wird verwendet, TZ-Variable wird ignoriert.")

def already_traded_today():
    if not os.path.exists(LOG_FILE):
        return False
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    trades_today_count = 0
    with open(LOG_FILE, "r") as f:
        for line in f:
            if today_str in line and "Order platziert" in line:
                trades_today_count += 1
    if trades_today_count >= MAX_ORDERS_PER_DAY:
        print(f"[SICHERHEITSHINWEIS] Tageslimit erreicht ({trades_today_count}/{MAX_ORDERS_PER_DAY}).")
        return True
    print(f"[INFO] Bisherige Orders heute: {trades_today_count} von maximal {MAX_ORDERS_PER_DAY}.")
    return False

def log_trade(details):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{now_str}] {details}\n")

def select_best_chain(chains, symbol):
    smart_chains = [c for c in chains if c.exchange == "SMART"]
    candidates = smart_chains if smart_chains else list(chains)
    if not candidates:
        return None
    exact_match = [c for c in candidates if getattr(c, "tradingClass", None) == symbol]
    pool = exact_match if exact_match else candidates
    best = max(pool, key=lambda c: (len(c.expirations), len(c.strikes)))
    return best

def check_margin_and_risk(ib, put_option, order, target_strike):
    """Simuliert die Order per whatIfOrder() um Margin-Auswirkung zu pruefen,
    und schaetzt das maximale Andienungsrisiko (Strike * 100 - erhaltene Praemie)."""
    try:
        what_if = ib.whatIfOrder(put_option, order)
    except Exception as e:
        print(f"[WARNUNG] Margin-Simulation (whatIfOrder) fehlgeschlagen: {e}")
        return True

    if what_if is None:
        print("[WARNUNG] Keine Antwort von whatIfOrder(). Margin-Pruefung nicht moeglich.")
        return True

    init_margin_before = float(what_if.initMarginBefore or 0)
    init_margin_after = float(what_if.initMarginChange or 0) + init_margin_before
    maint_margin_change = float(what_if.maintMarginChange or 0)
    equity_with_loan = float(what_if.equityWithLoanAfter or 0)

    max_assignment_cost = target_strike * 100
    premium_received = order.lmtPrice * 100

    print("\n--- MARGIN & RISIKO-PRUEFUNG (whatIfOrder) ---")
    print(f"Initial Margin nachher (geschaetzt): {init_margin_after:.2f} USD")
    print(f"Maintenance Margin Aenderung: {maint_margin_change:.2f} USD")
    print(f"Equity with Loan (nachher): {equity_with_loan:.2f} USD")
    print(f"Max. Andienungskosten bei Ausuebung: {max_assignment_cost:.2f} USD (abzgl. Praemie {premium_received:.2f} USD)")

    if equity_with_loan > 0 and init_margin_after > equity_with_loan * 0.9:
        print("[SICHERHEITSHINWEIS] Initial Margin nach Order liegt bei >90% des verfuegbaren Eigenkapitals!")
        confirm = input("Trotzdem fortfahren? (j/n): ").strip().lower()
        if confirm != "j":
            return False

    if equity_with_loan > 0 and max_assignment_cost > equity_with_loan:
        print(f"[SICHERHEITSHINWEIS] Max. Andienungskosten ({max_assignment_cost:.2f} USD) uebersteigen dein Eigenkapital ({equity_with_loan:.2f} USD)!")
        confirm = input("Trotzdem fortfahren? (j/n): ").strip().lower()
        if confirm != "j":
            return False

    return True

def run_bot():
    ib = IB()
    try:
        print(f"=== SHORT PUT BOT: EMERGING MARKETS ETF ({SYMBOL}) ===")

        if already_traded_today():
            return

        ib.connect("127.0.0.1", 4002, clientId=99, timeout=20)
        print("[INFO] Erfolgreich mit IB Gateway verbunden.")
        ib.reqMarketDataType(3)

        stock = Stock(SYMBOL, EXCHANGE, CURRENCY, primaryExchange=PRIMARY_EXCHANGE)
        ib.qualifyContracts(stock)

        open_trades = ib.openTrades()
        if any(t.contract.symbol == SYMBOL for t in open_trades):
            print(f"[SICHERHEITSHINWEIS] Es existiert bereits eine offene Order fuer {SYMBOL}!")
            return

        bars = ib.reqHistoricalData(
            stock, endDateTime="", durationStr="2 D",
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True
        )
        if not bars:
            print("[FEHLER] Keine Kursdaten empfangen.")
            return

        last_bar_date = bars[-1].date
        today_date = datetime.date.today()
        bar_date_only = last_bar_date.date() if hasattr(last_bar_date, "date") else last_bar_date
        if bar_date_only < today_date - datetime.timedelta(days=3):
            print(f"[WARNUNG] Letzter Kursdatensatz vom {bar_date_only} wirkt veraltet. Bitte pruefen.")

        last_close = bars[-1].close
        calculated_target = last_close * (1 - DISCOUNT)

        today = datetime.date.today()
        min_exp = today + datetime.timedelta(days=MIN_DAYS)
        max_exp = today + datetime.timedelta(days=MAX_DAYS)

        chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        if not chains:
            print("[FEHLER] Keine Optionsdaten gefunden.")
            return

        chain = select_best_chain(chains, SYMBOL)
        if chain is None:
            print("[FEHLER] Keine passende Options-Chain gefunden.")
            return

        expiry = None
        for exp_str in sorted(chain.expirations):
            exp_date = datetime.datetime.strptime(exp_str, "%Y%m%d").date()
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

        put_option = Option(SYMBOL, expiry, target_strike, "P", "SMART", currency=CURRENCY)
        qualified = ib.qualifyContracts(put_option)
        if not qualified:
            print("[FEHLER] Kontrakt konnte nicht qualifiziert werden.")
            return

        print("[INFO] Frage verzoegerte Marktdaten ab...")
        ticker = ib.reqMktData(put_option, "", False, False)

        try:
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
                print("[FEHLER] Keine validen Preisdaten (Bid/Ask/Close) empfangen. Abbruch.")
                return

            print("\n--- ZUSAMMENFASSUNG ---")
            print(f"ETF: iShares MSCI Emerging Markets ({SYMBOL}) | Letzter Kurs: {last_close:.2f} USD")
            print(f"Berechneter Zielpreis (-9.6%): {calculated_target:.2f} USD")
            print(f"Gewaehlter Strike: {target_strike} USD | Verfallsdatum: {expiry}")
            print(f"Option: {put_option.localSymbol} ({put_option.exchange})")

            if bid > 0 and ask > 0:
                print(f"Verzoegerte Marktdaten: Bid {bid:.2f} USD | Ask {ask:.2f} USD")
            else:
                print("Warnung: Keine aktiven Bid/Ask-Kurse. Historischer Close/Last als Fallback genutzt!")

            print(f"-> Berechnete Ziel-Praemie (Limit): {target_price:.2f} USD")

            if is_fallback_used:
                print("ACHTUNG: Der Preis basiert auf historischen Daten. Limit manuell pruefen!")

            user_input = input("\nSoll der Short Put zu diesem Mid-Preis platziert werden? (j/n): ").strip()
            if user_input.lower() == "j":
                order = LimitOrder("SELL", 1, target_price)

                margin_ok = check_margin_and_risk(ib, put_option, order, target_strike)
                if not margin_ok:
                    print("\n[ABBRUCH] Order wegen Margin-/Risiko-Bedenken nicht platziert.")
                else:
                    trade = ib.placeOrder(put_option, order)
                    ib.sleep(2)
                    print(f"\n[OK] Status: {trade.orderStatus.status}")
                    log_trade(f"Order platziert: {SYMBOL} Strike {target_strike} Expiry {expiry} Limit {target_price:.2f} USD | Status: {trade.orderStatus.status}")
            else:
                print("\n[ABBRUCH] Es wurde keine Order platziert.")
        finally:
            ib.cancelMktData(put_option)

    except Exception as e:
        print(f"\n[CRITICAL ERROR] Ein unerwarteter Fehler ist aufgetreten: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("\nVerbindung getrennt.")

if __name__ == "__main__":
    run_bot()
