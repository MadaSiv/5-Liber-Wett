from __future__ import annotations
import os
import secrets
import threading
from dataclasses import dataclass, field
from decimal import Decimal, getcontext, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Optional
import json

from nicegui import ui, app

# =========================
#   Konfiguration & Setup
# =========================

# Geldarithmetik
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_UP
STAKE = Decimal("5.00")
CENT = Decimal("0.01")

# Passwortschutz (optional)
APP_PASSWORD = os.getenv("APP_PASSWORD")  # wenn None/"" → kein Login nötig

# Speicherort (ohne Render-Disk: im Projektordner)
APP_DIR = Path(os.getenv("APP_DIR", str(Path.cwd() / "data")))
DEFAULT_PATH = APP_DIR / "wette_pot.json"
APP_DIR.mkdir(parents=True, exist_ok=True)

# Zeitzone Schweiz (robust)
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        CH_TZ = ZoneInfo("Europe/Zurich")
    except ZoneInfoNotFoundError:
        import tzdata  # type: ignore
        CH_TZ = ZoneInfo("Europe/Zurich")
except Exception:
    CH_TZ = datetime.now().astimezone().tzinfo or timezone.utc


def q(amount: Decimal) -> Decimal:
    return amount.quantize(CENT)


def chf(amount: Decimal) -> str:
    return f"{q(amount):.2f} CHF"


class Kind(Enum):
    BET = "BET"
    BEER = "BEER"
    TRANSFER = "TRANSFER"


TYPE_LABELS = {
    Kind.BET: "Wette",
    Kind.BEER: "Bierkauf",
    Kind.TRANSFER: "Ausgleichszahlung",
}


@dataclass(slots=True)
class Transaction:
    timestamp: datetime
    kind: Kind
    losers: str
    comment: str
    delta: Decimal
    payer: str = ""
    receiver: str = ""
    transfer_amount: Decimal = Decimal("0.00")

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "kind": self.kind.value,
            "losers": self.losers,
            "comment": self.comment,
            "delta": str(q(self.delta)),
            "payer": self.payer,
            "receiver": self.receiver,
            "transfer_amount": str(q(self.transfer_amount)),
        }

    @staticmethod
    def from_dict(d: dict) -> "Transaction":
        dt = datetime.fromisoformat(d.get("timestamp")) if d.get("timestamp") else datetime.now(CH_TZ)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(CH_TZ)
        return Transaction(
            timestamp=dt,
            kind=Kind(d["kind"]),
            losers=d.get("losers", ""),
            comment=d.get("comment", ""),
            delta=Decimal(d.get("delta", "0.00")).quantize(CENT),
            payer=d.get("payer", ""),
            receiver=d.get("receiver", ""),
            transfer_amount=Decimal(d.get("transfer_amount", "0.00")).quantize(CENT),
        )


@dataclass(slots=True)
class Pot:
    balance: Decimal = Decimal("0.00")
    history: List[Transaction] = field(default_factory=list)
    last_reset: Optional[datetime] = None

    def to_data(self) -> dict:
        return {
            "balance": str(q(self.balance)),
            "history": [t.to_dict() for t in self.history],
            "last_reset": self.last_reset.isoformat() if self.last_reset else None,
        }

    def from_data(self, data: dict) -> None:
        self.balance = q(Decimal(data.get("balance", "0.00")))
        self.history = [Transaction.from_dict(x) for x in data.get("history", [])]
        lr = data.get("last_reset")
        if lr:
            dt = datetime.fromisoformat(lr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            self.last_reset = dt.astimezone(CH_TZ)
        else:
            self.last_reset = None

    def recalc_balance(self) -> None:
        total = Decimal("0.00")
        for t in self.history:
            if t.kind in (Kind.BET, Kind.BEER):
                total += t.delta
        self.balance = q(total)

    # Business-Methoden
    def add_bet(self, sven_right: bool, sevi_right: bool, comment: Optional[str] = None, stake: Decimal = STAKE) -> str:
        stake = q(stake)
        if stake <= 0:
            return "Fehler: Einsatz muss > 0 sein."
        deposit = Decimal("0.00")
        losers = []
        if not sven_right:
            deposit += stake
            losers.append("Sven verliert")
        if not sevi_right:
            deposit += stake
            losers.append("Sevi verliert")
        if not losers:
            losers.append("beide richtig")
        losers_text = ", ".join(losers)
        clean_comment = comment.strip() if comment else ""
        self.balance = q(self.balance + deposit)
        self.history.append(Transaction(datetime.now(CH_TZ), Kind.BET, losers_text, clean_comment, q(deposit)))
        return f"Wette verbucht: {losers_text}. Neuer Saldo: {chf(self.balance)}"

    def pay_beer(self, amount: Decimal, payer: str) -> str:
        amount = q(amount)
        if amount <= 0:
            return "Fehler: Betrag muss > 0 sein."
        if amount > self.balance:
            return f"Fehler: Betrag {chf(amount)} übersteigt den Saldo {chf(self.balance)}."
        self.balance = q(self.balance - amount)
        self.history.append(Transaction(datetime.now(CH_TZ), Kind.BEER, "Bier bezahlt", "", -amount, payer))
        return f"Bezahlt: {chf(amount)} für Bier (Zahler: {payer}). Neuer Saldo: {chf(self.balance)}"

    def transfer(self, amount: Decimal, payer: str, receiver: str, comment: str = "Ausgleich") -> str:
        amount = q(amount)
        if amount <= 0:
            return "Fehler: Betrag muss > 0 sein."
        if payer == receiver:
            return "Fehler: Zahler und Empfänger dürfen nicht identisch sein."
        sven_total, sevi_total = self.person_totals()
        available = sven_total if payer == "Sven" else sevi_total
        if amount > available:
            return f"Fehler: {payer} hat nur {chf(available)} verfügbar für Transfer."
        self.history.append(Transaction(datetime.now(CH_TZ), Kind.TRANSFER, "", comment, Decimal("0.00"), payer, receiver, amount))
        return f"Transfer verbucht: {payer} → {receiver} {chf(amount)} (Pot unverändert: {chf(self.balance)})"

    def reset(self) -> None:
        self.balance = Decimal("0.00")
        self.history.clear()
        self.last_reset = datetime.now(CH_TZ)

    def person_totals(self) -> tuple[Decimal, Decimal]:
        sven = Decimal("0.00")
        sevi = Decimal("0.00")
        for t in self.history:
            if t.kind == Kind.BET and t.delta > 0:
                losers_flags = []
                if "Sven verliert" in t.losers:
                    losers_flags.append("Sven")
                if "Sevi verliert" in t.losers:
                    losers_flags.append("Sevi")
                n = len(losers_flags)
                if n == 0:
                    continue
                share = q(t.delta / n)
                if "Sven" in losers_flags:
                    sven += share
                if "Sevi" in losers_flags:
                    sevi += share
            elif t.kind == Kind.BEER and t.delta < 0:
                if t.payer == "Sven":
                    sven += t.delta  # negativ -> reduziert
                elif t.payer == "Sevi":
                    sevi += t.delta
            elif t.kind == Kind.TRANSFER:
                amt = q(t.transfer_amount)
                if t.payer == "Sven":
                    sven -= amt
                elif t.payer == "Sevi":
                    sevi -= amt
                if t.receiver == "Sven":
                    sven += amt
                elif t.receiver == "Sevi":
                    sevi += amt
        return q(sven), q(sevi)


# ==========
#   State
# ==========
lock = threading.Lock()
pot = Pot()


def load_state() -> None:
    if DEFAULT_PATH.exists():
        with open(DEFAULT_PATH, "r", encoding="utf-8") as f:
            pot.from_data(json.load(f))
        pot.recalc_balance()


def save_state() -> None:
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DEFAULT_PATH, "w", encoding="utf-8") as f:
        json.dump(pot.to_data(), f, ensure_ascii=False, indent=2)


load_state()

# ==========
#   UI
# ==========

BG = "#FFF8EA"
SURFACE = "#FFFFFF"
TEXT = "#2B1E0E"
ACCENT = "#EAB308"
ui.colors(primary=ACCENT)


def ts_fmt(dt: datetime) -> str:
    return dt.astimezone(CH_TZ).strftime("%d.%m.%Y %H:%M")


def build_ui():
    """Baut die komplette App-UI – wird nur nach erfolgreichem Login aufgerufen."""

    # Header
    with ui.header().classes('items-center justify-between bg-white'):
        ui.label('🍺 5-Franken-Wette').style(f'color:{TEXT}; font-weight:700; font-size:20px')
        right_row = ui.row().classes('items-center gap-3')
        with right_row:
            balance_label = ui.label().style(f'color:{TEXT}; font-size:16px')
            if APP_PASSWORD:
                def do_logout():
                    app.storage.user.pop('auth_ok', None)
                    ui.open('/')
                ui.button('Logout', on_click=do_logout).props('flat')

    # Personensalden
    with ui.row().classes('items-center gap-6 px-4 py-1'):
        sven_label = ui.label().style(f'color:{TEXT}; opacity:0.8')
        sevi_label = ui.label().style(f'color:{TEXT}; opacity:0.8')

    # Verlaufstabelle
    table_rows: list[dict] = []
    sum_label = ui.label().style(f'color:{TEXT}; font-weight:700')

    def refresh_top():
        with lock:
            pot.recalc_balance()
            sven, sevi = pot.person_totals()
            balance_label.text = f'Aktueller Saldo: {chf(pot.balance)}'
            sven_label.text = f'Sven: {sven:.2f} CHF'
            sevi_label.text = f'Sevi: {sevi:.2f} CHF'

    def rebuild_rows() -> None:
        table_rows.clear()
        total = Decimal("0.00")
        with lock:
            for t in pot.history:
                if t.kind == Kind.TRANSFER:
                    betrag_display = f"{q(t.transfer_amount):.2f}"
                else:
                    betrag_display = f"{q(t.delta):.2f}"
                    total += q(t.delta)
                if t.kind == Kind.BET:
                    main = f"Verlierer → {t.losers}."
                elif t.kind == Kind.BEER:
                    main = f"Zahler → {t.payer or '?'}."
                else:
                    main = f"Ausgleich → {t.payer} → {t.receiver}."
                table_rows.append({
                    'Zeit': ts_fmt(t.timestamp),
                    'Typ': TYPE_LABELS.get(t.kind, t.kind.value),
                    'Betrag': betrag_display,
                    'Verlierer/Zahler/Ausgleich': main,
                    'Kommentar': t.comment,
                })
        sum_label.text = f"Summe angezeigt: {q(total):.2f} CHF"

    columns = [
        {'name': 'Zeit', 'label': 'Zeit', 'field': 'Zeit', 'sortable': True},
        {'name': 'Typ', 'label': 'Typ', 'field': 'Typ', 'sortable': True},
        {'name': 'Betrag', 'label': 'Betrag', 'field': 'Betrag', 'sortable': True},
        {'name': 'Verlierer/Zahler/Ausgleich', 'label': 'Verlierer/Zahler/Ausgleich', 'field': 'Verlierer/Zahler/Ausgleich', 'sortable': True},
        {'name': 'Kommentar', 'label': 'Kommentar', 'field': 'Kommentar', 'sortable': True},
    ]

    with ui.card().classes('m-4'):
        ui.label('📜 Verlauf').style(f'color:{TEXT}; font-weight:600')
        table = ui.table(columns=columns, rows=table_rows, row_key='Zeit').props('flat').classes('w-full')
        with ui.row().classes('justify-between items-center mt-2'):
            last_reset_label = ui.label().style('opacity:0.7')
            sum_label

    def refresh_table():
        rebuild_rows()
        table.update()
        with lock:
            if pot.last_reset is None:
                last_reset_label.text = "Zuletzt zurückgesetzt: nie"
            else:
                last_reset_label.text = "Zuletzt zurückgesetzt: " + pot.last_reset.astimezone(CH_TZ).strftime("%d.%m.%Y %H:%M")

    # Dialoge / Aktionen
    def dlg_neue_wette():
        with ui.dialog() as dialog, ui.card().classes('min-w-[360px]'):
            ui.label('🎲 Neue Wette').classes('text-lg font-semibold')
            is_standard = ui.toggle(['5-Liber', 'Individuell'], value='5-Liber').classes('my-2')
            stake_in = ui.input('Einsatz je Person (CHF)').bind_visibility_from(is_standard, 'value', lambda v: v == 'Individuell')
            stake_in.value = f"{STAKE:.2f}"

            sven_richtig = ui.toggle(['Sven richtig?'], value=[]).classes('mt-2')
            sevi_richtig = ui.toggle(['Sevi richtig?'], value=[]).classes('mt-1')
            comment = ui.input('Kommentar (optional)').classes('mt-2')

            def submit():
                try:
                    stake = STAKE
                    if is_standard.value == 'Individuell':
                        stake = q(Decimal((stake_in.value or "").replace(",", ".")))
                        if stake <= 0:
                            ui.notify('Einsatz muss > 0 sein.', type='negative'); return
                    sven_ok = ('Sven richtig?' in (sven_richtig.value or []))
                    sevi_ok = ('Sevi richtig?' in (sevi_richtig.value or []))
                    with lock:
                        msg = pot.add_bet(sven_ok, sevi_ok, comment.value or "", stake)
                        save_state()
                    ui.notify(msg, type='positive')
                    refresh_top(); refresh_table()
                    dialog.close()
                except Exception:
                    ui.notify('Ungültige Eingabe.', type='negative')

            with ui.row().classes('justify-end gap-2 mt-3'):
                ui.button('Abbrechen', on_click=dialog.close)
                ui.button('OK', on_click=submit, color='primary')

        dialog.open()

    def dlg_bier_bezahlen():
        with ui.dialog() as dialog, ui.card().classes('min-w-[360px]'):
            ui.label('🍺 Bier bezahlen').classes('text-lg font-semibold')
            payer = ui.select(['Sven', 'Sevi'], value='Sven', label='Zahler')
            amount = ui.input('Betrag (CHF)')
            comment = ui.input('Kommentar (optional)')

            def submit():
                try:
                    betrag = Decimal((amount.value or "").replace(",", "."))
                    with lock:
                        msg = pot.pay_beer(betrag, payer.value)
                        if msg.startswith("Fehler"):
                            ui.notify(msg, type='negative'); return
                        save_state()
                    ui.notify(msg, type='positive')
                    refresh_top(); refresh_table()
                    dialog.close()
                except Exception:
                    ui.notify('Ungültiger Betrag.', type='negative')
            with ui.row().classes('justify-end gap-2 mt-3'):
                ui.button('Abbrechen', on_click=dialog.close)
                ui.button('OK', on_click=submit, color='primary')
        dialog.open()

    def dlg_transfer():
        with ui.dialog() as dialog, ui.card().classes('min-w-[360px]'):
            ui.label('🔁 Geld transferieren').classes('text-lg font-semibold')
            payer = ui.select(['Sven', 'Sevi'], value='Sven', label='Zahler')
            amount = ui.input('Betrag (CHF)')
            info_line = ui.label().style('opacity:0.8')

            def update_info():
                with lock:
                    sven_total, sevi_total = pot.person_totals()
                avail = sven_total if payer.value == 'Sven' else sevi_total
                info_line.text = f'Verfügbar für {payer.value}: {chf(avail)}'

            payer.on('update:model-value')(lambda _: update_info())
            update_info()

            def submit():
                try:
                    amt = q(Decimal((amount.value or "").replace(",", ".")))
                    receiver = 'Sevi' if payer.value == 'Sven' else 'Sven'
                    with lock:
                        res = pot.transfer(amt, payer.value, receiver)
                        if res.startswith("Fehler"):
                            ui.notify(res, type='negative'); return
                        save_state()
                    ui.notify(res, type='positive')
                    refresh_top(); refresh_table()
                    dialog.close()
                except Exception:
                    ui.notify('Ungültiger Betrag.', type='negative')
            with ui.row().classes('justify-end gap-2 mt-3'):
                ui.button('Abbrechen', on_click=dialog.close)
                ui.button('OK', on_click=submit, color='primary')
        dialog.open()

    def dlg_ausgleich():
        with lock:
            sven, sevi = pot.person_totals()
            if sven < 0 and sevi > 0:
                amount = min(sevi, -sven); payer, receiver = "Sevi", "Sven"
            elif sevi < 0 and sven > 0:
                amount = min(sven, -sevi); payer, receiver = "Sven", "Sevi"
            else:
                ui.notify('Kein Ausgleich nötig – niemand ist im Minus.', type='info'); return
        with ui.dialog() as dialog, ui.card():
            ui.label('🤝 Ausgleich vorschlagen').classes('text-lg font-semibold')
            ui.label(f'Vorschlag: {payer} → {receiver} {chf(amount)}.\nDirekt buchen?')
            def do_book():
                with lock:
                    res = pot.transfer(amount, payer, receiver, comment="Autom. Ausgleich")
                    if res.startswith("Fehler"):
                        ui.notify(res, type='negative'); return
                    save_state()
                ui.notify(res, type='positive')
                refresh_top(); refresh_table()
                dialog.close()
            with ui.row().classes('justify-end gap-2 mt-3'):
                ui.button('Abbrechen', on_click=dialog.close)
                ui.button('Buchen', on_click=do_book, color='primary')
        dialog.open()

    def do_reset():
        with ui.dialog() as dialog, ui.card():
            ui.label('🧹 Verlauf & Saldo löschen').classes('text-lg font-semibold')
            ui.label('Wirklich Verlauf & Saldo komplett löschen?')
            def yes():
                with lock:
                    pot.reset()
                    save_state()
                refresh_top(); refresh_table()
                ui.notify('Verlauf und Saldo wurden gelöscht.', type='positive')
                dialog.close()
            with ui.row().classes('justify-end gap-2 mt-3'):
                ui.button('Abbrechen', on_click=dialog.close)
                ui.button('Löschen', on_click=yes, color='negative')
        dialog.open()

    # Aktions-Buttons
    with ui.row().classes('gap-2 px-4'):
        ui.button('🎲 Neue Wette', on_click=dlg_neue_wette)
        ui.button('🍺 Bier bezahlen', on_click=dlg_bier_bezahlen)
        ui.button('🔁 Geld transferieren', on_click=dlg_transfer)
        ui.button('🤝 Ausgleich vorschlagen', on_click=dlg_ausgleich)
        ui.button('🧹 Verlauf & Saldo löschen', on_click=do_reset).props('color=negative')

    # Footer
    with ui.footer().classes('justify-end'):
        ui.label('Made with NiceGUI')

    # Initial refresh
    def initial_refresh():
        refresh_top()
        refresh_table()

    initial_refresh()


@ui.page('/')
def index():
    # Wenn Passwort gesetzt und (noch) nicht angemeldet → Login-Card
    if APP_PASSWORD and not app.storage.user.get('auth_ok'):
        with ui.card().classes('max-w-sm mx-auto mt-24'):
            ui.label('🔒 Login').classes('text-lg font-semibold')
            pwd = ui.input('Passwort', password=True, password_toggle_button=True).classes('mt-2')
            def do_login():
                if (pwd.value or "") == APP_PASSWORD:
                    app.storage.user['auth_ok'] = True
                    ui.open('/')  # Seite neu laden
                else:
                    ui.notify('Falsches Passwort', type='negative')
            ui.button('Login', on_click=do_login, color='primary').classes('mt-3')
        return

    # Andernfalls: App aufbauen
    build_ui()


# Secret für Session-Speicher (wichtig für app.storage.user)
STORAGE_SECRET = os.getenv("STORAGE_SECRET") or secrets.token_urlsafe(32)

# Run (Render setzt $PORT automatisch)
ui.run(
    title='5 Franken Wette',
    host='0.0.0.0',
    port=int(os.getenv('PORT', '8080')),
    reload=False,
    storage_secret=STORAGE_SECRET,  # <<<< wichtig!
)
