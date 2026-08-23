import math
import os
import datetime
import sys

# WORKAROUND: Zeitzonen-Absicherung
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

os.environ['TZ'] = 'Europe/Berlin'
from ib_insync import IB, Stock, Option, LimitOrder

# --- KONFIGURATION & SICHERHEIT ---
MAX_ORDERS_PER_DAY = 4       # Max. 4 Orders pro Tag
LOG_FILE = "trades_log.txt"
SYMBOL = "EEM"
CURRENCY = "USD"
EXCHANGE = "SMART"
PRIMARY_EXCHANGE = "ARCA"

# Strategie-Parameter
DISCOUNT = 0.096             # 9,6 % Abstand zum aktuellen Kurs
MIN_DAYS = 25                # Laufzeit 25 bis 38 Tage
MAX_DAYS = 38

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
        print(f"[SICHERHEITSHINWEIS] Das Tageslimit von {MAX_ORDERS_PER_DAY} Orders wurde heute erreicht ({trades_today_count}/{MAX_ORDERS_PER_DAY}).")
        return True
    print(f"[INFO] Bisherige Orders heute: {trades_today_count} von maximal {MAX_ORDERS_PER_DAY}.")
    return False

def log_trade(details):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{now_str}] {details}\n")

def run_bot():
    ib = IB()
    try:
        print(f"=== SHORT PUT BOT: EMERGING MARKETS ETF ({SYMBOL}) ===")
        
        # 1. Sicherheits-Check: Tages-Sperre
        if already_traded_today():
            return
            
        # 2. Verbindung herstellen (Standard TWS: 7496/7497, Gateway: 4001/4002)
        ib.connect('127.0.0.1', 4002, clientId=99, timeout=20)
        print("[INFO] Erfolgreich mit IB Gateway verbunden.")
        
        # Verzögerte Marktdaten explizit anfordern
        ib.reqMarketDataType(3)
        
        # 3. Offene Orders prüfen
        stock = Stock(SYMBOL, EXCHANGE, CURRENCY, primaryExchange=PRIMARY_EXCHANGE)
        ib.qualifyContracts(stock)
        
        open_trades = ib.openTrades()
        if any(t.contract.symbol == SYMBOL for t in open_trades):
            print(f"[SICHERHEITSHINWEIS] Es existiert bereits eine offene Order für {SYMBOL}!")
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
            
        # Suchen nach der SMART-Kette mit den meisten Laufzeiten/Strikes
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
            
        # Strike-Auswahl: Explizites Typecasting auf Float zur Vermeidung von Vergleichsfehlern
        valid_strikes = [float(s) for s in chain.strikes if float(s) <= calculated_target]
        if not valid_strikes:
            print("[FEHLER] Kein passender Strike unterhalb des Ziel-Preises vorhanden.")
            return
        target_strike = max(valid_strikes)
        
        # Optionskontrakt qualifizieren
        put_option = Option(SYMBOL, expiry, target_strike, 'P', 'SMART', currency=CURRENCY)
        qualified = ib.qualifyContracts(put_option)
        if not qualified:
            print("[FEHLER] Kontrakt konnte nicht qualifiziert werden.")
            return
            
        # 6. Marktdaten abfragen & präzise Warten auf Ticker
        print("[INFO] Frage verzögerte Marktdaten ab...")
        ticker = ib.reqMktData(put_option, '', False, False)
        
        # Erhöht auf 10 Sekunden für verzögerte Datenfeeds über das Gateway
        for _ in range(10):
            ib.sleep(1)
            if ticker.bid > 0 or ticker.ask > 0:
                break
                
        bid = ticker.bid
        ask = ticker.ask
        
        # Mid-Preis Ermittlung mit striktem Validierungs-Fallback
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
            # Kritischer Abbruch statt blindem 0.10 USD default Trade
            print("[FEHLER] Keine validen Preisdaten (Bid/Ask/Close) empfangen. Order-Platzierung abgebrochen.")
            return

        # 7. Sauber strukturierte Zusammenfassung anzeigen
        print("\n--- ZUSAMMENFASSUNG ---")
        print(f"ETF: iShares MSCI Emerging Markets ({SYMBOL}) | Letzter Kurs: {last_close:.2f} USD")
        print(f"Berechneter Zielpreis (-9.6%): {calculated_target:.2f} USD")
        print(f"Gewählter Strike: {target_strike} USD | Verfallsdatum: {expiry}")
        print(f"Option: {put_option.localSymbol} ({put_option.exchange})")
        
        if bid > 0 and ask > 0:
            print(f"Verzögerte Marktdaten: Bid {bid:.2f} USD | Ask {ask:.2f} USD")
        else:
            print("⚠️ Warnung: Keine aktiven Bid/Ask-Kurse. Historischer Close/Last als Fallback genutzt!")
            
        print(f"-> Berechnete Ziel-Prämie (Limit): {target_price:.2f} USD")
        
        # Zusätzliche Sicherheitsabfrage bei Fallback-Preisen
        if is_fallback_used:
            print("⚠️ ACHTUNG: Der Preis basiert auf historischen Daten. Limit manuell prüfen!")

        # 8. Bestätigung abfragen
        user_input = input("\nSoll der Short Put zu diesem Mid-Preis platziert werden? (j/n): ").strip()
        if user_input.lower() == 'j':
            order = LimitOrder('SELL', 1, target_price)
            trade = ib.placeOrder(put_option, order)
            
            # Warten auf Status-Update der API
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
