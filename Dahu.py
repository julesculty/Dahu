# dahu.py
import os
import io
import shutil
import datetime as dt

import bcrypt
import pandas as pd
import plotly.express as px
import streamlit as st

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

from sqlalchemy import (
    create_engine, Column, Integer, String, Date, DateTime, Boolean,
    ForeignKey, Float, Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from streamlit_calendar import calendar


# =========================================================
# CONFIG
# =========================================================
APP_TITLE = "Jean-Louis au Dahu — Planning"
DB_PATH = os.environ.get("HOTEL_DB", "hotel.db")

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin"

HOTEL_HEADER = "Le Clos de la Balme, Corrençon-en-Vercors"
HOTEL_FOOTER = "Merci pour votre visite."

COLOR_FREE = "#2ecc71"      # vert
COLOR_RESERVED = "#e74c3c"  # rouge
COLOR_PAID = "#3498db"      # bleu
COLOR_BLOCKED = "#95a5a6"   # gris

FR_DAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# =========================================================
# MODELS
# =========================================================
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)
    role = Column(String(20), default="admin")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class Room(Base):
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True)
    number = Column(String(40), unique=True, nullable=False)  # identifiant interne unique
    name = Column(String(80), nullable=False)                 # affiché partout
    price = Column(Float, default=95.0)
    housekeeping = Column(String(10), default="clean")        # clean / todo
    maintenance = Column(Boolean, default=False)

    booking_rooms = relationship("BookingRoom", back_populates="room")
    blocks = relationship("MaintenanceBlock", back_populates="room")


class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    full_name = Column(String(120), nullable=False)
    phone = Column(String(30), default="")
    email = Column(String(120), default="")
    address = Column(String(250), default="")
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    bookings = relationship("Booking", back_populates="client")


class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)

    checkin = Column(Date, nullable=False)
    checkout = Column(Date, nullable=False)  # exclusive

    extras = Column(Float, default=0.0)
    deposit = Column(Float, default=0.0)
    payment_method = Column(String(30), default="")
    invoice_number = Column(String(40), default="")

    paid = Column(Boolean, default=False)
    paid_at = Column(DateTime, nullable=True)

    created_by = Column(String(50), default="")
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    client = relationship("Client", back_populates="bookings")
    rooms = relationship("BookingRoom", back_populates="booking", cascade="all, delete-orphan")


class BookingRoom(Base):
    __tablename__ = "booking_rooms"
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    price_per_night = Column(Float, default=0.0)

    booking = relationship("Booking", back_populates="rooms")
    room = relationship("Room", back_populates="booking_rooms")

    __table_args__ = (UniqueConstraint("booking_id", "room_id", name="uq_booking_room"),)


class MaintenanceBlock(Base):
    __tablename__ = "maintenance_blocks"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    start = Column(Date, nullable=False)
    end = Column(Date, nullable=False)  # exclusive
    reason = Column(String(160), default="Travaux")
    created_by = Column(String(50), default="")
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    room = relationship("Room", back_populates="blocks")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=dt.datetime.utcnow)
    username = Column(String(50), nullable=False)
    action = Column(String(120), nullable=False)
    meta = Column(Text, default="")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(String(400), default="")


# =========================================================
# DB INIT
# =========================================================
def db_init():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.get(Setting, "invoice_seq"):
            db.add(Setting(key="invoice_seq", value="1"))

        if not db.query(User).filter(User.username == DEFAULT_ADMIN_USER).first():
            pw_hash = bcrypt.hashpw(DEFAULT_ADMIN_PASS.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            db.add(User(username=DEFAULT_ADMIN_USER, password_hash=pw_hash, role="admin", active=True))

        if db.query(Room).count() == 0:
            for i in range(1, 16):
                number = str(100 + i)
                db.add(Room(number=number, name=f"Chambre {number}", price=95.0))

        db.commit()


def log(db, username: str, action: str, meta: str = ""):
    db.add(AuditLog(username=username, action=action, meta=meta))
    db.commit()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


# =========================================================
# HELPERS
# =========================================================
def week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def nights_count(checkin: dt.date, checkout: dt.date) -> int:
    return max(0, (checkout - checkin).days)


def iso_to_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s.split("T")[0])


def booking_conflicts_for_room(db, room_id: int, checkin: dt.date, checkout: dt.date, exclude_booking_id: int | None = None) -> bool:
    q = db.query(BookingRoom).join(Booking).filter(
        BookingRoom.room_id == room_id,
        Booking.checkout > checkin,
        Booking.checkin < checkout
    )
    if exclude_booking_id is not None:
        q = q.filter(Booking.id != exclude_booking_id)
    return q.first() is not None


def room_blocked_in_range(db, room_id: int, checkin: dt.date, checkout: dt.date) -> bool:
    blocks = db.query(MaintenanceBlock).filter(
        MaintenanceBlock.room_id == room_id,
        MaintenanceBlock.end > checkin,
        MaintenanceBlock.start < checkout
    ).all()
    return len(blocks) > 0


def next_invoice_number(db) -> str:
    s = db.get(Setting, "invoice_seq")
    seq = int(s.value or "1")
    inv = f"F-{dt.date.today().strftime('%Y%m')}-{seq:05d}"
    s.value = str(seq + 1)
    db.commit()
    return inv


# =========================================================
# PDF
# =========================================================
def build_invoice_pdf(booking: Booking) -> bytes:
    buff = io.BytesIO()
    c = canvas.Canvas(buff, pagesize=A4)
    w, h = A4

    x = 18 * mm
    y = h - 20 * mm

    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, "FACTURE")
    y -= 8 * mm

    c.setFont("Helvetica", 10)
    c.drawString(x, y, HOTEL_HEADER)
    y -= 10 * mm

    inv = booking.invoice_number or "—"
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, f"N° Facture : {inv}")
    y -= 6 * mm

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Date : {dt.date.today().strftime('%d/%m/%Y')}")
    y -= 10 * mm

    client = booking.client
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, "Client")
    y -= 6 * mm

    c.setFont("Helvetica", 10)
    c.drawString(x, y, client.full_name)
    y -= 5 * mm
    if client.phone:
        c.drawString(x, y, f"Tél : {client.phone}")
        y -= 5 * mm
    if client.email:
        c.drawString(x, y, f"Email : {client.email}")
        y -= 5 * mm
    if client.address:
        c.drawString(x, y, client.address[:90])
        y -= 8 * mm

    nights = nights_count(booking.checkin, booking.checkout)
    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Séjour : {booking.checkin.strftime('%d/%m/%Y')} → {booking.checkout.strftime('%d/%m/%Y')} ({nights} nuit(s))")
    y -= 10 * mm

    total_rooms = sum(float(br.price_per_night) for br in booking.rooms) * nights
    extras = float(booking.extras or 0.0)
    total = total_rooms + extras
    deposit = float(booking.deposit or 0.0)
    remaining = max(0.0, total - deposit)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, "Détail")
    y -= 7 * mm

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Chambres : {', '.join([br.room.name for br in booking.rooms])}")
    y -= 6 * mm
    c.drawString(x, y, f"Total chambres : {total_rooms:.2f} €")
    y -= 6 * mm
    c.drawString(x, y, f"Extras : {extras:.2f} €")
    y -= 8 * mm

    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, f"TOTAL TTC : {total:.2f} €")
    y -= 6 * mm
    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"Acompte : {deposit:.2f} €")
    y -= 6 * mm
    c.drawString(x, y, f"Reste à payer : {remaining:.2f} €")
    y -= 10 * mm

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(x, y, HOTEL_FOOTER)

    c.showPage()
    c.save()
    return buff.getvalue()


# =========================================================
# UI STYLE
# =========================================================
def inject_css():
    st.markdown(
        f"""
        <style>
        .main .block-container {{ padding-top: 1rem; padding-bottom: 2rem; max-width: 1450px; }}
        .cellwrap {{ border-radius: 14px; padding: 0px; }}
        .cellwrap button {{
            width: 100% !important;
            height: 74px !important;
            border-radius: 14px !important;
            border: none !important;
            font-weight: 900 !important;
            color: white !important;
            white-space: normal !important;
            line-height: 1.15 !important;
        }}
        .cellwrap.free button {{ background: {COLOR_FREE} !important; }}
        .cellwrap.reserved button {{ background: {COLOR_RESERVED} !important; }}
        .cellwrap.paid button {{ background: {COLOR_PAID} !important; }}
        .cellwrap.blocked button {{ background: {COLOR_BLOCKED} !important; cursor: not-allowed !important; }}

        .roomwrap button {{
            width: 100% !important;
            height: 58px !important;
            border-radius: 14px !important;
            font-weight: 900 !important;
        }}
        </style>
        """,
        unsafe_allow_html=True
    )


# =========================================================
# AUTH
# =========================================================
def login_screen():
    inject_css()

    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        st.markdown(f"## {APP_TITLE}")
        if os.path.exists("logo.png"):
            st.image("logo.png", width="stretch")

        st.markdown("### Connexion")
        u = st.text_input("Nom d'utilisateur", value="admin", key="login_user")
        p = st.text_input("Mot de passe", type="password", value="admin", key="login_pass")
        if st.button("Se connecter", width="stretch", key="login_btn"):
            with SessionLocal() as db:
                user = db.query(User).filter(User.username == u, User.active == True).first()
                if user and verify_password(p, user.password_hash):
                    st.session_state["user"] = {"username": user.username, "role": user.role}
                    log(db, user.username, "LOGIN", "Connexion")
                    st.rerun()
                else:
                    st.error("Identifiants invalides.")


def require_login():
    if "user" not in st.session_state:
        login_screen()
        st.stop()


# =========================================================
# NAV
# =========================================================
def do_logout():
    try:
        with SessionLocal() as db:
            log(db, st.session_state.get("user", {}).get("username", "unknown"), "LOGOUT", "Déconnexion")
    except Exception:
        pass
    st.session_state.pop("user", None)
    st.rerun()


def sidebar_nav():
    with st.sidebar:
        if os.path.exists("logo.png"):
            st.image("logo.png", width="stretch")

        st.markdown("### Navigation")
        page = st.radio(
            "Menu",
            ["Planning", "Arrivées / Départs", "Calendrier", "Dossiers", "Dashboard", "Clients", "Paramètres"],
            label_visibility="collapsed",
            key="nav_page"
        )
        st.divider()
        st.caption(f"Connecté : **{st.session_state['user']['username']}**")
        st.button("Logout", width="stretch", key="logout_btn", on_click=do_logout)
    return page


# =========================================================
# PANEL STATE CALLBACKS (fiabilise le planning)
# =========================================================
def set_panel_room(room_id: int):
    st.session_state["panel"] = ("room", int(room_id))


def set_panel_new(room_id: int, day_iso: str):
    st.session_state["panel"] = ("new", int(room_id), dt.date.fromisoformat(day_iso))


def set_panel_edit(booking_id: int):
    st.session_state["panel"] = ("edit", int(booking_id))


def close_panel():
    st.session_state["panel"] = None


# =========================================================
# ROOM CONTROLS
# =========================================================
def room_controls(db, room: Room, prefix: str):
    st.markdown(f"### {room.name}")

    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        hk = st.toggle("Ménage : Propre", value=(room.housekeeping == "clean"), key=f"{prefix}_hk_{room.id}")
        new_hk = "clean" if hk else "todo"
        if new_hk != room.housekeeping:
            room.housekeeping = new_hk
            db.commit()
            log(db, st.session_state["user"]["username"], "ROOM_HOUSEKEEPING", f"room={room.id} -> {new_hk}")
            st.rerun()

    with c2:
        maint = st.toggle("Travaux (global)", value=room.maintenance, key=f"{prefix}_maint_{room.id}")
        if maint != room.maintenance:
            room.maintenance = maint
            db.commit()
            log(db, st.session_state["user"]["username"], "ROOM_MAINTENANCE_GLOBAL", f"room={room.id} -> {maint}")
            st.rerun()

    with c3:
        price = st.number_input("Prix / nuit (€)", min_value=0.0, value=float(room.price), step=1.0, key=f"{prefix}_price_{room.id}")
        if float(price) != float(room.price):
            room.price = float(price)
            db.commit()
            log(db, st.session_state["user"]["username"], "ROOM_PRICE", f"room={room.id} -> {price}")
            st.rerun()


# =========================================================
# CREATE BOOKING PANEL (auto-select client)
# =========================================================
def create_booking_panel(db, default_room: Room, default_checkin: dt.date, prefix: str):
    st.markdown("### Nouveau dossier (multi-chambre)")

    selected_client_key = f"{prefix}_selected_client_id"
    if selected_client_key not in st.session_state:
        st.session_state[selected_client_key] = None

    rooms = db.query(Room).order_by(Room.number.asc()).all()
    rooms_labels = [f"{r.name}  [id:{r.id}]" for r in rooms]
    default_label = f"{default_room.name}  [id:{default_room.id}]"

    c1, c2 = st.columns(2)
    with c1:
        checkin = st.date_input("Arrivée", value=default_checkin, key=f"{prefix}_ci")
    with c2:
        checkout = st.date_input("Départ", value=default_checkin + dt.timedelta(days=1), key=f"{prefix}_co")

    selected_rooms = st.multiselect("Chambres", options=rooms_labels, default=[default_label], key=f"{prefix}_rooms")
    selected_room_ids = [int(lbl.split("[id:")[1].replace("]", "").strip()) for lbl in selected_rooms]

    st.markdown("#### Client")
    client = db.get(Client, st.session_state[selected_client_key]) if st.session_state[selected_client_key] else None

    if client:
        st.success(f"Client sélectionné : **{client.full_name}**")
        if st.button("Changer de client", width="stretch", key=f"{prefix}_change_client"):
            st.session_state[selected_client_key] = None
            st.rerun()

    if client is None:
        search = st.text_input("Rechercher client (nom/tel/email)", key=f"{prefix}_search")
        if search.strip():
            like = f"%{search.strip()}%"
            matches = db.query(Client).filter(
                (Client.full_name.ilike(like)) | (Client.phone.ilike(like)) | (Client.email.ilike(like))
            ).order_by(Client.created_at.desc()).limit(25).all()

            if matches:
                chosen = st.selectbox("Sélectionner", [f"#{c.id} — {c.full_name}" for c in matches], key=f"{prefix}_client_sel")
                cid = int(chosen.split("—")[0].strip().replace("#", ""))
                if st.button("Utiliser ce client", width="stretch", key=f"{prefix}_use_client"):
                    st.session_state[selected_client_key] = cid
                    st.rerun()
            else:
                st.info("Aucun client trouvé.")

        with st.expander("Ou créer un nouveau client", expanded=True):
            name = st.text_input("Nom complet*", key=f"{prefix}_newc_name")
            phone = st.text_input("Téléphone", key=f"{prefix}_newc_phone")
            email = st.text_input("Email", key=f"{prefix}_newc_email")
            address = st.text_input("Adresse", key=f"{prefix}_newc_addr")

            if st.button("Créer et utiliser ce client", width="stretch", key=f"{prefix}_newc_btn"):
                if not name.strip():
                    st.error("Nom requis.")
                else:
                    newc = Client(full_name=name.strip(), phone=phone.strip(), email=email.strip(), address=address.strip())
                    db.add(newc)
                    db.commit()
                    log(db, st.session_state["user"]["username"], "CLIENT_CREATE", newc.full_name)
                    st.session_state[selected_client_key] = newc.id
                    st.rerun()

    st.markdown("#### Paiement (optionnel)")
    deposit = st.number_input("Acompte (€)", min_value=0.0, value=0.0, step=1.0, key=f"{prefix}_deposit")
    payment_method = st.selectbox("Mode de paiement", ["", "CB", "Espèces", "Virement", "Chèque", "Autre"], key=f"{prefix}_pm")

    client = db.get(Client, st.session_state[selected_client_key]) if st.session_state[selected_client_key] else None

    # raisons blocage (affichées)
    reasons = []
    if checkout <= checkin:
        reasons.append("Dates invalides (départ <= arrivée)")
    if client is None:
        reasons.append("Aucun client sélectionné")
    if len(selected_room_ids) == 0:
        reasons.append("Aucune chambre sélectionnée")
    if reasons:
        st.warning("Impossible de créer : " + " | ".join(reasons))

    # disponibilité
    if checkout > checkin and selected_room_ids:
        problems = []
        for rid in selected_room_ids:
            r = db.get(Room, rid)
            if r.maintenance:
                problems.append(f"{r.name}: travaux global")
                continue
            if room_blocked_in_range(db, rid, checkin, checkout):
                problems.append(f"{r.name}: période travaux")
                continue
            if booking_conflicts_for_room(db, rid, checkin, checkout):
                problems.append(f"{r.name}: déjà réservée")
        if problems:
            st.error("Indisponible : " + " | ".join(problems))

    nights = nights_count(checkin, checkout)
    total_rooms = sum(float(db.get(Room, rid).price) for rid in selected_room_ids) * nights if selected_room_ids else 0.0
    st.info(f"Nuits: {nights} — Total chambres estimé: **{total_rooms:.2f} €** (hors extras)")

    disabled = (checkout <= checkin) or (client is None) or (len(selected_room_ids) == 0)
    if st.button("Créer le dossier", width="stretch", type="primary", disabled=disabled, key=f"{prefix}_create_btn"):
        # sécurité dispo
        for rid in selected_room_ids:
            r = db.get(Room, rid)
            if r.maintenance or room_blocked_in_range(db, rid, checkin, checkout) or booking_conflicts_for_room(db, rid, checkin, checkout):
                st.error("Création refusée : au moins une chambre est indisponible.")
                return

        b = Booking(
            client_id=client.id,
            checkin=checkin,
            checkout=checkout,
            extras=0.0,
            deposit=float(deposit),
            payment_method=str(payment_method),
            paid=False,
            created_by=st.session_state["user"]["username"]
        )
        db.add(b)
        db.flush()

        for rid in selected_room_ids:
            r = db.get(Room, rid)
            db.add(BookingRoom(booking_id=b.id, room_id=r.id, price_per_night=float(r.price)))

        db.commit()
        log(db, st.session_state["user"]["username"], "BOOKING_CREATE", f"booking={b.id}")
        st.session_state["panel"] = ("edit", b.id)
        st.rerun()


# =========================================================
# BOOKING PANEL (modif / suppression)
# =========================================================
def booking_panel(db, b: Booking, prefix: str):
    client = b.client
    st.markdown(f"### Dossier #{b.id}")
    st.write(f"**Client :** {client.full_name}")
    st.write(f"**Téléphone :** {client.phone or '-'}")
    st.write(f"**Email :** {client.email or '-'}")
    st.write(f"**Séjour :** {b.checkin} → {b.checkout}")
    st.write(f"**Chambres :** {', '.join([br.room.name for br in b.rooms])}")

    st.markdown("#### Modifier")
    c1, c2 = st.columns(2)
    with c1:
        new_ci = st.date_input("Arrivée", value=b.checkin, key=f"{prefix}_ci")
    with c2:
        new_co = st.date_input("Départ", value=b.checkout, key=f"{prefix}_co")

    deposit = st.number_input("Acompte (€)", min_value=0.0, value=float(b.deposit or 0.0), step=1.0, key=f"{prefix}_deposit")
    pm_list = ["", "CB", "Espèces", "Virement", "Chèque", "Autre"]
    pm_index = pm_list.index(b.payment_method) if (b.payment_method in pm_list) else 0
    payment_method = st.selectbox("Mode de paiement", pm_list, index=pm_index, key=f"{prefix}_pm")

    all_rooms = db.query(Room).order_by(Room.number.asc()).all()
    labels = [f"{r.name}  [id:{r.id}]" for r in all_rooms]
    current = {br.room_id for br in b.rooms}
    default = [f"{r.name}  [id:{r.id}]" for r in all_rooms if r.id in current]
    new_rooms_labels = st.multiselect("Chambres du dossier", labels, default=default, key=f"{prefix}_rooms_edit")
    new_room_ids = [int(lbl.split("[id:")[1].replace("]", "").strip()) for lbl in new_rooms_labels]

    if st.button("Enregistrer modifications", width="stretch", key=f"{prefix}_save"):
        if new_co <= new_ci:
            st.error("Dates invalides.")
            return

        for rid in new_room_ids:
            r = db.get(Room, rid)
            if r.maintenance:
                st.error(f"{r.name}: travaux global.")
                return
            if room_blocked_in_range(db, rid, new_ci, new_co):
                st.error(f"{r.name}: période travaux.")
                return
            if booking_conflicts_for_room(db, rid, new_ci, new_co, exclude_booking_id=b.id):
                st.error(f"{r.name}: déjà réservée sur la période.")
                return

        b.checkin = new_ci
        b.checkout = new_co
        b.deposit = float(deposit)
        b.payment_method = str(payment_method)

        for br in list(b.rooms):
            if br.room_id not in set(new_room_ids):
                db.delete(br)
        db.flush()

        existing = {br.room_id for br in b.rooms}
        for rid in new_room_ids:
            if rid not in existing:
                r = db.get(Room, rid)
                db.add(BookingRoom(booking_id=b.id, room_id=r.id, price_per_night=float(r.price)))

        db.commit()
        log(db, st.session_state["user"]["username"], "BOOKING_UPDATE", f"booking={b.id}")
        st.rerun()

    st.divider()
    st.markdown("#### Facturation")

    nights = nights_count(b.checkin, b.checkout)
    total_rooms = sum(float(br.price_per_night) for br in b.rooms) * nights
    extras = float(b.extras or 0.0)

    new_extras = st.number_input("Extras (€)", min_value=0.0, value=float(extras), step=1.0, key=f"{prefix}_extras")
    if float(new_extras) != float(extras):
        b.extras = float(new_extras)
        db.commit()
        log(db, st.session_state["user"]["username"], "BOOKING_EXTRAS", f"booking={b.id} extras={new_extras}")
        st.rerun()

    total = total_rooms + float(b.extras or 0.0)
    remaining = max(0.0, total - float(b.deposit or 0.0))
    st.success(f"TOTAL TTC : **{total:.2f} €** — Reste : **{remaining:.2f} €**")
    st.write(f"**Facture :** {b.invoice_number or '—'}")

    c1, c2, c3 = st.columns(3)
    with c1:
        if not b.paid:
            if st.button("Encaisser", width="stretch", type="primary", key=f"{prefix}_pay"):
                b.paid = True
                b.paid_at = dt.datetime.utcnow()
                if not (b.invoice_number or "").strip():
                    b.invoice_number = next_invoice_number(db)
                db.commit()
                log(db, st.session_state["user"]["username"], "BOOKING_PAID", f"booking={b.id} invoice={b.invoice_number}")
                st.rerun()
        else:
            st.info("Déjà payé ✅")

    with c2:
        if st.button("Facture PDF", width="stretch", key=f"{prefix}_pdf"):
            if not (b.invoice_number or "").strip():
                b.invoice_number = next_invoice_number(db)
                db.commit()
            pdf_bytes = build_invoice_pdf(b)
            st.download_button(
                "Télécharger la facture PDF",
                data=pdf_bytes,
                file_name=f"facture_{b.invoice_number or ('booking_'+str(b.id))}.pdf",
                mime="application/pdf",
                width="stretch",
                key=f"{prefix}_pdf_dl"
            )

    with c3:
        if st.button("Supprimer dossier", width="stretch", key=f"{prefix}_delete"):
            bid = b.id
            db.delete(b)
            db.commit()
            log(db, st.session_state["user"]["username"], "BOOKING_DELETE", f"booking={bid}")
            st.session_state["panel"] = None
            st.rerun()


# =========================================================
# PAGES
# =========================================================
def planning_week_page():
    st.markdown("## Planning")

    if "panel" not in st.session_state:
        st.session_state["panel"] = None

    with SessionLocal() as db:
        rooms = db.query(Room).order_by(Room.number.asc()).all()

        sel = st.date_input("Semaine à afficher", value=dt.date.today(), key="week_selector")
        start = week_start(sel)
        days = [start + dt.timedelta(days=i) for i in range(7)]
        end = start + dt.timedelta(days=7)

        bookings_week = db.query(Booking).filter(
            Booking.checkout > start,
            Booking.checkin < end
        ).all()

        by_room_bookings = {r.id: [] for r in rooms}
        for b in bookings_week:
            for br in b.rooms:
                by_room_bookings.setdefault(br.room_id, []).append(b)

        blocks_week = db.query(MaintenanceBlock).filter(
            MaintenanceBlock.end > start,
            MaintenanceBlock.start < end
        ).all()
        by_room_blocks = {}
        for bl in blocks_week:
            by_room_blocks.setdefault(bl.room_id, []).append(bl)

        header_cols = st.columns([2.7] + [1]*7)
        header_cols[0].markdown("**Chambre**")
        for i, d in enumerate(days, start=1):
            header_cols[i].markdown(f"**{FR_DAYS[d.weekday()]}**  \n{d.strftime('%d/%m')}")
        st.divider()

        for room in rooms:
            row = st.columns([2.7] + [1]*7)

            with row[0]:
                st.markdown("<div class='roomwrap'>", unsafe_allow_html=True)
                label = f"{room.name}" + (" 🛠️" if room.maintenance else "")
                st.button(
                    label,
                    width="stretch",
                    key=f"room_btn_{room.id}",
                    on_click=set_panel_room,
                    args=(room.id,)
                )
                st.markdown("</div>", unsafe_allow_html=True)

            room_bookings = by_room_bookings.get(room.id, [])
            room_blocks = by_room_blocks.get(room.id, [])

            for idx, d in enumerate(days, start=1):
                with row[idx]:
                    if room.maintenance:
                        st.markdown("<div class='cellwrap blocked'>", unsafe_allow_html=True)
                        st.button("Travaux", width="stretch", disabled=True, key=f"blk_global_{room.id}_{d.isoformat()}")
                        st.markdown("</div>", unsafe_allow_html=True)
                        continue

                    bl = next((b for b in room_blocks if (b.start <= d < b.end)), None)
                    if bl:
                        st.markdown("<div class='cellwrap blocked'>", unsafe_allow_html=True)
                        st.button(f"Travaux\n{bl.reason}", width="stretch", disabled=True, key=f"blk_{bl.id}_{d.isoformat()}")
                        st.markdown("</div>", unsafe_allow_html=True)
                        continue

                    b = next((bk for bk in room_bookings if (bk.checkin <= d < bk.checkout)), None)

                    if b is None:
                        st.markdown("<div class='cellwrap free'>", unsafe_allow_html=True)
                        st.button(
                            "Libre",
                            width="stretch",
                            key=f"free_{room.id}_{d.isoformat()}",
                            on_click=set_panel_new,
                            args=(room.id, d.isoformat())
                        )
                        st.markdown("</div>", unsafe_allow_html=True)
                    else:
                        cls = "paid" if b.paid else "reserved"
                        label = "Payé" if b.paid else "Réservé"
                        client_name = b.client.full_name if b.client else "Client"
                        st.markdown(f"<div class='cellwrap {cls}'>", unsafe_allow_html=True)
                        st.button(
                            f"{label}\n{client_name}",
                            width="stretch",
                            key=f"bk_{b.id}_{room.id}_{d.isoformat()}",
                            on_click=set_panel_edit,
                            args=(b.id,)
                        )
                        st.markdown("</div>", unsafe_allow_html=True)

        st.divider()

        panel = st.session_state.get("panel")
        if panel:
            st.button("Fermer le panneau", width="stretch", key="close_panel_btn", on_click=close_panel)
            st.markdown("## Panneau d'action")

            kind = panel[0]
            if kind == "room":
                r = db.get(Room, int(panel[1]))
                if r:
                    room_controls(db, r, prefix="week_room")
            elif kind == "new":
                r = db.get(Room, int(panel[1]))
                day = panel[2]
                if r:
                    create_booking_panel(db, default_room=r, default_checkin=day, prefix="week_new")
            elif kind == "edit":
                b = db.get(Booking, int(panel[1]))
                if b:
                    booking_panel(db, b, prefix="week_edit")


def arrivals_departures_today_page():
    st.markdown("## Arrivées / Départs aujourd’hui")

    if "panel" not in st.session_state:
        st.session_state["panel"] = None

    today = dt.date.today()
    with SessionLocal() as db:
        arrivals = db.query(Booking).filter(Booking.checkin == today).order_by(Booking.created_at.desc()).all()
        departures = db.query(Booking).filter(Booking.checkout == today).order_by(Booking.created_at.desc()).all()

        def row_public(b: Booking):
            nights = nights_count(b.checkin, b.checkout)
            rooms = ", ".join([br.room.name for br in b.rooms]) if b.rooms else "-"
            total_rooms = sum(float(br.price_per_night) for br in b.rooms) * nights
            total = total_rooms + float(b.extras or 0.0)
            remaining = max(0.0, total - float(b.deposit or 0.0))
            return {
                "ID": b.id,
                "Client": b.client.full_name if b.client else "-",
                "Chambres": rooms,
                "Payé": "✅" if b.paid else "❌",
                "Total TTC": f"{total:.2f} €",
                "Reste": f"{remaining:.2f} €",
                "Facture": b.invoice_number or "-"
            }

        t1, t2 = st.tabs([f"Arrivées ({len(arrivals)})", f"Départs ({len(departures)})"])
        with t1:
            if arrivals:
                st.dataframe(pd.DataFrame([row_public(b) for b in arrivals]), width="stretch", hide_index=True)
            else:
                st.info("Aucune arrivée aujourd’hui.")

        with t2:
            if departures:
                st.dataframe(pd.DataFrame([row_public(b) for b in departures]), width="stretch", hide_index=True)
            else:
                st.info("Aucun départ aujourd’hui.")

        st.divider()
        bid = st.number_input("Ouvrir un dossier (ID)", min_value=0, value=0, step=1, key="ad_open_id")
        st.button("Ouvrir", width="stretch", key="ad_open_btn", on_click=(lambda: set_panel_edit(int(bid))) if bid else None)

        panel = st.session_state.get("panel")
        if panel and panel[0] == "edit":
            st.divider()
            st.button("Fermer le panneau", width="stretch", key="ad_close_panel", on_click=close_panel)
            b = db.get(Booking, int(panel[1]))
            if b:
                booking_panel(db, b, prefix="ad_edit")


def calendar_page():
    st.markdown("## Calendrier")

    with SessionLocal() as db:
        rooms = db.query(Room).order_by(Room.number.asc()).all()
        choice = st.selectbox("Chambre", [f"{r.name}  [id:{r.id}]" for r in rooms], key="cal_room_choice")
        room_id = int(choice.split("[id:")[1].replace("]", "").strip())
        room = db.get(Room, room_id)

        brs = db.query(BookingRoom).filter(BookingRoom.room_id == room.id).all()
        bookings = [br.booking for br in brs]
        blocks = db.query(MaintenanceBlock).filter(MaintenanceBlock.room_id == room.id).all()

        events = []
        for b in bookings:
            client = b.client.full_name if b.client else "Client"
            events.append({
                "id": f"bk_{b.id}",
                "title": f"{client} ({'PAYÉ' if b.paid else 'RÉSERVÉ'})",
                "start": b.checkin.isoformat(),
                "end": b.checkout.isoformat(),
                "backgroundColor": (COLOR_PAID if b.paid else COLOR_RESERVED),
                "borderColor": (COLOR_PAID if b.paid else COLOR_RESERVED),
                "editable": True
            })

        for bl in blocks:
            events.append({
                "id": f"blk_{bl.id}",
                "title": f"TRAVAUX — {bl.reason}",
                "start": bl.start.isoformat(),
                "end": bl.end.isoformat(),
                "backgroundColor": COLOR_BLOCKED,
                "borderColor": COLOR_BLOCKED,
                "editable": False
            })

        options = {
            "locale": "fr",
            "initialView": st.session_state.get("cal_view", "dayGridMonth"),
            "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,timeGridWeek"},
            "selectable": True,
            "editable": True,
            "eventDurationEditable": True,
            "eventStartEditable": True,
            "dayMaxEvents": True,
        }

        cal_state = calendar(events=events, options=options, key="fullcal_main")
        if isinstance(cal_state, dict) and cal_state.get("view"):
            st.session_state["cal_view"] = cal_state["view"]

        if "cal_panel" not in st.session_state:
            st.session_state["cal_panel"] = None

        if isinstance(cal_state, dict) and cal_state.get("select"):
            if not room.maintenance:
                s = iso_to_date(cal_state["select"]["start"])
                st.session_state["cal_panel"] = ("new", room.id, s)

        if isinstance(cal_state, dict) and cal_state.get("eventClick"):
            eid = str(cal_state["eventClick"]["event"]["id"])
            if eid.startswith("bk_"):
                bid = int(eid.replace("bk_", ""))
                st.session_state["cal_panel"] = ("edit", bid)

        st.divider()
        if st.button("Fermer le panneau", width="stretch", key="cal_close"):
            st.session_state["cal_panel"] = None
            st.rerun()

        panel = st.session_state.get("cal_panel")
        if panel:
            if panel[0] == "new":
                r = db.get(Room, int(panel[1]))
                if r:
                    create_booking_panel(db, default_room=r, default_checkin=panel[2], prefix="cal_new")
            elif panel[0] == "edit":
                b = db.get(Booking, int(panel[1]))
                if b:
                    booking_panel(db, b, prefix="cal_edit")


def bookings_list_page():
    st.markdown("## Dossiers")
    if "panel" not in st.session_state:
        st.session_state["panel"] = None

    with SessionLocal() as db:
        q = st.text_input("Recherche client (nom / tel / email)", key="bk_q")
        query = db.query(Booking).join(Client)
        if q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (Client.full_name.ilike(like)) | (Client.phone.ilike(like)) | (Client.email.ilike(like))
            )
        items = query.order_by(Booking.created_at.desc()).limit(300).all()

        df = pd.DataFrame([{
            "id": b.id,
            "client": b.client.full_name,
            "checkin": b.checkin,
            "checkout": b.checkout,
            "chambres": ", ".join([br.room.name for br in b.rooms]),
            "payé": b.paid,
            "facture": b.invoice_number,
            "extras": b.extras,
            "acompte": b.deposit,
        } for b in items])
        st.dataframe(df, width="stretch", hide_index=True)

        bid = st.number_input("Ouvrir dossier (ID)", min_value=0, value=0, step=1, key="bk_open_id")
        if st.button("Ouvrir", width="stretch", key="bk_open_btn") and bid > 0:
            st.session_state["panel"] = ("edit", int(bid))
            st.rerun()

        panel = st.session_state.get("panel")
        if panel and panel[0] == "edit":
            st.divider()
            st.button("Fermer le panneau", width="stretch", key="bk_close_panel", on_click=close_panel)
            b = db.get(Booking, int(panel[1]))
            if b:
                booking_panel(db, b, prefix="bk_edit")


def dashboard_page():
    st.markdown("## Dashboard")
    with SessionLocal() as db:
        c1, c2 = st.columns(2)
        with c1:
            start = st.date_input("Du", value=dt.date.today() - dt.timedelta(days=30), key="dash_start")
        with c2:
            end = st.date_input("Au", value=dt.date.today(), key="dash_end")

        bookings = db.query(Booking).filter(
            Booking.paid == True,
            Booking.checkout > start,
            Booking.checkin < (end + dt.timedelta(days=1))
        ).all()

        revenue_by_day = {}
        for b in bookings:
            per_night = sum(float(br.price_per_night) for br in b.rooms)
            cur = max(b.checkin, start)
            stop = min(b.checkout, end + dt.timedelta(days=1))
            while cur < stop:
                revenue_by_day[cur] = revenue_by_day.get(cur, 0.0) + per_night
                cur += dt.timedelta(days=1)
            if start <= b.checkout <= end:
                revenue_by_day[b.checkout] = revenue_by_day.get(b.checkout, 0.0) + float(b.extras or 0.0)

        days = pd.date_range(start=start, end=end, freq="D")
        df = pd.DataFrame({"date": [d.date() for d in days], "ca": [float(revenue_by_day.get(d.date(), 0.0)) for d in days]})
        fig = px.bar(df, x="date", y="ca", title="Chiffre d'affaires (€/jour)")
        st.plotly_chart(fig, width="stretch")
        st.metric("CA total période", f"{df['ca'].sum():.2f} €")


def clients_page():
    st.markdown("## Clients")
    with SessionLocal() as db:
        q = st.text_input("Recherche (nom / tel / email)", key="clients_q")
        query = db.query(Client)
        if q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (Client.full_name.ilike(like)) | (Client.phone.ilike(like)) | (Client.email.ilike(like))
            )
        clients = query.order_by(Client.created_at.desc()).limit(500).all()

        df = pd.DataFrame([{
            "id": c.id,
            "nom": c.full_name,
            "téléphone": c.phone,
            "email": c.email,
            "adresse": c.address
        } for c in clients])
        st.dataframe(df, width="stretch", hide_index=True)


def settings_page():
    st.markdown("## Paramètres")
    with SessionLocal() as db:
        t_rooms, t_backup = st.tabs(["Chambres", "Sauvegarde"])

        with t_rooms:
            rooms = db.query(Room).order_by(Room.number.asc()).all()
            choice = st.selectbox("Chambre", [f"{r.name}  [id:{r.id}]" for r in rooms], key="set_room_choice")
            room_id = int(choice.split("[id:")[1].replace("]", "").strip())
            room = db.get(Room, room_id)

            new_name = st.text_input("Nom affiché (planning)", value=room.name, key="set_room_name")
            new_number = st.text_input("Identifiant interne (unique)", value=room.number, key="set_room_number")
            new_price = st.number_input("Prix / nuit (€)", min_value=0.0, value=float(room.price), step=1.0, key="set_room_price")
            maint = st.toggle("Travaux global", value=room.maintenance, key="set_room_maint")

            if st.button("Enregistrer chambre", width="stretch", key="set_room_save"):
                room.name = new_name.strip() if new_name.strip() else room.name
                room.number = new_number.strip() if new_number.strip() else room.number
                room.price = float(new_price)
                room.maintenance = maint
                db.commit()
                st.rerun()

        with t_backup:
            if st.button("Créer une sauvegarde (.db)", width="stretch", key="backup_btn"):
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = f"hotel_backup_{ts}.db"
                shutil.copyfile(DB_PATH, backup_path)
                st.success(f"Sauvegarde créée : {backup_path}")
                with open(backup_path, "rb") as f:
                    st.download_button(
                        "Télécharger la sauvegarde",
                        data=f.read(),
                        file_name=backup_path,
                        mime="application/octet-stream",
                        width="stretch",
                        key="backup_download"
                    )


# =========================================================
# MAIN
# =========================================================
def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    inject_css()
    db_init()
    require_login()

    if "panel" not in st.session_state:
        st.session_state["panel"] = None

    page = sidebar_nav()

    if page == "Planning":
        planning_week_page()
    elif page == "Arrivées / Départs":
        arrivals_departures_today_page()
    elif page == "Calendrier":
        calendar_page()
    elif page == "Dossiers":
        bookings_list_page()
    elif page == "Dashboard":
        dashboard_page()
    elif page == "Clients":
        clients_page()
    elif page == "Paramètres":
        settings_page()


if __name__ == "__main__":
    main()
