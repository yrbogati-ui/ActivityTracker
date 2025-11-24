import os
import csv
from io import StringIO
from flask import Flask, render_template, request, redirect, Response, jsonify
from datetime import datetime, date
from difflib import SequenceMatcher
import psycopg2
from urllib.parse import urlparse
import bcrypt

app = Flask(__name__)

# --------------------------------------------------------------------------
# API TOKEN (for agent → server authentication)
# --------------------------------------------------------------------------
API_TOKEN = "xinfini-org-activitytracker-9082347908234"


def require_token(func):
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-API-Key")
        if token != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# --------------------------------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing!")

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
# CONFIG
# --------------------------------------------------------------------------
IDLE_THRESHOLD_MINUTES = 3
MICROSECONDS_THRESHOLD = 30
MERGE_GAP_MINUTES = 5


# --------------------------------------------------------------------------
# UTILS
# --------------------------------------------------------------------------
def similar(a, b):
    return SequenceMatcher(None, a or "", b or "").ratio()

def _to_dt(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


# --------------------------------------------------------------------------
# PROJECTS
# --------------------------------------------------------------------------
def get_projects():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM projects ORDER BY name ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------------
# COMPRESSION ENGINE
# --------------------------------------------------------------------------
def compress_events(raw):
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

        if ev["duration"] < MICROSECONDS_THRESHOLD:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        if ev["project"] == last["project"] and gap <= MERGE_GAP_MINUTES:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        if similar(ev["title"], last["title"]) > 0.55:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        blocks.append(ev)

    return blocks


# --------------------------------------------------------------------------
# TODAY SUMMARY
# --------------------------------------------------------------------------
def summarize_today_compressed():
    today = date.today().isoformat()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
        ORDER BY start_time ASC
    """, (today,))
    raw = cur.fetchall()
    conn.close()

    compressed = compress_events(raw)
    summary = {}

    for e in compressed:
        proj = e["project"]
        summary[proj] = summary.get(proj, 0) + e["duration"]

    for proj in summary:
        summary[proj] = round(summary[proj] / 3600, 2)

    return summary


# --------------------------------------------------------------------------
# LOGIN API (AGENT LOGIN)
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

    if not bcrypt.checkpw(password.encode("utf-8"), pwd_hash.encode("utf-8")):
        return jsonify({"error": "Wrong password"}), 401

    return jsonify({"status": "ok", "user_id": user_id})


# --------------------------------------------------------------------------
# BATCH EVENT INGESTION (agent → server)
# --------------------------------------------------------------------------
@app.route("/api/events/batch", methods=["POST"])
@require_token
def api_events_batch():
    events = request.json.get("events", [])

    conn = db_conn()
    cur = conn.cursor()

    for ev in events:
        cur.execute("""
            INSERT INTO activity_events (user_id, start_time, end_time, app_name, window_title, project)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            ev["user_id"],
            ev["start_time"],
            ev["end_time"],
            ev["app_name"],
            ev["window_title"],
            ev.get("project", None)
        ))

    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "count": len(events)})


# --------------------------------------------------------------------------
# MAIN UI VIEW
# --------------------------------------------------------------------------
@app.route("/")
def view_events():
    date_str = request.args.get("date", date.today().isoformat())

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
        ORDER BY start_time ASC
    """, (date_str,))
    rows = cur.fetchall()
    conn.close()

    enriched = []
    for e in rows:
        start = _to_dt(e[1])
        end = _to_dt(e[2])
        duration = (end - start).total_seconds() / 3600
        enriched.append(e + (duration,))

    assigned = [e for e in enriched if e[5] not in ("Unassigned", None)]
    unassigned = [e for e in enriched if e[5] in ("Unassigned", None)]

    return render_template(
        "events.html",
        assigned=assigned,
        unassigned=unassigned,
        summary=summarize_today_compressed(),
        projects=get_projects(),
        date_str=date_str
    )


# --------------------------------------------------------------------------
# UPDATE / UNDO
# --------------------------------------------------------------------------
@app.route("/update", methods=["POST"])
def update():
    event_id = request.form.get("event_id")
    project_id = request.form.get("project_id")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
    proj = cur.fetchone()

    if proj:
        cur.execute("UPDATE activity_events SET project = %s WHERE id = %s", (proj[0], event_id))
        conn.commit()

    conn.close()
    return redirect("/")


@app.route("/undo", methods=["POST"])
def undo():
    event_id = request.form.get("event_id")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE activity_events SET project = 'Unassigned' WHERE id = %s", (event_id,))
    conn.commit()
    conn.close()

    return redirect("/")


# --------------------------------------------------------------------------
# BULK UPDATE
# --------------------------------------------------------------------------
@app.route("/bulk_update", methods=["POST"])
def bulk_update():
    project_id = request.form.get("bulk_project_id")
    ids = eval(request.form.get("event_ids", "[]"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
    proj = cur.fetchone()

    if proj:
        cur.executemany(
            "UPDATE activity_events SET project = %s WHERE id = %s",
            [(proj[0], i) for i in ids]
        )
        conn.commit()

    conn.close()
    return redirect("/")


@app.route("/bulk_undo", methods=["POST"])
def bulk_undo():
    ids = eval(request.form.get("event_ids", "[]"))

    conn = db_conn()
    cur = conn.cursor()
    cur.executemany(
        "UPDATE activity_events SET project = 'Unassigned' WHERE id = %s",
        [(i,) for i in ids]
    )
    conn.commit()
    conn.close()

    return redirect("/")


# --------------------------------------------------------------------------
# PROJECT MANAGEMENT
# --------------------------------------------------------------------------
@app.route("/projects")
def project_page():
    return render_template("projects.html", projects=get_projects())


@app.route("/add_project", methods=["POST"])
def add_project():
    name = request.form.get("project_name")
    if name:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO projects (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        conn.commit()
        conn.close()
    return redirect("/projects")


@app.route("/delete_project/<pid>")
def delete_project(pid):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM projects WHERE id = %s", (pid,))
    conn.commit()
    conn.close()
    return redirect("/projects")


# --------------------------------------------------------------------------
# CSV EXPORTS
# --------------------------------------------------------------------------
@app.route("/export_csv/today")
def export_today():
    today = date.today().isoformat()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
        ORDER BY start_time ASC
    """, (today,))
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
            round(e["duration"] / 60, 2),
        ])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=today_compressed.csv"}
    )


@app.route("/export_csv/summary")
def export_summary():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT project,
               SUM(EXTRACT(EPOCH FROM (end_time - start_time)))/3600.0 AS hours
        FROM activity_events
        GROUP BY project
        ORDER BY hours DESC
    """)
    rows = cur.fetchall()
    conn.close()

    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Project", "Total Hours"])

    for r in rows:
        w.writerow([r[0], round(r[1] or 0, 2)])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=project_summary.csv"}
    )

@app.route("/admin/users")
def admin_users():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, email, role FROM users ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    return render_template("admin_users.html", users=rows)

@app.route("/admin/users/add", methods=["POST"])
def admin_add_user():
    name = request.form.get("name")
    email = request.form.get("email")
    password = request.form.get("password")
    role = request.form.get("role", "user")

    if not (name and email and password):
        return "Missing fields", 400

    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

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
# TIMELINE VIEW
# --------------------------------------------------------------------------
@app.route("/timeline")
def timeline():
    date_str = request.args.get("date", date.today().isoformat())

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT start_time, end_time, app_name, window_title, project
        FROM activity_events
        WHERE DATE(start_time) = %s
        ORDER BY start_time ASC
    """, (date_str,))
    raw = cur.fetchall()
    conn.close()

    compressed = compress_events(raw)

    blocks = [
        {
            "start": e["start"].isoformat(),
            "end": e["end"].isoformat(),
            "project": e["project"],
            "app": e["app"],
            "title": e["title"],
            "duration": round(e["duration"] / 60, 2)
        }
        for e in compressed
    ]

    return render_template("timeline.html", blocks=blocks, date_str=date_str)


# --------------------------------------------------------------------------
# RUN
# --------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
