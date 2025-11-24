import time
import ctypes
from ctypes import wintypes, Structure, c_uint, byref
from datetime import datetime
import psutil
import threading
import requests
import json
import getpass  # for password input

# ----------------------------------------------------------------------
# SERVER CONFIG
# ----------------------------------------------------------------------

SERVER_URL = "https://activitytracker-38jj.onrender.com"
API_KEY = "xinfini-org-activitytracker-9082347908234"  # same header key used on server

# ----------------------------------------------------------------------
# GLOBALS
# ----------------------------------------------------------------------

pending_sessions = []
USER_ID = None

# ----------------------------------------------------------------------
# WINDOWS ACTIVE WINDOW HELPERS
# ----------------------------------------------------------------------

user32 = ctypes.windll.user32

GetForegroundWindow = user32.GetForegroundWindow
GetWindowTextW = user32.GetWindowTextW
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId


def get_active_window_info():
    hwnd = GetForegroundWindow()
    if not hwnd:
        return None

    length = GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buff, length + 1)
    window_title = buff.value

    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    try:
        proc = psutil.Process(pid.value)
        app_name = proc.name()
    except psutil.NoSuchProcess:
        app_name = "Unknown"

    return app_name, window_title


# ----------------------------------------------------------------------
# IDLE TIME DETECTION
# ----------------------------------------------------------------------

class LASTINPUTINFO(Structure):
    _fields_ = [
        ("cbSize", c_uint),
        ("dwTime", c_uint)
    ]


def get_idle_seconds():
    last_input = LASTINPUTINFO()
    last_input.cbSize = ctypes.sizeof(LASTINPUTINFO)

    if not user32.GetLastInputInfo(byref(last_input)):
        return 0

    millis = ctypes.windll.kernel32.GetTickCount() - last_input.dwTime
    return millis / 1000.0


IDLE_THRESHOLD_SECONDS = 3 * 60   # 3 minutes


# ----------------------------------------------------------------------
# LOGIN (EMAIL + PASSWORD)
# ----------------------------------------------------------------------

def login():
    """
    Prompt user for email + password, call /api/login, set USER_ID.
    """
    global USER_ID

    print("== X Infin Activity Agent Login ==")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")

    try:
        resp = requests.post(
            f"{SERVER_URL}/api/login",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"email": email, "password": password})
        )
    except Exception as e:
        print(f"❌ Network error while logging in: {e}")
        return False

    if resp.status_code != 200:
        print(f"❌ Login failed ({resp.status_code}): {resp.text}")
        return False

    data = resp.json()
    if data.get("status") != "ok":
        print(f"❌ Login error: {data}")
        return False

    USER_ID = data["user_id"]
    print(f"✔ Logged in as {email} (user_id={USER_ID})")
    return True


# ----------------------------------------------------------------------
# SESSION QUEUE
# ----------------------------------------------------------------------

def add_session_to_queue(session):
    payload = {
        "user_id": USER_ID,
        "start_time": session["start"].isoformat(),
        "end_time": session["end"].isoformat(),
        "app_name": session["app"],
        "window_title": session["title"],
        "project": None
    }
    pending_sessions.append(payload)


def print_session(session):
    print(f"SESSION: {session['start']} → {session['end']} | {session['app']} | {session['title']}")


# ----------------------------------------------------------------------
# SENDER (uses SERVER_URL + API_KEY)
# ----------------------------------------------------------------------

def send_to_server(sessions):
    try:
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": API_KEY
        }

        payload = {
            "events": sessions
        }

        resp = requests.post(
            f"{SERVER_URL}/api/events/batch",
            headers=headers,
            data=json.dumps(payload)
        )

        if resp.status_code == 200:
            print(f"✔ Sent {len(sessions)} sessions.")
            return True
        else:
            print(f"❌ Server error {resp.status_code}: {resp.text}")
            return False

    except Exception as e:
        print(f"❌ Network error: {e}")
        return False


# ----------------------------------------------------------------------
# BACKGROUND WORKER
# ----------------------------------------------------------------------

def sender_worker():
    global pending_sessions

    while True:
        time.sleep(60)

        if not pending_sessions:
            # print("No sessions to upload.")
            continue

        to_send = pending_sessions.copy()
        print(f"Uploading {len(to_send)} sessions...")

        if send_to_server(to_send):
            pending_sessions = []
        else:
            print("⚠ Retrying later.")


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------

def main():
    global USER_ID

    print("X Infin Activity Agent starting...\n")

    # 1) LOGIN FIRST
    if not login():
        print("Exiting because login failed.")
        return

    # 2) Start sender worker
    threading.Thread(target=sender_worker, daemon=True).start()

    current_session = None
    in_idle_mode = False

    while True:
        now = datetime.now()
        idle_seconds = get_idle_seconds()

        # ---------------- IDLE MODE ----------------
        if idle_seconds > IDLE_THRESHOLD_SECONDS:
            if not in_idle_mode:
                if current_session:
                    print_session(current_session)
                    add_session_to_queue(current_session)

                current_session = {
                    "start": now,
                    "end": now,
                    "app": "Idle",
                    "title": "User inactive"
                }
                print("💤 Entered IDLE mode")
                in_idle_mode = True
            else:
                current_session["end"] = now

            time.sleep(5)
            continue

        # ---------------- EXIT IDLE ----------------
        if in_idle_mode:
            print_session(current_session)
            add_session_to_queue(current_session)
            print("🔄 Exit IDLE mode")
            in_idle_mode = False
            current_session = None

        # ---------------- ACTIVE MODE --------------
        info = get_active_window_info()
        if info is None:
            time.sleep(5)
            continue

        app, title = info
        short_title = title[:120]

        if current_session is None:
            current_session = {
                "start": now,
                "end": now,
                "app": app,
                "title": short_title
            }

        elif current_session["app"] == app and current_session["title"] == short_title:
            current_session["end"] = now

        else:
            print_session(current_session)
            add_session_to_queue(current_session)

            current_session = {
                "start": now,
                "end": now,
                "app": app,
                "title": short_title
            }

        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
