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
# API TOKEN (for desktop agent ingestion)
# --------------------------------------------------------------------------
API_TOKEN = "xinfini-org-activitytracker-9082347908234"


def require_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key") != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)
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
    """
    raw: rows of (start_time, end_time, app_name, window_title, project)
    returns list of dicts with merged blocks
    """
    events = []
    for r in raw:
        start = _to_dt(r[0])
        end = _to_dt(r[1])
        app = r[2]
        title = r[3]
        proj = r[4] or "Unassigned"

        events.append({
            "start": start,
            "end": end,
            "app": app,
            "title": title,
            "project": proj,
            "duration": (end - start).total_seconds()
        })

    events.sort(key=lambda x: x["start"])
    if not events:
        return []

    blocks = [events[0]]

    for ev in events[1:]:
        last = blocks[-1]
        gap = (ev["start"] - last["end"]).total_seconds() / 60
        same_project = (ev["project"] == last["project"])
        title_similarity = similar(ev["title"], last["title"])

        # Insert break if large gap
        if gap > IDLE_THRESHOLD_MINUTES:
            break_block = {
                "start": last["end"],
                "end": ev["start"],
                "app": "Idle",
                "title": "Break",
                "project": "Break",
                "duration": (ev["start"] - last["end"]).total_seconds()
            }
            blocks.append(break_block)
            blocks.append(ev)
            continue

        # Micro-events (< 30 seconds)
        if ev["duration"] < MICROSECONDS_THRESHOLD:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        # Merge same project if small gap
        if same_project and gap <= MERGE_GAP_MINUTES:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        # Merge similar titles
        if title_similarity > 0.55:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        blocks.append(ev)

    return blocks


def get_projects():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM projects ORDER BY name ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_compressed_for_user_date(user_id, date_str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
          AND user_id = %s
        ORDER BY start_time ASC
    """, (date_str, user_id))
    raw = cur.fetchall()
    conn.close()
    return compress_events(raw)


def summarize_by_project(blocks):
    summary = {}
    for e in blocks:
        proj = e["project"] or "Unassigned"
        summary[proj] = summary.get(proj, 0) + e["duration"]

    # seconds → hours
    for proj in summary:
        summary[proj] = round(summary[proj] / 3600.0, 2)

    return summary


# --------------------------------------------------------------------------
# LOGIN PAGES
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

    try:
        ok = bcrypt.checkpw(password.encode(), pwd_hash.encode())
    except AttributeError:
        # In case password_hash is already bytes
        ok = bcrypt.checkpw(password.encode(), pwd_hash)

    if not ok:
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
    data = request.json or {}
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

    try:
        ok = bcrypt.checkpw(password.encode(), pwd_hash.encode())
    except AttributeError:
        ok = bcrypt.checkpw(password.encode(), pwd_hash)

    if not ok:
        return jsonify({"error": "Wrong password"}), 401

    return jsonify({"status": "ok", "user_id": user_id})


# --------------------------------------------------------------------------
# API — BATCH EVENT INGESTION (agent → server)
# --------------------------------------------------------------------------
@app.route("/api/events/batch", methods=["POST"])
@require_token
def api_events_batch():
    data = request.json or {}
    events = data.get("events", [])

    if not isinstance(events, list):
        return jsonify({"error": "Invalid payload"}), 400

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
# MAIN DASHBOARD (per-user)
# --------------------------------------------------------------------------
@app.route("/")
@require_login
def view_events():
    date_str = request.args.get("date", date.today().isoformat())
    user_id = session["user_id"]

    # Raw events for table view
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
          AND user_id = %s
        ORDER BY start_time ASC
    """, (date_str, user_id))
    rows = cur.fetchall()
    conn.close()

    enriched = []
    for e in rows:
        start = _to_dt(e[1])
        end = _to_dt(e[2])
        hours = (end - start).total_seconds() / 3600.0
        enriched.append(e + (hours,))

    assigned = [e for e in enriched if e[5] not in ("Unassigned", None)]
    unassigned = [e for e in enriched if e[5] in ("Unassigned", None)]

    # Compressed events for summary + timeline
    compressed = get_compressed_for_user_date(user_id, date_str)
    summary = summarize_by_project(compressed)

    timeline_blocks = [
        {
            "start": e["start"].isoformat(),
            "end": e["end"].isoformat(),
            "project": e["project"],
            "app": e["app"],
            "title": e["title"],
            "duration_min": round(e["duration"] / 60.0, 1),
        }
        for e in compressed
    ]

    return render_template(
        "events.html",
        assigned=assigned,
        unassigned=unassigned,
        summary=summary,
        projects=get_projects(),
        date_str=date_str,
        timeline_blocks=timeline_blocks,
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


# --------------------------------------------------------------------------
# CSV EXPORT (per-user)
# --------------------------------------------------------------------------
@app.route("/export_csv/today")
@require_login
def export_today():
    today = date.today().isoformat()
    user_id = session["user_id"]

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
          AND user_id = %s
        ORDER BY start_time ASC
    """, (today, user_id))
    raw = cur.fetchall()
    conn.close()

    compressed = compress_events(raw)

    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Start", "End", "Project", "App", "Window", "Duration (min)"])

    for e in compressed:
        w.writerow([
            e["start"],
            e["end"],
            e["project"],
            e["app"],
            e["title"],
            round(e["duration"] / 60.0, 2),
        ])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=today.csv"}
    )


# --------------------------------------------------------------------------
# SIMPLE VERSION CHECK (for sanity)
# --------------------------------------------------------------------------
@app.route("/version")
def version():
    return "1001"


# --------------------------------------------------------------------------
# RUN LOCAL
# --------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
