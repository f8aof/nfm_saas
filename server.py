#!/usr/bin/env python3
# =============================================================================
#  NFM SaaS — Serveur principal
#  Flask + Flask-SocketIO + SQLite + Auth + Email confirmation
#  F8AOF — Noise Floor Meter Platform
# =============================================================================

import os, json, time, secrets, hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, flash)
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
from pathlib import Path

# ── Config ──
BASE_DIR  = Path(__file__).parent
DB_PATH   = BASE_DIR / "instance" / "nfm.db"
SECRET    = os.environ.get("NFM_SECRET", secrets.token_hex(32))
MAIL_FROM = os.environ.get("NFM_MAIL_FROM", "noreply@nfm.local")
BASE_URL  = os.environ.get("NFM_BASE_URL", "http://localhost:5000")

app = Flask(__name__)
app.secret_key = SECRET
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Stations connectées en mémoire : {station_id: {sid, last_data}}
connected_agents = {}

# =============================================================================
#  BASE DE DONNÉES
# =============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign      TEXT    UNIQUE NOT NULL,
    email         TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    confirmed     INTEGER DEFAULT 0,
    confirm_token TEXT,
    token_expiry  REAL,
    created_at    REAL    DEFAULT (unixepoch()),
    last_login    REAL,
    is_admin      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    name          TEXT    NOT NULL,
    description   TEXT,
    -- Carte son
    audio_device_name  TEXT,
    audio_device_idx   INTEGER,
    audio_sample_rate  INTEGER DEFAULT 48000,
    audio_bits         INTEGER DEFAULT 24,
    -- Transceiver
    rig_brand     TEXT,
    rig_model     TEXT,
    rig_hamlib_id INTEGER,
    rig_port      TEXT,
    rig_baud      INTEGER DEFAULT 9600,
    rig_ptt_type  TEXT    DEFAULT "RTS",
    -- Mesure
    fft_size      INTEGER DEFAULT 4096,
    fft_window    TEXT    DEFAULT "hann",
    fft_avg       INTEGER DEFAULT 32,
    percentile    INTEGER DEFAULT 10,
    cal_offset    REAL,
    -- Meta
    active        INTEGER DEFAULT 1,
    created_at    REAL    DEFAULT (unixepoch()),
    last_seen     REAL
);

CREATE TABLE IF NOT EXISTS measurements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id    INTEGER NOT NULL REFERENCES stations(id),
    timestamp     REAL    NOT NULL,
    nf_dbfs       REAL    NOT NULL,
    nf_dbm        REAL,
    rms_dbfs      REAL,
    peak_dbfs     REAL,
    freq_hz       INTEGER,
    mode          TEXT,
    fft_size      INTEGER,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_meas_station ON measurements(station_id, timestamp);

CREATE TABLE IF NOT EXISTS psd_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id    INTEGER NOT NULL REFERENCES stations(id),
    timestamp     REAL    NOT NULL,
    fft_size      INTEGER,
    sample_rate   INTEGER,
    data_json     TEXT    NOT NULL
);
"""

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

# =============================================================================
#  AUTH
# =============================================================================
def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        # Compte admin par defaut (dev local) : ADMIN / admin
        existing = db.execute(
            "SELECT id FROM users WHERE callsign='ADMIN'").fetchone()
        if not existing:
            db.execute("""
                INSERT INTO users
                  (callsign, email, password_hash, confirmed, is_admin)
                VALUES (?, ?, ?, 1, 1)
            """, ("ADMIN", "admin@nfm.local", hash_password("admin")))
            print("  + Compte admin cree  -> indicatif: ADMIN  mdp: admin")
        else:
            db.execute(
                "UPDATE users SET confirmed=1, is_admin=1, password_hash=? "
                "WHERE callsign='ADMIN'",
                (hash_password("admin"),))
    print("  OK Base de donnees initialisee")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated

def send_confirmation_email(email, callsign, token):
    """Envoi email de confirmation. En dev : affiche le lien dans la console."""
    link = f"{BASE_URL}/confirm/{token}"
    print(f"\n  📧 Email de confirmation pour {callsign} <{email}>")
    print(f"  Lien : {link}\n")
    # En production, utiliser smtplib ou une API email (SendGrid, Mailgun...)
    try:
        import smtplib
        from email.mime.text import MIMEText
        smtp_host = os.environ.get("SMTP_HOST", "")
        if not smtp_host:
            return
        msg = MIMEText(f"""
Bonjour {callsign},

Confirmez votre inscription sur NFM Platform :
{link}

Ce lien expire dans 24 heures.

73 de F8AOF
""")
        msg["Subject"] = "Confirmation inscription NFM Platform"
        msg["From"]    = MAIL_FROM
        msg["To"]      = email
        with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", 587))) as s:
            if os.environ.get("SMTP_USER"):
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    except Exception as e:
        print(f"  ⚠ Envoi email échoué : {e}")

# =============================================================================
#  ROUTES — AUTH
# =============================================================================
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        callsign = request.form.get("callsign","").upper().strip()
        email    = request.form.get("email","").lower().strip()
        password = request.form.get("password","")
        confirm  = request.form.get("confirm","")

        errors = []
        if not callsign: errors.append("Indicatif requis")
        if not email:    errors.append("Email requis")
        if len(password) < 8: errors.append("Mot de passe : 8 caractères minimum")
        if password != confirm: errors.append("Mots de passe différents")

        if not errors:
            token  = secrets.token_urlsafe(32)
            expiry = time.time() + 86400  # 24h
            try:
                with get_db() as db:
                    db.execute("""INSERT INTO users
                        (callsign,email,password_hash,confirm_token,token_expiry)
                        VALUES (?,?,?,?,?)""",
                        (callsign, email, hash_password(password), token, expiry))
                send_confirmation_email(email, callsign, token)
                flash("Inscription réussie ! Vérifiez votre email pour confirmer.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError as e:
                if "callsign" in str(e):
                    errors.append("Cet indicatif est déjà utilisé")
                else:
                    errors.append("Cet email est déjà utilisé")

        for e in errors:
            flash(e, "error")

    return render_template("register.html")

@app.route("/confirm/<token>")
def confirm_email(token):
    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE confirm_token=? AND token_expiry>?",
            (token, time.time())).fetchone()
        if not user:
            flash("Lien invalide ou expiré.", "error")
            return redirect(url_for("login"))
        db.execute(
            "UPDATE users SET confirmed=1, confirm_token=NULL WHERE id=?",
            (user["id"],))
    flash("Email confirmé ! Vous pouvez vous connecter.", "success")
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("identifier","").strip()
        password   = request.form.get("password","")
        with get_db() as db:
            user = db.execute(
                """SELECT * FROM users WHERE
                   (callsign=? OR email=?) AND password_hash=?""",
                (identifier.upper(), identifier.lower(),
                 hash_password(password))).fetchone()
        if not user:
            flash("Identifiants incorrects.", "error")
        elif not user["confirmed"]:
            flash("Email non confirmé. Vérifiez votre boîte mail.", "error")
        else:
            session.permanent = True
            session["user_id"]   = user["id"]
            session["callsign"]  = user["callsign"]
            session["is_admin"]  = user["is_admin"]
            with get_db() as db:
                db.execute("UPDATE users SET last_login=? WHERE id=?",
                           (time.time(), user["id"]))
            return redirect(url_for("dashboard"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# =============================================================================
#  ROUTES — DASHBOARD
# =============================================================================
@app.route("/dashboard")
@login_required
def dashboard():
    with get_db() as db:
        stations = db.execute(
            "SELECT * FROM stations WHERE user_id=? ORDER BY name",
            (session["user_id"],)).fetchall()
        # Stats rapides
        stats = db.execute("""
            SELECT s.id, s.name,
                   COUNT(m.id) as n_meas,
                   MIN(m.nf_dbfs) as nf_min,
                   MAX(m.timestamp) as last_meas
            FROM stations s
            LEFT JOIN measurements m ON m.station_id=s.id
            WHERE s.user_id=?
            GROUP BY s.id""",
            (session["user_id"],)).fetchall()
    stats_map = {r["id"]: r for r in stats}
    # Stations en ligne
    online = set(connected_agents.keys())
    return render_template("dashboard.html",
                           stations=stations,
                           stats_map=stats_map,
                           online=online)

# =============================================================================
#  ROUTES — STATIONS
# =============================================================================
@app.route("/station/new", methods=["GET","POST"])
@login_required
def station_new():
    if request.method == "POST":
        data = request.form
        with get_db() as db:
            db.execute("""INSERT INTO stations
                (user_id, name, description,
                 audio_device_name, audio_sample_rate, audio_bits,
                 rig_brand, rig_model, rig_hamlib_id,
                 rig_port, rig_baud, rig_ptt_type,
                 fft_size, fft_window, fft_avg, percentile)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session["user_id"],
                 data.get("name","Station"), data.get("description",""),
                 data.get("audio_device_name",""),
                 int(data.get("audio_sample_rate",48000)),
                 int(data.get("audio_bits",24)),
                 data.get("rig_brand",""), data.get("rig_model",""),
                 int(data.get("rig_hamlib_id",0) or 0),
                 data.get("rig_port",""), int(data.get("rig_baud",9600)),
                 data.get("rig_ptt_type","RTS"),
                 int(data.get("fft_size",4096)),
                 data.get("fft_window","hann"),
                 int(data.get("fft_avg",32)),
                 int(data.get("percentile",10))))
        flash("Station créée.", "success")
        return redirect(url_for("dashboard"))
    return render_template("station_edit.html", station=None, hamlib_rigs=HAMLIB_RIGS)

@app.route("/station/<int:sid>/edit", methods=["GET","POST"])
@login_required
def station_edit(sid):
    with get_db() as db:
        station = db.execute(
            "SELECT * FROM stations WHERE id=? AND user_id=?",
            (sid, session["user_id"])).fetchone()
    if not station:
        flash("Station introuvable.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        data = request.form
        with get_db() as db:
            db.execute("""UPDATE stations SET
                name=?, description=?,
                audio_device_name=?, audio_sample_rate=?, audio_bits=?,
                rig_brand=?, rig_model=?, rig_hamlib_id=?,
                rig_port=?, rig_baud=?, rig_ptt_type=?,
                fft_size=?, fft_window=?, fft_avg=?, percentile=?,
                cal_offset=?
                WHERE id=? AND user_id=?""",
                (data.get("name"), data.get("description",""),
                 data.get("audio_device_name",""),
                 int(data.get("audio_sample_rate",48000)),
                 int(data.get("audio_bits",24)),
                 data.get("rig_brand",""), data.get("rig_model",""),
                 int(data.get("rig_hamlib_id",0) or 0),
                 data.get("rig_port",""), int(data.get("rig_baud",9600)),
                 data.get("rig_ptt_type","RTS"),
                 int(data.get("fft_size",4096)),
                 data.get("fft_window","hann"),
                 int(data.get("fft_avg",32)),
                 int(data.get("percentile",10)),
                 float(data.get("cal_offset") or 0) or None,
                 sid, session["user_id"]))
        flash("Station mise à jour.", "success")
        return redirect(url_for("dashboard"))

    return render_template("station_edit.html",
                           station=station, hamlib_rigs=HAMLIB_RIGS)

@app.route("/station/<int:sid>/delete", methods=["POST"])
@login_required
def station_delete(sid):
    with get_db() as db:
        db.execute("DELETE FROM stations WHERE id=? AND user_id=?",
                   (sid, session["user_id"]))
    flash("Station supprimée.", "success")
    return redirect(url_for("dashboard"))

@app.route("/station/<int:sid>/live")
@login_required
def station_live(sid):
    with get_db() as db:
        station = db.execute(
            "SELECT s.*, u.callsign FROM stations s JOIN users u ON u.id=s.user_id"
            " WHERE s.id=?", (sid,)).fetchone()
    if not station:
        flash("Station introuvable.", "error")
        return redirect(url_for("dashboard"))
    online = sid in connected_agents
    return render_template("live.html", station=station, online=online)

@app.route("/station/<int:sid>/history")
@login_required
def station_history(sid):
    hours = int(request.args.get("hours", 24))
    since = time.time() - hours * 3600
    with get_db() as db:
        station = db.execute(
            "SELECT * FROM stations WHERE id=? AND user_id=?",
            (sid, session["user_id"])).fetchone()
        meas = db.execute("""
            SELECT timestamp, nf_dbfs, nf_dbm, rms_dbfs, freq_hz, mode
            FROM measurements WHERE station_id=? AND timestamp>?
            ORDER BY timestamp""",
            (sid, since)).fetchall()
    return render_template("history.html", station=station,
                           measurements=meas, hours=hours)

# =============================================================================
#  API JSON
# =============================================================================
@app.route("/api/station/<int:sid>/data")
@login_required
def api_station_data(sid):
    hours = float(request.args.get("hours", 1))
    since = time.time() - hours * 3600
    with get_db() as db:
        meas = db.execute("""
            SELECT timestamp, nf_dbfs, nf_dbm, rms_dbfs, freq_hz
            FROM measurements WHERE station_id=? AND timestamp>?
            ORDER BY timestamp""",
            (sid, since)).fetchall()
    return jsonify([dict(r) for r in meas])

@app.route("/api/station/<int:sid>/config")
def api_station_config(sid):
    """Config retournée à l'agent local."""
    token = request.headers.get("X-Agent-Token","")
    with get_db() as db:
        station = db.execute(
            "SELECT s.*, u.callsign FROM stations s JOIN users u ON u.id=s.user_id"
            " WHERE s.id=?", (sid,)).fetchone()
    if not station:
        return jsonify({"error":"not found"}), 404
    return jsonify(dict(station))

@app.route("/api/hamlib/search")
def api_hamlib_search():
    q = request.args.get("q","").lower()
    results = [r for r in HAMLIB_RIGS
               if q in r["model"].lower() or q in r["brand"].lower()
               or q in str(r["id"])]
    return jsonify(results[:30])

# =============================================================================
#  WEBSOCKET — Agent ↔ Serveur ↔ Navigateur
# =============================================================================
@socketio.on("agent_connect")
def on_agent_connect(data):
    """L'agent local s'enregistre."""
    sid_val  = data.get("station_id")
    token    = data.get("token","")
    # Vérification token simple (à renforcer en prod)
    with get_db() as db:
        station = db.execute(
            "SELECT * FROM stations WHERE id=?", (sid_val,)).fetchone()
    if not station:
        emit("error", {"msg": "Station inconnue"})
        return

    connected_agents[sid_val] = {
        "socket_id": request.sid,
        "last_data":  None,
        "connected_at": time.time(),
        "station_name": station["name"],
        "callsign": ""
    }
    join_room(f"station_{sid_val}")
    emit("agent_ok", {"station_id": sid_val, "name": station["name"]})
    # Notifier les navigateurs qui regardent cette station
    socketio.emit("station_online",
                  {"station_id": sid_val, "name": station["name"]},
                  room=f"watch_{sid_val}")
    with get_db() as db:
        db.execute("UPDATE stations SET last_seen=? WHERE id=?",
                   (time.time(), sid_val))
    print(f"  ✓ Agent connecté : station {sid_val} — {station['name']}")

@socketio.on("agent_disconnect")
def on_agent_disconnect():
    """Nettoyage quand un agent se déconnecte."""
    for sid_val, info in list(connected_agents.items()):
        if info["socket_id"] == request.sid:
            del connected_agents[sid_val]
            socketio.emit("station_offline",
                          {"station_id": sid_val},
                          room=f"watch_{sid_val}")
            print(f"  Agent déconnecté : station {sid_val}")
            break

@socketio.on("disconnect")
def on_disconnect():
    on_agent_disconnect()

@socketio.on("measurement")
def on_measurement(data):
    """Mesure reçue de l'agent → sauvegarde + broadcast."""
    sid_val = data.get("station_id")
    if sid_val not in connected_agents:
        return

    nf    = data.get("nf_dbfs")
    rms   = data.get("rms_dbfs")
    peak  = data.get("peak_dbfs")
    freq  = data.get("freq_hz")
    mode  = data.get("mode")
    t     = data.get("timestamp", time.time())

    # Calcul dBm si calibration connue
    nf_dbm = None
    with get_db() as db:
        station = db.execute(
            "SELECT cal_offset FROM stations WHERE id=?", (sid_val,)).fetchone()
        if station and station["cal_offset"]:
            nf_dbm = nf + station["cal_offset"]

        # Sauvegarde en DB (1 point toutes les 5s max)
        last = connected_agents[sid_val].get("last_saved", 0)
        if t - last >= 5:
            db.execute("""INSERT INTO measurements
                (station_id,timestamp,nf_dbfs,nf_dbm,rms_dbfs,peak_dbfs,freq_hz,mode)
                VALUES (?,?,?,?,?,?,?,?)""",
                (sid_val, t, nf, nf_dbm, rms, peak, freq, mode))
            connected_agents[sid_val]["last_saved"] = t

    connected_agents[sid_val]["last_data"] = data

    # Broadcast aux navigateurs qui regardent
    socketio.emit("live_data", {
        "station_id": sid_val,
        "timestamp":  t,
        "nf_dbfs":    nf,
        "nf_dbm":     nf_dbm,
        "rms_dbfs":   rms,
        "peak_dbfs":  peak,
        "freq_hz":    freq,
        "mode":       mode,
    }, room=f"watch_{sid_val}")

@socketio.on("psd_data")
def on_psd_data(data):
    """PSD spectrum de l'agent → broadcast (pas sauvegardé à chaque trame)."""
    sid_val = data.get("station_id")
    if sid_val not in connected_agents:
        return
    socketio.emit("live_psd", data, room=f"watch_{sid_val}")

@socketio.on("watch_station")
def on_watch_station(data):
    """Un navigateur s'abonne à une station."""
    sid_val = data.get("station_id")
    join_room(f"watch_{sid_val}")
    # Envoyer immédiatement la dernière donnée si disponible
    if sid_val in connected_agents:
        last = connected_agents[sid_val].get("last_data")
        if last:
            emit("live_data", last)
        emit("station_online", {"station_id": sid_val})
    else:
        emit("station_offline", {"station_id": sid_val})

@socketio.on("cat_command")
def on_cat_command(data):
    """Commande CAT depuis le navigateur → relayée à l'agent."""
    sid_val = data.get("station_id")
    if sid_val in connected_agents:
        socketio.emit("cat_command", data,
                      room=f"station_{sid_val}")

# =============================================================================
#  BASE HAMLIB — transceivers courants + recherche
# =============================================================================
HAMLIB_RIGS = [
    # ── ICOM ──
    {"id":3,"brand":"Icom","model":"IC-706","protocols":["CI-V"],"default_baud":9600},
    {"id":3021,"brand":"Icom","model":"IC-706MkIIG / MkIIH","protocols":["CI-V"],"default_baud":9600},
    {"id":373,"brand":"Icom","model":"IC-7300","protocols":["CI-V USB"],"default_baud":115200},
    {"id":3073,"brand":"Icom","model":"IC-7610","protocols":["CI-V USB"],"default_baud":115200},
    {"id":375,"brand":"Icom","model":"IC-7100","protocols":["CI-V USB"],"default_baud":115200},
    {"id":370,"brand":"Icom","model":"IC-7200","protocols":["CI-V USB"],"default_baud":115200},
    {"id":376,"brand":"Icom","model":"IC-7300","protocols":["CI-V"],"default_baud":19200},
    {"id":377,"brand":"Icom","model":"IC-9700","protocols":["CI-V USB"],"default_baud":115200},
    {"id":378,"brand":"Icom","model":"IC-705","protocols":["CI-V USB"],"default_baud":115200},
    {"id":371,"brand":"Icom","model":"IC-7600","protocols":["CI-V"],"default_baud":19200},
    {"id":361,"brand":"Icom","model":"IC-746","protocols":["CI-V"],"default_baud":9600},
    {"id":341,"brand":"Icom","model":"IC-7400","protocols":["CI-V"],"default_baud":9600},
    # ── YAESU ──
    {"id":1035,"brand":"Yaesu","model":"FT-991A","protocols":["CAT"],"default_baud":38400},
    {"id":1034,"brand":"Yaesu","model":"FT-991","protocols":["CAT"],"default_baud":38400},
    {"id":1021,"brand":"Yaesu","model":"FT-817","protocols":["CAT"],"default_baud":9600},
    {"id":1021,"brand":"Yaesu","model":"FT-818","protocols":["CAT"],"default_baud":9600},
    {"id":1039,"brand":"Yaesu","model":"FT-DX10","protocols":["CAT"],"default_baud":38400},
    {"id":1040,"brand":"Yaesu","model":"FT-DX101D","protocols":["CAT"],"default_baud":38400},
    {"id":1033,"brand":"Yaesu","model":"FT-857D","protocols":["CAT"],"default_baud":9600},
    {"id":1020,"brand":"Yaesu","model":"FT-897D","protocols":["CAT"],"default_baud":9600},
    {"id":1013,"brand":"Yaesu","model":"FT-100D","protocols":["CAT"],"default_baud":4800},
    # ── KENWOOD ──
    {"id":229,"brand":"Kenwood","model":"TS-590SG","protocols":["CAT"],"default_baud":115200},
    {"id":243,"brand":"Kenwood","model":"TS-890S","protocols":["CAT"],"default_baud":115200},
    {"id":228,"brand":"Kenwood","model":"TS-590S","protocols":["CAT"],"default_baud":115200},
    {"id":234,"brand":"Kenwood","model":"TS-2000","protocols":["CAT"],"default_baud":9600},
    {"id":235,"brand":"Kenwood","model":"TS-480HX","protocols":["CAT"],"default_baud":9600},
    {"id":244,"brand":"Kenwood","model":"TS-990S","protocols":["CAT"],"default_baud":57600},
    # ── SDR ──
    {"id":1,"brand":"SDR","model":"RTL-SDR (lecture seule)","protocols":["USB"],"default_baud":0},
    {"id":2,"brand":"SDR","model":"HackRF One","protocols":["USB"],"default_baud":0},
    {"id":2,"brand":"SDR","model":"LimeSDR","protocols":["USB"],"default_baud":0},
    # ── ELECRAFT ──
    {"id":2029,"brand":"Elecraft","model":"K3","protocols":["CAT"],"default_baud":38400},
    {"id":2034,"brand":"Elecraft","model":"KX3","protocols":["CAT"],"default_baud":38400},
    {"id":2042,"brand":"Elecraft","model":"K4","protocols":["CAT"],"default_baud":115200},
    # ── FlexRadio ──
    {"id":6001,"brand":"FlexRadio","model":"FLEX-6600","protocols":["SmartSDR"],"default_baud":0},
    # ── GENERIC ──
    {"id":2,"brand":"Générique","model":"Rig en réseau (rotctld)","protocols":["NET"],"default_baud":0},
]

# =============================================================================
#  ROUTES — GUIDE PROPAGATION
# =============================================================================
@app.route("/guide")
@app.route("/guide/<page>")
def guide(page="greyline"):
    pages = {
        "greyline":  {"title": "Ligne grise",           "icon": "◐"},
        "tropo":     {"title": "Propagation tropo",      "icon": "🌫"},
        "ionosphere":{"title": "Ionosphère & indices",   "icon": "☀"},
        "nfm-prop":  {"title": "NFM & propagation",      "icon": "📊"},
        "antenna":   {"title": "Antennes & bandes",      "icon": "📡"},
        "resources": {"title": "Ressources & outils",    "icon": "🔗"},
    }
    if page not in pages:
        page = "greyline"
    return render_template(f"guide/{page}.html",
                           current_page=page,
                           pages=pages)

# =============================================================================
#  ROUTES — ADMIN
# =============================================================================
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Accès administrateur requis.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

@app.route("/admin")
@login_required
@admin_required
def admin():
    with get_db() as db:
        users    = db.execute(
            "SELECT u.*, COUNT(s.id) as n_stations FROM users u "
            "LEFT JOIN stations s ON s.user_id=u.id "
            "GROUP BY u.id ORDER BY u.created_at DESC").fetchall()
        stations = db.execute(
            "SELECT s.*, u.callsign, COUNT(m.id) as n_meas "
            "FROM stations s JOIN users u ON u.id=s.user_id "
            "LEFT JOIN measurements m ON m.station_id=s.id "
            "GROUP BY s.id ORDER BY s.last_seen DESC NULLS LAST").fetchall()
        stats = db.execute("""
            SELECT
                (SELECT COUNT(*) FROM users)        as n_users,
                (SELECT COUNT(*) FROM users WHERE confirmed=1) as n_confirmed,
                (SELECT COUNT(*) FROM stations)     as n_stations,
                (SELECT COUNT(*) FROM measurements) as n_measurements
        """).fetchone()
    online = set(connected_agents.keys())
    return render_template("admin.html",
                           users=users, stations=stations,
                           stats=stats, online=online)

@app.route("/admin/user/<int:uid>/toggle-confirm", methods=["POST"])
@login_required
@admin_required
def admin_toggle_confirm(uid):
    with get_db() as db:
        user = db.execute("SELECT confirmed FROM users WHERE id=?", (uid,)).fetchone()
        if user:
            db.execute("UPDATE users SET confirmed=? WHERE id=?",
                       (0 if user["confirmed"] else 1, uid))
    return redirect(url_for("admin"))

@app.route("/admin/user/<int:uid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_delete_user(uid):
    with get_db() as db:
        db.execute("DELETE FROM measurements WHERE station_id IN "
                   "(SELECT id FROM stations WHERE user_id=?)", (uid,))
        db.execute("DELETE FROM stations WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    flash("Utilisateur supprimé.", "success")
    return redirect(url_for("admin"))

# =============================================================================
#  ROUTES — API AGENT (devices + CAT test)
# =============================================================================
@app.route("/api/agent/devices")
def api_agent_devices():
    """
    Retourne la liste des cartes son détectées côté agent.
    En prod, l'agent pousse cette info via WebSocket.
    Ici on retourne ce qui est disponible en mémoire.
    """
    devices = []
    for sid, info in connected_agents.items():
        if "devices" in info:
            devices = info["devices"]
            break
    return jsonify(devices)

@app.route("/api/agent/cat-test", methods=["POST"])
def api_agent_cat_test():
    """Demande à l'agent de tester la connexion CAT."""
    data = request.get_json()
    # Relayer à l'agent via WebSocket si connecté
    for sid, info in connected_agents.items():
        socketio.emit("cat_test_request", data,
                      room=f"station_{sid}")
        return jsonify({"ok": False, "error": "Réponse agent non encore reçue — relancez"})
    return jsonify({"ok": False, "error": "Aucun agent connecté"})

# WebSocket : l'agent pousse ses devices au démarrage
@socketio.on("agent_devices")
def on_agent_devices(data):
    sid_val = data.get("station_id")
    if sid_val in connected_agents:
        connected_agents[sid_val]["devices"] = data.get("devices", [])

# =============================================================================
#  ROUTES — UTILITAIRES
# =============================================================================
@app.route("/api/status")
def api_status():
    """Healthcheck + stats publiques."""
    with get_db() as db:
        n_users    = db.execute("SELECT COUNT(*) FROM users WHERE confirmed=1").fetchone()[0]
        n_stations = db.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    return jsonify({
        "status":    "ok",
        "version":   "1.0.0",
        "users":     n_users,
        "stations":  n_stations,
        "online":    len(connected_agents),
        "timestamp": time.time(),
    })

# ── Route calibration depuis live ──
@app.route("/api/station/<int:sid>/calibrate", methods=["POST"])
@login_required
def api_calibrate(sid):
    data = request.get_json()
    offset = data.get("offset")
    with get_db() as db:
        db.execute("UPDATE stations SET cal_offset=? WHERE id=? AND user_id=?",
                   (offset, sid, session["user_id"]))
    return jsonify({"ok": True, "offset": offset})

# ── Route CSV export ──
@app.route("/api/station/<int:sid>/data/csv")
@login_required
def api_data_csv(sid):
    hours = float(request.args.get("hours", 24))
    since = time.time() - hours * 3600
    with get_db() as db:
        meas = db.execute("""
            SELECT timestamp, nf_dbfs, nf_dbm, rms_dbfs, freq_hz, mode
            FROM measurements WHERE station_id=? AND timestamp>?
            ORDER BY timestamp""", (sid, since)).fetchall()
    import io
    buf = io.StringIO()
    buf.write("Timestamp_ISO,nf_dbfs_hz,nf_dbm_hz,rms_dbfs,freq_hz,mode\n")
    for m in meas:
        buf.write(f"{datetime.fromtimestamp(m['timestamp']).isoformat()},"
                  f"{m['nf_dbfs']:.3f},"
                  f"{m['nf_dbm']:.3f if m['nf_dbm'] else ''},"
                  f"{m['rms_dbfs']:.2f if m['rms_dbfs'] else ''},"
                  f"{m['freq_hz'] or ''},"
                  f"{m['mode'] or ''}\n")
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment;filename=nfm_{sid}.csv"})

# =============================================================================
#  LANCEMENT
# =============================================================================
if __name__ == "__main__":
    init_db()
    print(f"\n  NFM SaaS Platform — F8AOF")
    print(f"  → http://localhost:5000")
    print(f"  → http://localhost:5000/guide")
    print(f"  → http://localhost:5000/admin  (compte admin)\n")
    import os
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werzeug=True)
