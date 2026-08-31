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
