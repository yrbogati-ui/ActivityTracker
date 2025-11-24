import os
import csv
from io import StringIO
from flask import (
    Flask, render_template, request, redirect, Response,
    jsonify, session
)
from datetime import datetime, date
from difflib import SequenceMatcher
from urllib.parse import urlparse
import psycopg2
import bcrypt
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_key_change_later")

# --------------------------------------------------------------------------
# API TOKEN (for activity agent ingestion)
# --------------------------------------------------------------------------
API_TOKEN = "xinfini-org-activitytracker-9082347908234"


def require_token(func):
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key") != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# --------------------------------------------------------------------------
# DB CONNECTION
# --------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing.")

def db_conn():
    parsed = urlparse(DATABASE_URL)
    return psycopg2.connect(
        database=parsed.path[1:],
        user=parsed.username,
        password=parsed.password,
        host=parsed.hostname,
        port=parsed.port
    )

# --------------------------------------------------------------------------
# ACCESS CONTROL
# --------------------------------------------------------------------------
def require_login(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        return func(*args, **kwargs)
    return wrapper

def require_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            return "Access denied — Admin only", 403
        return func(*args, **kwargs)
    return wrapper

# --------------------------------------------------------------------------
# COMPRESSION ENGINE
# --------------------------------------------------------------------------
IDLE_THRESHOLD_MINUTES = 3
MICROSECONDS_THRESHOLD = 30
MERGE_GAP_MINUTES = 5

def similar(a, b):
    return SequenceMatcher(None, a or "", b or "").ratio()

def _to_dt(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)

def compress_events(raw):
    events = []
    for r in raw:
        start = _to_dt(r[0])
        end = _to_dt(r[1])
        events.append({
            "start": start,
            "end": end,
            "app": r[2],
            "title": r[3],
            "project": r[4] or "Unassigned",
            "duration": (end - start).total_seconds()
        })

    events.sort(key=lambda x: x["start"])
    if not events:
        return []

    blocks = [events[0]]

    for ev in events[1:]:
        last = blocks[-1]
        gap = (ev["start"] - last["end"]).total_seconds() / 60
        same_proj = ev["project"] == last["project"]
        title_sim = similar(ev["title"], last["title"])

        if gap > IDLE_THRESHOLD_MINUTES:
            blocks.append({
                "start": last["end"],
                "end": ev["start"],
                "app": "Idle",
                "title": "Break",
                "project": "Break",
                "duration": (ev["start"] - last["end"]).total_seconds()
            })
            blocks.append(ev)
            continue

        if ev["duration"] < MICROSECONDS_THRESHOLD:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        if same_proj and gap <= MERGE_GAP_MINUTES:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        if title_sim > 0.55:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        blocks.append(ev)

    return blocks


# --------------------------------------------------------------------------
# LOGIN PAGE
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email")
    password = request.form.get("password")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash, role FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return render_template("login.html", error="Invalid email or password")

    user_id, pwd_hash, role = row

    if not bcrypt.checkpw(password.encode(), pwd_hash.encode()):
        return render_template("login.html", error="Invalid email or password")

    session["user_id"] = user_id
    session["role"] = role

    return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --------------------------------------------------------------------------
# API — LOGIN (for desktop agent)
# --------------------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json
    email = data.get("email")
    password = data.get("password")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Invalid email"}), 401

    user_id, pwd_hash = row

    if not bcrypt.checkpw(password.encode(), pwd_hash.encode()):
        return jsonify({"error": "Wrong password"}), 401

    return jsonify({"status": "ok", "user_id": user_id})


# --------------------------------------------------------------------------
# API — BATCH EVENT INGESTION
# --------------------------------------------------------------------------
@app.route("/api/events/batch", methods=["POST"])
@require_token
def api_events_batch():
    events = request.json.get("events", [])

    conn = db_conn()
    cur = conn.cursor()

    for ev in events:
        cur.execute("""
            INSERT INTO activity_events
                (user_id, start_time, end_time, app_name, window_title, project)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            ev["user_id"],
            ev["start_time"],
            ev["end_time"],
            ev["app_name"],
            ev["window_title"],
            ev.get("project"),
        ))

    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "count": len(events)})


# --------------------------------------------------------------------------
# MAIN DASHBOARD (USER ONLY)
# --------------------------------------------------------------------------
@app.route("/")
@require_login
def view_events():
    date_str = request.args.get("date", date.today().isoformat())

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
          AND user_id = %s
        ORDER BY start_time ASC
    """, (date_str, session["user_id"]))
    rows = cur.fetchall()
    conn.close()

    enriched = []
    for e in rows:
        start = _to_dt(e[1])
        end = _to_dt(e[2])
        enriched.append(e + ((end - start).total_seconds() / 3600,))

    assigned = [e for e in enriched if e[5] not in ("Unassigned", None)]
    unassigned = [e for e in enriched if e[5] in ("Unassigned", None)]

    return render_template(
        "events.html",
        assigned=assigned,
        unassigned=unassigned,
        date_str=date_str
    )


# --------------------------------------------------------------------------
# ADMIN USER MANAGEMENT
# --------------------------------------------------------------------------
@app.route("/admin/users")
@require_admin
def admin_users():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, email, role FROM users ORDER BY id ASC")
    rows = cur.fetchall()
    print("LOGIN DEBUG:", email, row)   # <--- ADD THIS
    conn.close()

    return render_template("admin_users.html", users=rows)


@app.route("/admin/users/add", methods=["POST"])
@require_admin
def admin_add_user():
    name = request.form.get("name")
    email = request.form.get("email")
    password = request.form.get("password")
    role = request.form.get("role", "user")

    pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (name, email, password_hash, role)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (email) DO NOTHING
    """, (name, email, pwd_hash, role))

    conn.commit()
    conn.close()

    return redirect("/admin/users")

@app.route("/version")
def version():
    return "VERSION 999"


# --------------------------------------------------------------------------
# CSV EXPORTS (USER ONLY)
# --------------------------------------------------------------------------
@app.route("/export_csv/today")
@require_login
def export_today():
    today = date.today().isoformat()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
          AND user_id = %s
        ORDER BY start_time ASC
    """, (today, session["user_id"]))
    raw = cur.fetchall()
    conn.close()

    compressed = compress_events(raw)

    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Start", "End", "Project", "App", "Window", "Duration (min)"])

    for e in compressed:
        w.writerow([
            e["start"], e["end"], e["project"],
            e["app"], e["title"], round(e["duration"] / 60, 2)
        ])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=today.csv"}
    )


# --------------------------------------------------------------------------
# RUN
# --------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
