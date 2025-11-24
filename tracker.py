import time
import sqlite3
from datetime import datetime

import psutil
import win32gui
import win32process


DB_PATH = r"C:\Users\DELL\Projects\ActivityTracker\activity.db"

POLL_INTERVAL_SECONDS = 1  # how often we check for active window


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time   TEXT NOT NULL,
            app_name   TEXT,
            window_title TEXT,
            project    TEXT,        -- will be filled later
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def get_active_window_info():
    """Return (app_name, window_title) of the currently active window."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None, None

        window_title = win32gui.GetWindowText(hwnd)

        # Get process ID, then process name via psutil
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        app_name = None
        try:
            p = psutil.Process(pid)
            app_name = p.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            app_name = None

        return app_name, window_title

    except Exception as e:
        # In production you might log this; for now just print
        print("Error getting active window:", e)
        return None, None


def save_event(start_time, end_time, app_name, window_title):
    """Insert a usage block into the database."""
    if start_time is None or end_time is None:
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO activity_events (start_time, end_time, app_name, window_title, project, created_at)
        VALUES (?, ?, ?, ?, NULL, ?)
    """, (
        start_time.isoformat(),
        end_time.isoformat(),
        app_name,
        window_title,
        datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()


def main():
    print("Initializing database...")
    init_db()

    print("Starting activity tracker. Press Ctrl+C to stop.")
    last_app = None
    last_title = None
    last_start_time = datetime.now()

    try:
        while True:
            app_name, window_title = get_active_window_info()

            # If window or app changed, we close previous block and start new one
            if (app_name, window_title) != (last_app, last_title):
                now = datetime.now()

                # Save the previous event block if it exists and had a title
                if last_title is not None:
                    save_event(last_start_time, now, last_app, last_title)
                    print(f"Saved: {last_app} | {last_title} | {last_start_time} -> {now}")

                # Start new block
                last_app = app_name
                last_title = window_title
                last_start_time = now

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        # When you stop the script, close the current block at that moment
        now = datetime.now()
        if last_title is not None:
            save_event(last_start_time, now, last_app, last_title)
            print(f"\nFinal saved: {last_app} | {last_title} | {last_start_time} -> {now}")
        print("\nTracker stopped.")


if __name__ == "__main__":
    main()
