import os
import csv
from io import StringIO
from flask import Flask, render_template, request, redirect, Response
from datetime import datetime, date
from difflib import SequenceMatcher

import psycopg2
from urllib.parse import urlparse

app = Flask(__name__)

# --------------------------------------------------------------------------
# DATABASE CONNECTION (Render style)
# --------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable missing.")

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
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)

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
        same_project = (ev["project"] == last["project"])
        title_similarity = similar(ev["title"], last["title"])

        # Insert break
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

        # Micro events (<30s)
        if ev["duration"] < MICROSECONDS_THRESHOLD:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        # Merge same project (<5m gap)
        if same_project and gap <= MERGE_GAP_MINUTES:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        # Merge by title similarity
        if title_similarity > 0.55:
            last["end"] = ev["end"]
            last["duration"] += ev["duration"]
            continue

        blocks.append(ev)

    return blocks

# --------------------------------------------------------------------------
# SUMMARY (TODAY)
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
        proj = e["project"] or "Unassigned"
        summary[proj] = summary.get(proj, 0) + e["duration"]

    for proj in summary:
        summary[proj] = round(summary[proj] / 3600, 2)

    return summary

# --------------------------------------------------------------------------
# UPDATE / UNDO
# --------------------------------------------------------------------------
def update_project_single(event_id, project):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE activity_events SET project = %s WHERE id = %s",
        (project, event_id)
    )
    conn.commit()
    conn.close()

def update_project_bulk(ids, project):
    conn = db_conn()
    cur = conn.cursor()
    cur.executemany(
        "UPDATE activity_events SET project = %s WHERE id = %s",
        [(project, i) for i in ids]
    )
    conn.commit()
    conn.close()

def undo_single(event_id):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE activity_events SET project = 'Unassigned' WHERE id = %s",
        (event_id,)
    )
    conn.commit()
    conn.close()

def undo_bulk(ids):
    conn = db_conn()
    cur = conn.cursor()
    cur.executemany(
        "UPDATE activity_events SET project = 'Unassigned' WHERE id = %s",
        [(i,) for i in ids]
    )
    conn.commit()
    conn.close()

# --------------------------------------------------------------------------
# MAIN VIEW
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

    apps = sorted(set(r[3] for r in rows))

    enriched = []
    for e in rows:
        start = _to_dt(e[1])
        end = _to_dt(e[2])
        duration = (end - start).total_seconds() / 3600
        enriched.append(e + (duration,))

    assigned = []
    unassigned = []

    for e in enriched:
        proj = e[5] or "Unassigned"
        if proj in ("Unassigned", "None"):
            unassigned.append(e)
        else:
            assigned.append(e)

    return render_template(
        "events.html",
        assigned=assigned,
        unassigned=unassigned,
        summary=summarize_today_compressed(),
        projects=get_projects(),
        date_str=date_str,
        apps=apps
    )

# --------------------------------------------------------------------------
# ACTION ROUTES
# --------------------------------------------------------------------------
@app.route("/update", methods=["POST"])
def update():
    event_id = request.form.get("event_id")
    project_id = request.form.get("project_id")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
    proj = cur.fetchone()
    conn.close()

    if proj:
        update_project_single(event_id, proj[0])

    return redirect("/")

@app.route("/undo", methods=["POST"])
def undo():
    undo_single(request.form.get("event_id"))
    return redirect("/")

@app.route("/bulk_update", methods=["POST"])
def bulk_update_route():
    project_id = request.form.get("bulk_project_id")
    raw = request.form.get("event_ids")
    ids = list(map(int, eval(raw))) if raw else []

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
    proj = cur.fetchone()
    conn.close()

    if proj and ids:
        update_project_bulk(ids, proj[0])

    return redirect("/")

@app.route("/bulk_undo", methods=["POST"])
def bulk_undo_route():
    raw = request.form.get("event_ids")
    ids = list(map(int, eval(raw))) if raw else []

    if ids:
        undo_bulk(ids)

    return redirect("/")

# --------------------------------------------------------------------------
# PROJECT MANAGEMENT
# --------------------------------------------------------------------------
@app.route("/projects")
def project_page():
    return render_template("projects.html", projects=get_projects())

@app.route("/add_project", methods=["POST"])
def add_project_route():
    name = request.form.get("project_name")
    if name:
        add_project(name)
    return redirect("/projects")

@app.route("/delete_project/<pid>")
def delete_project_route(pid):
    delete_project(pid)
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
            round(e["duration"] / 60, 2)
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
        ORDERORDER BY hours DESC
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

# --------------------------------------------------------------------------
# TIMELINE (MULTI-ROW FANCY VERSION)
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
