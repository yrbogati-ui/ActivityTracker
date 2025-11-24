import os
import csv
from io import StringIO
from datetime import datetime, date
from difflib import SequenceMatcher
from functools import wraps
from urllib.parse import urlparse

import psycopg2
import bcrypt
from flask import (
    Flask, render_template, request, redirect, Response,
    jsonify, session
)

# --------------------------------------------------------------------------
# APP + SECRET
# --------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_key_change_later")

# --------------------------------------------------------------------------
# API TOKEN (for desktop agent ingestion — NOT per-user)
# --------------------------------------------------------------------------
API_TOKEN = "xinfini-org-activitytracker-9082347908234"


def require_token(func):
    """Simple header-based API token for the agent."""
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
# ACCESS CONTROL HELPERS
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
    raw rows: (start_time, end_time, app_name, window_title, project)
    returns list of dicts with merged/cleaned blocks.
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
        same_proj = (ev["project"] == last["project"])
        title_sim = similar(ev["title"], last["title"])

        # Insert break if there's a big gap
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

        # Micro events (< 30s)
        if ev["duration"] < MICROSECONDS_THRESHOLD:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        # Merge same project with short gaps
        if same_proj and gap <= MERGE_GAP_MINUTES:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        # Merge by similar title
        if title_sim > 0.55:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        blocks.append(ev)

    return blocks


def summarize_today_compressed_user(user_id, date_str):
    """Compressed project summary for a single user + date."""
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

    compressed = compress_events(raw)
    summary = {}

    for e in compressed:
        proj = e["project"] or "Unassigned"
        summary[proj] = summary.get(proj, 0) + e["duration"]

    # convert to hours with 2 decimals
    for proj in summary:
        summary[proj] = round(summary[proj] / 3600, 2)

    return summary


# --------------------------------------------------------------------------
# PROJECTS HELPERS
# --------------------------------------------------------------------------
def get_projects():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM projects ORDER BY name ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


def add_project(name):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO projects (name)
        VALUES (%s)
        ON CONFLICT (name) DO NOTHING
    """, (name,))
    conn.commit()
    conn.close()


def delete_project(pid):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE id = %s", (pid,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# EVENT UPDATE / UNDO HELPERS (user-scoped)
# --------------------------------------------------------------------------
def update_project_single(event_id, project, user_id):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE activity_events SET project = %s WHERE id = %s AND user_id = %s",
        (project, event_id, user_id)
    )
    conn.commit()
    conn.close()


def update_project_bulk(ids, project, user_id):
    if not ids:
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.executemany(
        "UPDATE activity_events SET project = %s WHERE id = %s AND user_id = %s",
        [(project, i, user_id) for i in ids]
    )
    conn.commit()
    conn.close()


def undo_single(event_id, user_id):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE activity_events SET project = 'Unassigned' WHERE id = %s AND user_id = %s",
        (event_id, user_id)
    )
    conn.commit()
    conn.close()


def undo_bulk(ids, user_id):
    if not ids:
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.executemany(
        "UPDATE activity_events SET project = 'Unassigned' WHERE id = %s AND user_id = %s",
        [(i, user_id) for i in ids]
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# LOGIN / LOGOUT
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email")
    password = request.form.get("password")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, password_hash, role FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return render_template("login.html", error="Invalid email or password")

    user_id, name, pwd_hash, role = row

    # Normalize DB hash → always clean str
    if isinstance(pwd_hash, bytes):
        pwd_hash = pwd_hash.decode("utf-8", errors="ignore")
    pwd_hash = pwd_hash.strip()

    if not bcrypt.checkpw(password.encode("utf-8"), pwd_hash.encode("utf-8")):
        return render_template("login.html", error="Invalid email or password")

    # login success
    session["user_id"] = user_id
    session["user_name"] = name
    session["role"] = role

    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --------------------------------------------------------------------------
# API LOGIN (for desktop agent → fetch user_id)
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

    if isinstance(pwd_hash, bytes):
        pwd_hash = pwd_hash.decode("utf-8", errors="ignore")
    pwd_hash = pwd_hash.strip()

    if not bcrypt.checkpw(password.encode("utf-8"), pwd_hash.encode("utf-8")):
        return jsonify({"error": "Wrong password"}), 401

    return jsonify({"status": "ok", "user_id": user_id})


# --------------------------------------------------------------------------
# API — BATCH EVENT INGESTION (AGENT → SERVER)
# --------------------------------------------------------------------------
@app.route("/api/events/batch", methods=["POST"])
@require_token
def api_events_batch():
    payload = request.json or {}
    events = payload.get("events", [])

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
# MAIN DASHBOARD (PER-USER)
# --------------------------------------------------------------------------
@app.route("/")
@require_login
def view_events():
    date_str = request.args.get("date", date.today().isoformat())
    user_id = session["user_id"]

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
        duration_hours = (end - start).total_seconds() / 3600.0
        enriched.append(e + (duration_hours,))

    assigned = [e for e in enriched if e[5] not in ("Unassigned", None)]
    unassigned = [e for e in enriched if e[5] in ("Unassigned", None)]

    summary = summarize_today_compressed_user(user_id, date_str)
    projects = get_projects()

    return render_template(
        "events.html",
        assigned=assigned,
        unassigned=unassigned,
        summary=summary,
        projects=projects,
        date_str=date_str,
        user_name=session.get("user_name", "")
    )


# --------------------------------------------------------------------------
# ADMIN — USER MANAGEMENT
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

    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode()

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
# PROJECT MANAGEMENT (OPTIONAL SIMPLE ADMIN PAGE)
# --------------------------------------------------------------------------
@app.route("/projects")
@require_admin
def project_page():
    return render_template("projects.html", projects=get_projects())


@app.route("/projects/add", methods=["POST"])
@require_admin
def project_add_route():
    name = request.form.get("project_name")
    if name:
        add_project(name)
    return redirect("/projects")


@app.route("/projects/delete/<pid>")
@require_admin
def project_delete_route(pid):
    delete_project(pid)
    return redirect("/projects")


# --------------------------------------------------------------------------
# EVENT ASSIGN / UNDO ROUTES
# --------------------------------------------------------------------------
@app.route("/update", methods=["POST"])
@require_login
def update_event():
    event_id = request.form.get("event_id")
    project_id = request.form.get("project_id")
    user_id = session["user_id"]

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
    proj = cur.fetchone()
    conn.close()

    if proj:
        update_project_single(event_id, proj[0], user_id)

    return redirect("/")


@app.route("/undo", methods=["POST"])
@require_login
def undo_event():
    event_id = request.form.get("event_id")
    user_id = session["user_id"]
    undo_single(event_id, user_id)
    return redirect("/")


@app.route("/bulk_update", methods=["POST"])
@require_login
def bulk_update_route():
    project_id = request.form.get("bulk_project_id")
    raw = request.form.get("event_ids")
    user_id = session["user_id"]

    ids = []
    if raw:
        try:
            ids = list(map(int, eval(raw)))
        except Exception:
            ids = []

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
    proj = cur.fetchone()
    conn.close()

    if proj and ids:
        update_project_bulk(ids, proj[0], user_id)

    return redirect("/")


@app.route("/bulk_undo", methods=["POST"])
@require_login
def bulk_undo_route():
    raw = request.form.get("event_ids")
    user_id = session["user_id"]

    ids = []
    if raw:
        try:
            ids = list(map(int, eval(raw)))
        except Exception:
            ids = []

    if ids:
        undo_bulk(ids, user_id)

    return redirect("/")


# --------------------------------------------------------------------------
# CSV EXPORTS (PER USER)
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
            round(e["duration"] / 60, 2)
        ])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=today_compressed.csv"}
    )


# --------------------------------------------------------------------------
# SIMPLE VERSION ENDPOINT (you already used this for debugging)
# --------------------------------------------------------------------------
@app.route("/version")
def version():
    return "VERSION 999"


# --------------------------------------------------------------------------
# RUN (local dev)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
