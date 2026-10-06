from flask import Flask, request, redirect, url_for, session, flash, render_template_string, Response
from functools import wraps
from datetime import date, datetime, timedelta
import os
import json
import csv
import io
import shutil
import calendar
import smtplib
import threading
import secrets
import re

# Load local .env when present. Production platforms should use their
# Environment Variables / Secrets instead of committing a real .env file.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Optional Google Drive storage. The app falls back to local JSON storage
# when Google Drive is not configured, so it still runs on Windows/Linux.
try:
    import google.auth
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
    GOOGLE_DRIVE_LIBS_AVAILABLE = True
except Exception:
    GOOGLE_DRIVE_LIBS_AVAILABLE = False
from email.message import EmailMessage

app = Flask(__name__)

# Keep template errors visible in the terminal during development.
# Set JNV_DEBUG=1 to enable Flask debug mode.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ============================================================
# JAWAHAR NAVODAYA VIDYALAYA
# COMPLETE ATTENDANCE MANAGEMENT SYSTEM
# Flask + JSON
# NO SQL / NO SQLITE
#
# Modules:
# 1. Student Daily Attendance
# 2. Period / Subject Attendance
# 3. Morning Assembly Attendance
# 4. Hostel / Night Attendance
# 5. Mess Attendance
# 6. Medical / Sick Leave
# 7. Leave / Permission
# 8. Late / Early Leave
# 9. Teacher / Staff Attendance
# 10. Reports
# 11. Monthly Reports
# 12. Low Attendance
# 13. CSV Export
# 14. Backup
# ============================================================

app.secret_key = os.environ.get("JNV_SECRET_KEY", "JNV-ATTENDANCE-2026-SECURE")

# Portable storage paths. No hard-coded Windows path.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# ------------------------------------------------------------------
# GOOGLE DRIVE STORAGE
# ------------------------------------------------------------------
# Set JNV_GOOGLE_DRIVE_FOLDER_ID to a Google Drive folder ID to store
# all JSON data there. Cloud Run can use its service account via ADC.
# For local Windows use, you may instead set GOOGLE_APPLICATION_CREDENTIALS
# to a service-account JSON key, or use JNV_GOOGLE_SERVICE_ACCOUNT_JSON.
# If these variables are absent, the app uses local ./data JSON files.
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("JNV_GOOGLE_DRIVE_FOLDER_ID", "").strip()
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
_drive_service = None
_drive_lock = threading.RLock()
_drive_file_ids = {}

def google_drive_enabled():
    return bool(GOOGLE_DRIVE_FOLDER_ID) and GOOGLE_DRIVE_LIBS_AVAILABLE

def _drive_service():
    global _drive_service
    if not google_drive_enabled():
        return None
    with _drive_lock:
        if _drive_service is not None:
            return _drive_service
        try:
            credentials = None

            # Personal Google Drive: use an OAuth authorized-user token.
            # This is the safest option for a normal @gmail.com My Drive.
            raw_token = os.environ.get("JNV_GOOGLE_TOKEN_JSON", "").strip()
            if raw_token:
                from google.oauth2.credentials import Credentials as UserCredentials
                credentials = UserCredentials.from_authorized_user_info(
                    json.loads(raw_token), scopes=GOOGLE_DRIVE_SCOPES
                )

            # Optional service-account JSON. Recommended only for a Shared Drive
            # (service accounts do not have personal Drive storage quota).
            if credentials is None:
                raw = os.environ.get("JNV_GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
                if raw:
                    credentials = google.auth.load_credentials_from_dict(
                        json.loads(raw), scopes=GOOGLE_DRIVE_SCOPES
                    )[0]

            if credentials is None:
                credentials, _ = google.auth.default(scopes=GOOGLE_DRIVE_SCOPES)

            _drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
            return _drive_service
        except Exception as exc:
            print("Google Drive initialization failed:", exc)
            return None

def _drive_find_file(name):
    service = _drive_service()
    if not service:
        return None
    if name in _drive_file_ids:
        return _drive_file_ids[name]
    q = (
        "'" + GOOGLE_DRIVE_FOLDER_ID + "' in parents "
        "and name = '" + name.replace("'", "\\'") + "' "
        "and trashed = false"
    )
    try:
        result = service.files().list(
            q=q, spaces="drive", fields="files(id,name,modifiedTime)", pageSize=10
        ).execute()
        files = result.get("files", [])
        if files:
            _drive_file_ids[name] = files[0]["id"]
            return files[0]["id"]
    except Exception as exc:
        print("Google Drive lookup failed:", exc)
    return None

def _drive_save_json(name, data):
    service = _drive_service()
    if not service:
        return False
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False)
    try:
        file_id = _drive_find_file(name)
        if file_id:
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            created = service.files().create(
                body={"name": name, "parents": [GOOGLE_DRIVE_FOLDER_ID], "mimeType": "application/json"},
                media_body=media, fields="id"
            ).execute()
            _drive_file_ids[name] = created["id"]
        return True
    except Exception as exc:
        print("Google Drive save failed:", exc)
        return False

def _drive_load_json(name):
    service = _drive_service()
    if not service:
        return None
    try:
        file_id = _drive_find_file(name)
        if not file_id:
            return None
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.seek(0)
        return json.loads(buffer.read().decode("utf-8"))
    except Exception as exc:
        print("Google Drive load failed:", exc)
        return None

def restore_drive_cache():
    if not google_drive_enabled():
        return
    print("Google Drive storage enabled. Restoring JSON cache...")
    for kind, filename in FILES.items():
        data = _drive_load_json(filename)
        if isinstance(data, list):
            try:
                with open(path(kind), "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                print("Drive cache write failed:", filename, exc)


# ============================================================
# FILES
# ============================================================

FILES = {
    "users": "users.json",
    "classes": "classes.json",
    "subjects": "subjects.json",
    "teachers": "teachers.json",
    "students": "students.json",
    "assignments": "teacher_subjects.json",

    # Attendance
    "attendance": "attendance.json",
    "daily_attendance": "daily_attendance.json",
    "assembly_attendance": "assembly_attendance.json",
    "hostel_attendance": "hostel_attendance.json",
    "mess_attendance": "mess_attendance.json",
    "staff_attendance": "staff_attendance.json",
    "leave_records": "leave_records.json",
    "late_records": "late_records.json",
    "medical_records": "medical_records.json",

    # Special JNV routine attendance modules
    "morning_pt_attendance": "morning_pt_attendance.json",
    "remedial_attendance": "remedial_attendance.json",
    "evening_student_attendance": "evening_student_attendance.json",
    "evening_games_attendance": "evening_games_attendance.json",
    "cdd_hm_hm_attendance": "cdd_hm_hm_attendance.json",
    "teacher_submissions": "teacher_submissions.json",
    "mods": "mods.json",
    "mod_attendance": "mod_attendance.json",
    "password_resets": "password_resets.json",
    "principal_registrations": "principal_registrations.json",
    "admins": "admins.json",
    "admin_principal_registrations": "admin_principal_registrations.json",
    "admin_credential_logs": "admin_credential_logs.json",
}


# ============================================================
# MASTER DATA
# ============================================================

CLASSES = [
    {"id": "C1", "name": "Class 6", "section": "A"},
    {"id": "C2", "name": "Class 7", "section": "A"},
    {"id": "C3", "name": "Class 8", "section": "A"},
    {"id": "C4", "name": "Class 9", "section": "A"},
    {"id": "C5", "name": "Class 10", "section": "A"},
    {"id": "C6", "name": "Class 11", "section": "A"},
    {"id": "C7", "name": "Class 12", "section": "A"},
]

SUBJECTS = [
    {"id": "S1", "name": "AI"},
    {"id": "S2", "name": "English"},
    {"id": "S3", "name": "Hindi"},
    {"id": "S4", "name": "Mathematics"},
    {"id": "S5", "name": "Science"},
    {"id": "S6", "name": "Social Science"},
]

TEACHERS = [
    {
        "id": "T1",
        "user_id": "U2",
        "name": "Demo Teacher 1",
        "employee_id": "T001",
        "mobile": ""
    },
    {
        "id": "T2",
        "user_id": "U3",
        "name": "Demo Teacher 2",
        "employee_id": "T002",
        "mobile": ""
    },
]

USERS = [
    {
        "id": "U2",
        "username": "teacher1",
        "password": "teacher123",
        "role": "teacher",
        "name": "Demo Teacher 1"
    },
    {
        "id": "U3",
        "username": "teacher2",
        "password": "teacher123",
        "role": "teacher",
        "name": "Demo Teacher 2"
    },
]


ASSIGNMENTS = [
    {"id": "TS1", "teacher_id": "T1", "class_id": "C1", "subject_id": "S1"},
    {"id": "TS2", "teacher_id": "T1", "class_id": "C2", "subject_id": "S1"},
    {"id": "TS3", "teacher_id": "T1", "class_id": "C3", "subject_id": "S2"},
    {"id": "TS4", "teacher_id": "T1", "class_id": "C4", "subject_id": "S4"},

    {"id": "TS5", "teacher_id": "T2", "class_id": "C5", "subject_id": "S1"},
    {"id": "TS6", "teacher_id": "T2", "class_id": "C6", "subject_id": "S2"},
    {"id": "TS7", "teacher_id": "T2", "class_id": "C7", "subject_id": "S4"},
]


# ============================================================
# JNV SUMMARY ATTENDANCE MASTER SETTINGS
# ============================================================
# Demo class strength. Change these values later from the Classes
# module if you want different class-wise totals.
CLASS_TOTALS = {c["id"]: 40 for c in CLASSES}
CLASS_GENDER_TOTALS = {c["id"]: {"boys": 20, "girls": 20} for c in CLASSES}
REMEDIAL_TOTALS = {c["id"]: 30 for c in CLASSES}

HOUSE_GROUPS = [
    ("Aravali", "Jr", "Boys"), ("Aravali", "Jr", "Girls"),
    ("Aravali", "Sr", "Boys"), ("Aravali", "Sr", "Girls"),
    ("Nilgiri", "Jr", "Boys"), ("Nilgiri", "Jr", "Girls"),
    ("Nilgiri", "Sr", "Boys"), ("Nilgiri", "Sr", "Girls"),
    ("Shivalik", "Jr", "Boys"), ("Shivalik", "Jr", "Girls"),
    ("Shivalik", "Sr", "Boys"), ("Shivalik", "Sr", "Girls"),
    ("Udaigiri", "Jr", "Boys"), ("Udaigiri", "Jr", "Girls"),
    ("Udaigiri", "Sr", "Boys"), ("Udaigiri", "Sr", "Girls"),
]

HOUSE_TOTALS = {f"{h} {stage} {gender}": 60 for h, stage, gender in HOUSE_GROUPS}




FIRST = [
    "Aarav", "Vivaan", "Aditya", "Arjun",
    "Kabir", "Rohan", "Dev", "Yash",
    "Manav", "Ayaan", "Reyansh", "Atharv",
    "Vihaan", "Kunal", "Dhruv", "Rudra",
    "Anaya", "Diya", "Ishita", "Riya",
    "Myra", "Sara", "Kavya", "Pihu",
    "Siya", "Nisha", "Avni", "Meera"
]

LAST = [
    "Sharma", "Patel", "Kumar", "Singh",
    "Shah", "Mehta", "Joshi", "Verma"
]


# ============================================================
# DEMO STUDENTS
# ============================================================

STUDENTS = []

n = 1

for c in CLASSES:

    for j in range(4):

        STUDENTS.append({
            "id": f"ST{n}",
            "name": f"{FIRST[(n - 1) % len(FIRST)]} "
                    f"{LAST[(n + j) % len(LAST)]}",
            "roll": str(j + 1),
            "class_id": c["id"],
            "section": c["section"],
            "gender": "Male" if j % 2 == 0 else "Female",
            "mobile": "",
            "admission_no": f"JNV2026{n:03d}",
            "father_name": "",
            "mother_name": "",
            "house": ["Aravali", "Nilgiri", "Shivalik", "Udayagiri"][j % 4],
            "hostel": "Yes",
            "status": "Active"
        })

        n += 1


# ============================================================
# FILE FUNCTIONS
# ============================================================

def path(kind):
    return os.path.join(DATA_DIR, FILES[kind])


def next_id(prefix, items):

    nums = []

    for x in items:

        if isinstance(x, dict):

            value = str(x.get("id", ""))

            if value.startswith(prefix):

                try:
                    nums.append(
                        int(value[len(prefix):])
                    )
                except:
                    pass

    return prefix + str(max(nums, default=0) + 1)


def save(kind, data):

    target = path(kind)
    temp = target + ".tmp"

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp, target)

    # Mirror every JSON write to Google Drive when enabled.
    if google_drive_enabled():
        with _drive_lock:
            if not _drive_save_json(FILES[kind], data):
                print("WARNING: Data was saved locally but not to Google Drive:", kind)


def safe_load(kind, default):

    p = path(kind)

    # In Drive mode, refresh the local cache before reading.
    if google_drive_enabled():
        with _drive_lock:
            remote = _drive_load_json(FILES[kind])
            if isinstance(remote, list):
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(remote, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

    try:

        with open(
            p,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(data, list):

            if isinstance(data, dict):
                data = list(data.values())
            else:
                data = []

        return [
            x for x in data
            if isinstance(x, dict)
        ]

    except:

        save(kind, default)

        return list(default)


# ============================================================
# INITIALIZE DATA
# ============================================================

def ensure_demo_data():

    demo = {
        "users": USERS,
        "classes": CLASSES,
        "subjects": SUBJECTS,
        "teachers": TEACHERS,
        "students": STUDENTS,
        "assignments": ASSIGNMENTS,
    }

    for kind, data in demo.items():

        p = path(kind)

        valid = False
        old = []

        try:

            with open(
                p,
                "r",
                encoding="utf-8"
            ) as f:

                old = json.load(f)

            valid = (
                isinstance(old, list)
                and all(
                    isinstance(x, dict)
                    for x in old
                )
            )

        except:

            valid = False

        if not valid:

            if os.path.exists(p):

                try:
                    shutil.copy2(
                        p,
                        p + ".broken-backup.json"
                    )
                except:
                    pass

            save(kind, data)

    # Principal accounts are managed by the separate Admin panel. Multiple
    # Principal accounts are allowed. Existing Principal records are preserved.

    # Create empty attendance files

    for kind in [
        "attendance",
        "daily_attendance",
        "assembly_attendance",
        "hostel_attendance",
        "mess_attendance",
        "staff_attendance",
        "leave_records",
        "late_records",
        "medical_records",
        "morning_pt_attendance",
        "remedial_attendance",
        "evening_student_attendance",
        "evening_games_attendance",
        "cdd_hm_hm_attendance",
        "teacher_submissions",
        "mods",
        "mod_attendance",
        "principal_registrations",
        "admin_principal_registrations",
        "admin_credential_logs",
    ]:

        p = path(kind)

        try:

            with open(
                p,
                "r",
                encoding="utf-8"
            ) as f:

                json.load(f)

        except:

            save(kind, [])


# Restore existing Drive data before the normal demo-data initializer runs.
# This prevents a Cloud Run restart from replacing real attendance data.
restore_drive_cache()
ensure_demo_data()

def normalize_email(value):
    return (value or "").strip().lower()
# ============================================================
# SEPARATE ADMIN ACCOUNT
# ============================================================

def ensure_admin_account():
    admins_data = safe_load("admins", [])
    if not admins_data:
        # Default values requested by the owner. For deployment, set these
        # environment variables so the credentials never need to be stored
        # in the GitHub source code.
        admin_username = os.environ.get("JNV_ADMIN_USERNAME", "Roy@1901").strip()
        admin_password = os.environ.get("JNV_ADMIN_PASSWORD", "pass@1901")
        admin_email = normalize_email(os.environ.get("JNV_ADMIN_EMAIL", ""))
        admins_data = [{
            "id": "A1",
            "username": admin_username,
            "password": admin_password,
            "name": "JNV System Admin",
            "email": admin_email,
            "created_at": datetime.now().isoformat(timespec="seconds")
        }]
        save("admins", admins_data)
    return admins_data

ensure_admin_account()


# ============================================================
# DATA HELPERS
# ============================================================

def users():
    return safe_load("users", USERS)


def classes():
    return safe_load("classes", CLASSES)


def subjects():
    return safe_load("subjects", SUBJECTS)


def teachers():
    return safe_load("teachers", TEACHERS)


def students():
    return safe_load("students", STUDENTS)


def assignments():
    return safe_load("assignments", ASSIGNMENTS)


def attendance_records():
    return safe_load("attendance", [])


def daily_records():
    return safe_load("daily_attendance", [])


def assembly_records():
    return safe_load("assembly_attendance", [])


def hostel_records():
    return safe_load("hostel_attendance", [])


def mess_records():
    return safe_load("mess_attendance", [])


def staff_records():
    return safe_load("staff_attendance", [])


def leave_records():
    return safe_load("leave_records", [])


def late_records():
    return safe_load("late_records", [])


def medical_records():
    return safe_load("medical_records", [])


def special_attendance_records(kind):
    return safe_load(kind, [])


def password_reset_records():
    return safe_load("password_resets", [])


def principal_registration_records():
    return safe_load("principal_registrations", [])




def valid_email(value):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalize_email(value)))


def principal_users():
    return [u for u in users() if u.get("role") == "principal"]


def get_single_principal():
    principals = principal_users()
    return principals[0] if principals else None


def principal_by_username(username):
    username = (username or "").strip()
    return next((u for u in principal_users() if u.get("username") == username), None)


def send_principal_otp_email(to_email, principal_name, otp, purpose="registration"):
    sender, app_password = gmail_settings()
    to_email = normalize_email(to_email)
    if not valid_email(to_email):
        return False, "Email address is invalid."
    if not sender:
        return False, "Sender Gmail is not configured. Set JNV_GMAIL_ADDRESS."
    if not app_password:
        return False, "Gmail App Password is not configured. Set JNV_GMAIL_APP_PASSWORD."

    if purpose == "registration":
        subject = "JNV Attendance - Principal Account Verification OTP"
        title = "Principal Account Registration"
        intro = "Use this OTP to verify your email and create the Principal account."
    else:
        subject = "JNV Attendance - Principal Password Reset OTP"
        title = "Principal Password Reset"
        intro = "Use this OTP to verify your identity and reset the Principal password."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(
        f"Jawahar Navodaya Vidyalaya\n{title}\n\n"
        f"Dear {principal_name or 'Principal'},\n\n"
        f"{intro}\n\n"
        f"Your 6-digit OTP is: {otp}\n\n"
        "This OTP is valid for 10 minutes and can be used only once.\n"
        "If you did not request this, ignore this email.\n\n"
        "JNV Attendance Management System"
    )
    errors=[]
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(sender, app_password)
            smtp.send_message(msg)
        return True, f"OTP sent to {to_email}"
    except Exception as exc:
        errors.append(f"SMTP 587: {exc}")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.ehlo()
            smtp.login(sender, app_password)
            smtp.send_message(msg)
        return True, f"OTP sent to {to_email}"
    except Exception as exc:
        errors.append(f"SMTP SSL 465: {exc}")
    return False, "Gmail send failed. " + " | ".join(errors)


def cleanup_password_resets():
    now=datetime.now(); rows=[]
    for x in password_reset_records():
        try:
            if datetime.fromisoformat(str(x.get("expires_at", ""))) > now:
                rows.append(x)
        except Exception:
            pass
    save("password_resets", rows)
    return rows


def cleanup_principal_registrations():
    now=datetime.now(); rows=[]
    for x in principal_registration_records():
        try:
            if datetime.fromisoformat(str(x.get("expires_at", ""))) > now:
                rows.append(x)
        except Exception:
            pass
    save("principal_registrations", rows)
    return rows


# ============================================================
# ADMIN HELPERS
# ============================================================

def admins():
    return safe_load("admins", [])

def admin_current_user():
    aid = session.get("admin_id")
    if not aid:
        return None
    return next((a for a in admins() if a.get("id") == aid), None)

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not admin_current_user():
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

def admin_principal_registration_records():
    return safe_load("admin_principal_registrations", [])

def cleanup_admin_principal_registrations():
    now=datetime.now(); rows=[]
    for x in admin_principal_registration_records():
        try:
            if datetime.fromisoformat(str(x.get("expires_at", ""))) > now:
                rows.append(x)
        except Exception:
            pass
    save("admin_principal_registrations", rows)
    return rows

def send_principal_credentials_email(to_email, principal_name, username, password, school_name=""):
    sender, app_password = gmail_settings()
    to_email = normalize_email(to_email)
    if not valid_email(to_email):
        return False, "Principal email address is invalid."
    if not sender:
        return False, "Sender Gmail is not configured. Set JNV_GMAIL_ADDRESS."
    if not app_password:
        return False, "Gmail App Password is not configured. Set JNV_GMAIL_APP_PASSWORD."
    msg=EmailMessage()
    msg["Subject"]="JNV Attendance - Principal Login Credentials"
    msg["From"]=sender; msg["To"]=to_email
    msg.set_content(
        f"Jawahar Navodaya Vidyalaya\nJNV Attendance Management System\n\n"
        f"Dear {principal_name or 'Principal'},\n\n"
        "Your Principal account has been created and your email has been verified.\n\n"
        f"School: {school_name or 'JNV'}\n"
        f"Login Username: {username}\n"
        f"Login Password: {password}\n\n"
        "Please keep these credentials confidential.\n\nJNV Attendance Management System"
    )
    try:
        with smtplib.SMTP("smtp.gmail.com",587,timeout=30) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.ehlo(); smtp.login(sender,app_password); smtp.send_message(msg)
        return True,"Credentials email sent successfully."
    except Exception:
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com",465,timeout=30) as smtp:
                smtp.login(sender,app_password); smtp.send_message(msg)
            return True,"Credentials email sent successfully."
        except Exception as exc:
            return False,f"Credentials email failed: {exc}"


# ============================================================
# USER HELPERS
# ============================================================

def current_user():

    uid = session.get("user_id")

    if not uid:
        return None

    return next(
        (
            u for u in users()
            if u.get("id") == uid
        ),
        None
    )


def login_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        if not current_user():

            session.clear()

            return redirect(
                url_for("login")
            )

        return fn(*args, **kwargs)

    return wrapper


def principal_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        u = current_user()

        if not u:

            session.clear()

            return redirect(
                url_for("login")
            )

        if u.get("role") != "principal":

            flash(
                "Only Principal can open this section."
            )

            return redirect(
                url_for("dashboard")
            )

        return fn(*args, **kwargs)

    return wrapper


def teacher_profile(uid):

    return next(
        (
            t for t in teachers()
            if t.get("user_id") == uid
        ),
        None
    )


# ============================================================
# NAME HELPERS
# ============================================================

def class_name(cid):

    c = next(
        (
            x for x in classes()
            if x.get("id") == cid
        ),
        None
    )

    if not c:
        return cid

    return (
        f'{c.get("name")} - '
        f'{c.get("section", "")}'
    )


def subject_name(sid):

    s = next(
        (
            x for x in subjects()
            if x.get("id") == sid
        ),
        None
    )

    return (
        s.get("name")
        if s
        else sid
    )


def teacher_name(tid):

    t = next(
        (
            x for x in teachers()
            if x.get("id") == tid
        ),
        None
    )

    return (
        t.get("name")
        if t
        else tid
    )


def student_by_id(sid):

    return next(
        (
            s for s in students()
            if s.get("id") == sid
        ),
        None
    )


def esc(v):

    return (
        str(v)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ============================================================
# ATTENDANCE CALCULATIONS
# ============================================================

def percentage(present, total):

    if not total:
        return 0

    return round(
        present * 100 / total,
        1
    )


def attendance_pct(
    student_id=None,
    class_id=None,
    records=None
):

    records = (
        records
        if records is not None
        else attendance_records()
    )

    if student_id:

        rows = [
            r for r in records
            if r.get("student_id") == student_id
        ]

    elif class_id:

        rows = [
            r for r in records
            if r.get("class_id") == class_id
        ]

    else:

        rows = records

    if not rows:
        return 0

    present = sum(
        r.get("status") == "Present"
        for r in rows
    )

    return percentage(
        present,
        len(rows)
    )


def status_class(pct):

    if pct >= 75:
        return "good"

    if pct >= 60:
        return "warn"

    return "bad"


# ============================================================
# BACKUP
# ============================================================

def backup_all():

    stamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    folder = os.path.join(
        BACKUP_DIR,
        "backup_" + stamp
    )

    os.makedirs(
        folder,
        exist_ok=True
    )

    for kind in FILES:

        p = path(kind)

        if os.path.exists(p):

            shutil.copy2(
                p,
                os.path.join(
                    folder,
                    FILES[kind]
                )
            )

    return folder


# ============================================================
# BASE HTML
# ============================================================

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#123c69">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{{ title or 'JNV Attendance' }}</title>

<style>
/* ============================================================
   JNV ATTENDANCE - RESPONSIVE / MOBILE-FIRST UI
   Keeps existing Flask routes and page content unchanged.
   ============================================================ */

:root{
    --primary:#123c69;
    --primary2:#1769aa;
    --primary3:#285b8e;
    --bg:#f3f6fb;
    --card:#ffffff;
    --text:#172033;
    --muted:#65758b;
    --border:#d9e1ea;
    --success:#18794e;
    --danger:#b42318;
    --warning:#c66a00;
    --purple:#6b46c1;
    --shadow:0 2px 12px rgba(15,23,42,.08);
    --radius:14px;
}

*,
*::before,
*::after{
    box-sizing:border-box;
}

html{
    width:100%;
    min-height:100%;
    scroll-behavior:smooth;
    -webkit-text-size-adjust:100%;
}

body{
    margin:0;
    min-height:100vh;
    width:100%;
    overflow-x:hidden;
    font-family:Segoe UI,Arial,sans-serif;
    background:var(--bg);
    color:var(--text);
    line-height:1.45;
}

body.menu-open{
    overflow:hidden;
}

/* ---------------- NAVIGATION ---------------- */

nav{
    position:sticky;
    top:0;
    z-index:1000;
    width:100%;
    min-height:60px;
    background:var(--primary);
    color:#fff;
    padding:8px max(14px,env(safe-area-inset-right))
             8px max(14px,env(safe-area-inset-left));
    display:flex;
    align-items:center;
    gap:6px;
    box-shadow:0 2px 12px rgba(0,0,0,.16);
}

/* Mobile scroll behavior:
   The navigation stays visible while the user scrolls up,
   and slides away when scrolling down. */
nav{
    transition:transform .24s ease, box-shadow .24s ease;
    will-change:transform;
}

nav.nav-hidden{
    transform:translateY(-110%);
}

nav.menu-open{
    transform:translateY(0) !important;
}

.nav-brand{
    flex:0 0 auto;
    display:flex;
    align-items:center;
    gap:8px;
    min-width:0;
    font-size:18px;
    font-weight:800;
    white-space:nowrap;
}

.nav-toggle{
    display:none;
    width:44px;
    height:44px;
    flex:0 0 44px;
    margin:0;
    padding:0;
    border:0;
    border-radius:10px;
    background:rgba(255,255,255,.12);
    color:#fff;
    cursor:pointer;
    font-size:24px;
    line-height:1;
    align-items:center;
    justify-content:center;
    touch-action:manipulation;
}

.nav-toggle:hover,
.nav-toggle:focus-visible{
    background:rgba(255,255,255,.22);
    outline:none;
}

.nav-links{
    display:flex;
    align-items:center;
    justify-content:flex-end;
    gap:4px;
    flex:1 1 auto;
    min-width:0;
    flex-wrap:wrap;
}

nav a{
    color:#fff;
    text-decoration:none;
    padding:9px 10px;
    min-height:40px;
    display:inline-flex;
    align-items:center;
    justify-content:center;
    border-radius:8px;
    font-size:13px;
    white-space:nowrap;
    touch-action:manipulation;
}

nav a:hover,
nav a:focus-visible{
    background:var(--primary3);
    outline:none;
}

.menu-title{
    color:#dbeafe;
    font-size:12px;
    font-weight:700;
    padding:7px 8px 3px;
    white-space:nowrap;
}

/* ---------------- LAYOUT ---------------- */

.wrap{
    width:100%;
    max-width:1440px;
    margin:0 auto;
    padding:20px 16px calc(30px + env(safe-area-inset-bottom));
}

.card{
    width:100%;
    min-width:0;
    background:var(--card);
    border-radius:var(--radius);
    padding:18px;
    margin-bottom:18px;
    box-shadow:var(--shadow);
    overflow:visible;
}

h1,h2,h3,h4{
    line-height:1.2;
    overflow-wrap:anywhere;
}

h1{
    font-size:clamp(22px,3vw,32px);
    margin-top:0;
}

h2{
    font-size:clamp(19px,2.5vw,26px);
}

h3{
    font-size:18px;
}

p{
    overflow-wrap:anywhere;
}

.grid{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
    gap:14px;
}

.stat{
    min-width:0;
    padding:18px;
    border-radius:12px;
    background:#eaf2fb;
}

.stat strong{
    font-size:clamp(23px,3vw,30px);
    display:block;
    margin-top:4px;
    overflow-wrap:anywhere;
}

/* ---------------- TABLES ---------------- */

.table-wrap{
    width:100%;
    max-width:100%;
    overflow-x:auto;
    overflow-y:visible;
    -webkit-overflow-scrolling:touch;
    border-radius:10px;
}

table{
    width:100%;
    min-width:650px;
    border-collapse:collapse;
    background:#fff;
}

th,
td{
    padding:10px;
    border-bottom:1px solid #e5e9ef;
    text-align:left;
    vertical-align:middle;
}

th{
    background:#edf3f9;
    position:sticky;
    top:60px;
    z-index:2;
    font-weight:700;
}

tbody tr:hover{
    background:#f8fbff;
}

/* Existing tables automatically become scrollable on phones. */
@media(max-width:700px){
    table{
        display:block;
        width:100%;
        max-width:100%;
        overflow-x:auto;
        -webkit-overflow-scrolling:touch;
        white-space:nowrap;
        font-size:13px;
    }

    thead,
    tbody{
        width:max-content;
        min-width:100%;
    }

    th,
    td{
        padding:9px 10px;
    }

    th{
        position:sticky;
        top:0;
    }
}

/* ---------------- FORMS / CONTROLS ---------------- */

input,
select,
textarea,
button,
.btn{
    font:inherit;
}

input,
select,
textarea{
    width:auto;
    max-width:100%;
    min-height:42px;
    padding:10px 11px;
    border:1px solid #ccd5df;
    border-radius:9px;
    margin:4px 2px;
    background:#fff;
    color:var(--text);
}

textarea{
    min-height:90px;
    resize:vertical;
}

input:focus,
select:focus,
textarea:focus{
    outline:3px solid rgba(23,105,170,.14);
    border-color:var(--primary2);
}

button,
.btn{
    min-height:42px;
    padding:10px 13px;
    border:0;
    border-radius:9px;
    background:var(--primary2);
    color:#fff;
    cursor:pointer;
    text-decoration:none;
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:6px;
    touch-action:manipulation;
    font-weight:600;
}

button:hover,
.btn:hover{
    filter:brightness(.94);
}

button:active,
.btn:active{
    transform:translateY(1px);
}

.danger{background:var(--danger)}
.ok{background:var(--success)}
.gray{background:#64748b}
.orange{background:var(--warning)}
.purple{background:var(--purple)}

form{
    max-width:100%;
}

form > input,
form > select,
form > textarea{
    max-width:100%;
}

.toolbar{
    display:flex;
    gap:7px;
    align-items:center;
    flex-wrap:wrap;
}

.toolbar > *{
    max-width:100%;
}

.badge{
    display:inline-block;
    padding:5px 9px;
    border-radius:15px;
    background:#eaf2fb;
    font-size:12px;
}

.alert{
    padding:12px 14px;
    background:#fff2cc;
    border:1px solid #f0d77a;
    border-radius:9px;
    margin-bottom:12px;
    overflow-wrap:anywhere;
}

.small{
    color:var(--muted);
    font-size:13px;
}

.center{text-align:center}

.good{color:var(--success);font-weight:bold}
.bad{color:var(--danger);font-weight:bold}
.warn{color:#a15c00;font-weight:bold}

.progress{
    background:#e8edf3;
    border-radius:20px;
    overflow:hidden;
    height:12px;
    min-width:90px;
}

.progress span{
    display:block;
    height:100%;
    background:var(--success);
}

.menu-group{
    display:inline-flex;
    gap:5px;
    align-items:center;
    flex-wrap:wrap;
}

.section-title{
    border-left:5px solid var(--primary2);
    padding-left:10px;
}

/* ---------------- LOGIN ---------------- */

.login{
    position:relative;
    width:min(430px,100%);
    margin:clamp(25px,8vh,70px) auto;
}

.login .card{
    padding:22px;
}

.login form input,
.login form select,
.login form textarea{
    width:100%;
    margin:5px 0;
}

.login form button,
.login form .btn{
    width:100%;
    margin:6px 0;
}

.password-wrap{
    position:relative;
    width:100%;
    margin:0;
}

.password-wrap input{
    width:100%;
    padding-right:82px;
    box-sizing:border-box;
}

.password-toggle{
    position:absolute;
    right:6px;
    top:6px;
    min-height:30px;
    margin:0;
    padding:5px 9px;
    background:#64748b;
    color:#fff;
    border:0;
    border-radius:6px;
    font-size:12px;
    cursor:pointer;
}

/* ---------------- HELP CENTER ---------------- */

.help-page{
    width:min(1150px,100%);
    margin:20px auto;
    padding:0 10px;
}

.help-hero{
    background:linear-gradient(135deg,#4f46e5,#7c3aed);
    color:#fff;
    border-radius:16px;
    padding:28px;
    margin-bottom:18px;
    box-shadow:0 10px 30px rgba(76,29,149,.16);
}

.help-hero h1{
    margin:0 0 10px;
    font-size:clamp(24px,5vw,30px);
}

.help-hero p{
    margin:8px 0;
    line-height:1.6;
}

.help-grid{
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:16px;
}

.help-section{
    background:#fff;
    border-radius:14px;
    padding:20px;
    border:1px solid #e5e7eb;
    box-shadow:0 4px 14px rgba(15,23,42,.05);
    min-width:0;
}

.help-section h2{
    margin-top:0;
    color:#4f46e5;
    font-size:20px;
}

.help-section li{
    margin:6px 0;
    line-height:1.5;
}

.flow{
    display:flex;
    flex-wrap:wrap;
    align-items:center;
    justify-content:center;
    gap:10px;
    margin-top:12px;
}

.flow-step{
    background:#eef2ff;
    border:1px solid #c7d2fe;
    padding:10px 14px;
    border-radius:10px;
    font-weight:700;
    text-align:center;
    min-width:120px;
}

.flow-arrow{
    font-size:24px;
    font-weight:700;
    color:#6366f1;
}

.contact-card{
    background:#f8fafc;
    border-left:4px solid #6366f1;
    border-radius:10px;
    padding:12px 14px;
}

.help-footer{
    text-align:center;
    margin:20px 0 4px;
    color:#64748b;
    font-size:13px;
}

.login-help,
.login-help-button{
    background:#6d4bc1;
    color:#fff;
    border:0;
    border-radius:8px;
    font-weight:700;
    font-size:14px;
    box-shadow:0 2px 8px rgba(0,0,0,.12);
}

.login-help{
    position:absolute;
    right:18px;
    top:18px;
    text-decoration:none;
    padding:8px 12px;
}

.login-help-button{
    position:absolute;
    right:18px;
    top:18px;
    padding:8px 12px;
    cursor:pointer;
    z-index:2;
}

.help-modal{
    display:none;
    position:fixed;
    inset:0;
    background:rgba(15,23,42,.72);
    z-index:9999;
    padding:20px;
    overflow:auto;
}

.help-modal.open{display:block}

.help-modal-box{
    max-width:1100px;
    margin:20px auto;
    background:#f8fafc;
    border-radius:18px;
    box-shadow:0 15px 50px rgba(0,0,0,.3);
    overflow:hidden;
}

.help-modal-head{
    position:sticky;
    top:0;
    background:#4f46e5;
    color:#fff;
    padding:18px 22px;
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:12px;
    z-index:2;
}

.help-modal-head h2{
    margin:0;
    font-size:22px;
}

.help-close{
    background:#fff;
    color:#4f46e5;
    border:0;
    border-radius:8px;
    padding:8px 12px;
    font-weight:800;
    cursor:pointer;
}

.help-modal-content{padding:20px}

.help-mini-grid{
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:14px;
}

.help-mini-card{
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:12px;
    padding:16px;
}

.help-mini-card h3{
    margin-top:0;
    color:#4f46e5;
}

.help-flow{
    display:flex;
    flex-wrap:wrap;
    align-items:center;
    justify-content:center;
    gap:8px;
    margin-top:10px;
}

.help-flow-step{
    background:#eef2ff;
    border:1px solid #c7d2fe;
    border-radius:10px;
    padding:9px 12px;
    font-weight:700;
    text-align:center;
}

.help-flow-arrow{
    font-size:22px;
    font-weight:800;
    color:#6366f1;
}

.help-contact{
    background:#eef2ff;
    border-left:4px solid #6366f1;
    border-radius:10px;
    padding:10px 14px;
}

/* ---------------- MOBILE ---------------- */

@media(max-width:900px){
    .nav-toggle{
        display:inline-flex;
        order:2;
        margin-left:auto;
    }

    .nav-brand{
        order:1;
        max-width:calc(100% - 54px);
        overflow:hidden;
        text-overflow:ellipsis;
    }

    .nav-links{
        display:none;
        position:absolute;
        left:0;
        right:0;
        top:60px;
        max-height:calc(100vh - 60px);
        overflow-y:auto;
        -webkit-overflow-scrolling:touch;
        background:var(--primary);
        padding:10px 12px calc(14px + env(safe-area-inset-bottom));
        flex-direction:column;
        align-items:stretch;
        justify-content:flex-start;
        gap:3px;
        box-shadow:0 10px 18px rgba(0,0,0,.2);
    }

    nav.menu-open .nav-links{
        display:flex;
    }

    nav a{
        width:100%;
        min-height:44px;
        justify-content:flex-start;
        padding:11px 13px;
        font-size:14px;
    }

    .menu-title{
        padding:12px 13px 5px;
    }

    .wrap{
        padding:14px 10px calc(24px + env(safe-area-inset-bottom));
    }

    .card{
        padding:14px;
        margin-bottom:14px;
        border-radius:12px;
    }

    .grid{
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:10px;
    }

    .stat{
        padding:14px;
    }

    .toolbar{
        align-items:stretch;
    }

    .toolbar > input,
    .toolbar > select,
    .toolbar > button,
    .toolbar > .btn{
        flex:1 1 150px;
    }

    .help-grid,
    .help-mini-grid{
        grid-template-columns:1fr;
    }

    .help-hero{
        padding:20px;
    }

    .help-flow{
        flex-direction:column;
    }

    .help-flow-arrow{
        display:none;
    }

    .help-flow-step{
        width:100%;
    }

    .flow{
        flex-direction:column;
    }

    .flow-arrow{
        display:none;
    }

    .flow-step{
        width:100%;
    }

    .help-modal{
        padding:8px;
    }

    .help-modal-box{
        margin:4px auto;
        border-radius:14px;
    }

    .help-modal-head{
        padding:14px;
    }

    .help-modal-content{
        padding:12px;
    }
}

@media(max-width:520px){
    .wrap{
        padding-left:8px;
        padding-right:8px;
    }

    .card{
        padding:12px;
    }

    .grid{
        grid-template-columns:1fr 1fr;
    }

    .stat{
        padding:12px;
    }

    .stat strong{
        font-size:22px;
    }

    input,
    select,
    textarea,
    button,
    .btn{
        min-height:44px;
    }

    /* Forms become single-column on small phones. */
    form{
        width:100%;
    }

    form > input:not([type="hidden"]),
    form > select,
    form > textarea{
        width:100%;
        margin:4px 0;
    }

    form > button,
    form > .btn{
        width:100%;
        margin:5px 0;
    }

    .login{
        margin:18px auto;
    }

    .login-help,
    .login-help-button{
        position:static;
        display:block;
        width:max-content;
        margin:0 0 12px auto;
    }

    .help-page{
        padding:0;
        margin:8px auto;
    }

    .help-hero{
        border-radius:12px;
        padding:16px;
    }

    .help-section{
        padding:15px;
    }
}

@media(max-width:360px){
    .grid{
        grid-template-columns:1fr;
    }

    .nav-brand{
        font-size:16px;
    }

    .card{
        padding:10px;
    }
}

/* Respect users who prefer less motion. */
@media(prefers-reduced-motion:reduce){
    *,
    *::before,
    *::after{
        scroll-behavior:auto !important;
        transition:none !important;
        animation:none !important;
    }
}


/* Login page: polished responsive layout for phones, tablets and laptops */
.login-page{
    min-height:calc(100vh - 24px);
    display:flex;
    align-items:center;
    justify-content:center;
    padding:28px 16px 40px;
}
.login-shell{
    width:min(100%, 560px);
}
.login.card{
    position:relative;
    width:100%;
    margin:0;
    padding:32px;
    border:1px solid var(--border);
    border-radius:20px;
    box-shadow:0 12px 35px rgba(15,23,42,.12);
    background:var(--card);
}
.login-logo{
    width:68px;
    height:68px;
    margin:0 auto 14px;
    display:grid;
    place-items:center;
    border-radius:18px;
    background:linear-gradient(135deg,var(--primary),var(--primary2));
    color:#fff;
    font-size:34px;
    box-shadow:0 8px 20px rgba(18,60,105,.22);
}
.login-title{
    margin:0;
    text-align:center;
    font-size:clamp(25px,5vw,34px);
    line-height:1.15;
}
.login-subtitle{
    margin:9px auto 24px;
    max-width:420px;
    text-align:center;
    color:var(--muted);
    font-size:15px;
}
.login-form{
    display:grid;
    gap:13px;
}
.login-form input{
    width:100%;
    min-height:48px;
    margin:0;
    font-size:16px;
    border-radius:11px;
}
.login-password{
    position:relative;
}
.login-password input{
    width:100%;
    box-sizing:border-box;
    padding-right:90px;
}
.login-password .password-toggle{
    position:absolute;
    right:6px;
    top:6px;
    width:auto !important;
    min-width:58px;
    max-width:82px;
    min-height:36px;
    height:36px;
    padding:0 12px;
    margin:0;
    border-radius:8px;
    font-size:14px;
    line-height:1;
    z-index:2;
}
.login-submit{
    width:100%;
    min-height:48px;
    margin:0;
    font-size:16px;
    font-weight:700;
    border-radius:11px;
}
.login-help-note{
    margin:17px 0 0;
    text-align:center;
    color:var(--muted);
    font-size:14px;
}
.login-divider{
    height:1px;
    margin:24px 0;
    background:var(--border);
    border:0;
}
.login-actions{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:10px;
}
.login-actions .btn{
    width:100%;
    min-height:46px;
    margin:0;
    display:flex;
    align-items:center;
    justify-content:center;
    text-align:center;
    line-height:1.25;
    padding:10px 12px;
    border-radius:10px;
}
.login-demo{
    margin-top:20px;
    padding:14px 16px;
    border:1px solid var(--border);
    border-radius:12px;
    background:#f8fafc;
}
.login-demo-title{
    margin:0 0 9px;
    font-weight:700;
}
.login-demo-row{
    display:flex;
    justify-content:space-between;
    gap:12px;
    padding:7px 0;
    border-top:1px solid #e7ecf2;
    font-size:14px;
}
.login-demo-row:first-of-type{border-top:0}
.login-demo code{
    padding:3px 7px;
    border-radius:6px;
    background:#eef2f7;
    font-size:13px;
}
.login-help-button{
    position:absolute;
    top:18px;
    right:18px;
    min-height:38px;
    padding:7px 12px;
    margin:0;
    border-radius:9px;
    font-size:14px;
}
@media(max-width:700px){
    .login-page{padding:18px 12px 28px;align-items:flex-start}
    .login-password .password-toggle{width:auto !important;min-width:58px;max-width:82px;}
    .login-shell{padding-top:18px}
    .login.card{padding:26px 18px 20px;border-radius:16px}
    .login-logo{width:58px;height:58px;font-size:29px;border-radius:15px}
    .login-title{font-size:27px;padding-top:18px}
    .login-subtitle{font-size:14px;margin-bottom:20px}
    .login-actions{grid-template-columns:1fr}
    .login-actions .btn{min-height:46px}
    .login-demo-row{align-items:flex-start;flex-direction:column;gap:4px}
    .login-help-button{top:12px;right:12px}
}
@media(max-width:380px){
    .login-page{padding-left:8px;padding-right:8px}
    .login.card{padding-left:14px;padding-right:14px}
    .login-title{font-size:24px}
}
</style>

<script>
function filterTable(inputId, tableId){
    const input = document.getElementById(inputId);
    const table = document.getElementById(tableId);
    if(!input || !table) return;

    const q = input.value.toLowerCase();

    table.querySelectorAll('tbody tr').forEach(r=>{
        r.style.display = r.innerText.toLowerCase().includes(q) ? '' : 'none';
    });
}

function markAll(status){
    document.querySelectorAll('select.att-status').forEach(s => s.value = status);
}

function confirmDelete(){
    return confirm('Are you sure you want to delete this record?');
}

function togglePassword(inputId, button){
    const input = document.getElementById(inputId);
    if(!input || !button) return;

    if(input.type === 'password'){
        input.type = 'text';
        button.innerText = 'Hide';
        button.setAttribute('aria-label', 'Hide password');
    }else{
        input.type = 'password';
        button.innerText = 'Show';
        button.setAttribute('aria-label', 'Show password');
    }
}

function openHelp(){
    const modal = document.getElementById('loginHelpModal');
    if(modal){
        modal.classList.add('open');
        document.body.style.overflow='hidden';
    }
}

function closeHelp(){
    const modal = document.getElementById('loginHelpModal');
    if(modal){
        modal.classList.remove('open');
        document.body.style.overflow='';
    }
}

function toggleMobileMenu(){
    const nav = document.getElementById('mainNav');
    const button = document.getElementById('mobileMenuButton');
    if(!nav || !button) return;

    const open = nav.classList.toggle('menu-open');
    if(open){
        nav.classList.remove('nav-hidden');
    }
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.innerText = open ? '✕' : '☰';
    document.body.classList.toggle('menu-open', open);
}

function closeMobileMenu(){
    const nav = document.getElementById('mainNav');
    const button = document.getElementById('mobileMenuButton');
    if(!nav || !button) return;

    nav.classList.remove('menu-open');
    button.setAttribute('aria-expanded', 'false');
    button.innerText = '☰';
    document.body.classList.remove('menu-open');
}

document.addEventListener('keydown', function(e){
    if(e.key === 'Escape'){
        closeHelp();
        closeMobileMenu();
    }
});

document.addEventListener('click', function(e){
    const nav = document.getElementById('mainNav');
    if(!nav || !nav.classList.contains('menu-open')) return;

    if(e.target.closest('a')){
        closeMobileMenu();
    }
});

/* ============================================================
   MOBILE NAVIGATION SCROLL BEHAVIOR
   - Scroll DOWN: hide the top navigation.
   - Scroll UP: show the top navigation.
   - Near the top: always show it.
   - Desktop/tablet: keep normal sticky navigation.
   - Never hide the navigation while the hamburger menu is open.
   ============================================================ */
(function(){
    let lastScrollY = window.scrollY || 0;
    let ticking = false;
    const SHOW_AT_TOP = 12;
    const MIN_DELTA = 6;

    function updateMobileNav(){
        const nav = document.getElementById('mainNav');
        if(!nav){
            ticking = false;
            return;
        }

        const currentY = window.scrollY || window.pageYOffset || 0;

        /* Do not use hide-on-scroll on larger screens. */
        if(window.innerWidth > 900){
            nav.classList.remove('nav-hidden');
            lastScrollY = currentY;
            ticking = false;
            return;
        }

        /* Keep nav visible at the very top. */
        if(currentY <= SHOW_AT_TOP){
            nav.classList.remove('nav-hidden');
            lastScrollY = currentY;
            ticking = false;
            return;
        }

        /* Keep it visible while the mobile menu is open. */
        if(nav.classList.contains('menu-open')){
            nav.classList.remove('nav-hidden');
            lastScrollY = currentY;
            ticking = false;
            return;
        }

        const delta = currentY - lastScrollY;

        if(delta > MIN_DELTA){
            /* Scrolling down */
            nav.classList.add('nav-hidden');
        }else if(delta < -MIN_DELTA){
            /* Scrolling up */
            nav.classList.remove('nav-hidden');
        }

        lastScrollY = currentY;
        ticking = false;
    }

    window.addEventListener('scroll', function(){
        if(!ticking){
            window.requestAnimationFrame(updateMobileNav);
            ticking = true;
        }
    }, {passive:true});

    window.addEventListener('resize', function(){
        const nav = document.getElementById('mainNav');
        if(window.innerWidth > 900 && nav){
            nav.classList.remove('nav-hidden');
        }
        lastScrollY = window.scrollY || 0;
    }, {passive:true});
})();

window.addEventListener('resize', function(){
    if(window.innerWidth > 900){
        closeMobileMenu();
    }
});
</script>
</head>

<body>

{% if me %}
<nav id="mainNav" aria-label="Main navigation">

    <div class="nav-brand">🏫 JNV Attendance</div>

    <button
        id="mobileMenuButton"
        class="nav-toggle"
        type="button"
        aria-label="Open navigation menu"
        aria-expanded="false"
        onclick="toggleMobileMenu()"
    >☰</button>

    <div class="nav-links">

        <a href="{{url_for('dashboard')}}">Dashboard</a>

        {% if me.role == 'principal' %}

        <a href="{{url_for('manage_teachers')}}">Teachers</a>
        <a href="{{url_for('manage_classes')}}">Classes</a>
        <a href="{{url_for('manage_subjects')}}">Subjects</a>
        <a href="{{url_for('manage_assignments')}}">Assignments</a>
        <a href="{{url_for('principal_mod_management')}}">🛡️ MOD</a>
        <a href="{{url_for('principal_mod_report')}}">MOD Attendance</a>
        <a href="{{url_for('students_page')}}">Students</a>

        <span class="menu-title">📊 Teacher Monitoring</span>
        <a href="{{url_for('principal_period_report')}}">Period Attendance</a>
        <a href="{{url_for('principal_daily_report')}}">Daily Attendance</a>
        <a href="{{url_for('principal_assembly_report')}}">Morning Assembly</a>
        <a href="{{url_for('principal_hostel_report')}}">Hostel / Night</a>
        <a href="{{url_for('principal_pt_report')}}">Morning PT</a>
        <a href="{{url_for('principal_remedial_report')}}">Remedial</a>
        <a href="{{url_for('principal_evening_student_report')}}">Evening Student</a>
        <a href="{{url_for('principal_evening_games_report')}}">Evening Games</a>

        <a href="{{url_for('staff_attendance')}}">Staff</a>
        <a href="{{url_for('leave_management')}}">Leave</a>
        <a href="{{url_for('medical_management')}}">Medical</a>

        <a href="{{url_for('attendance_reports')}}">All Reports</a>
        <a href="{{url_for('house_wise_report')}}">House-wise</a>
        <a href="{{url_for('principal_report')}}">Class-wise</a>
        <a href="{{url_for('student_report')}}">Student Report</a>
        <a href="{{url_for('monthly_report')}}">Monthly</a>
        <a href="{{url_for('low_attendance')}}">Low Attendance</a>

        <a href="{{url_for('export_all_attendance')}}">CSV Export</a>
        <a href="{{url_for('backup_data')}}">Backup</a>

        {% elif me.role == 'mod' %}

        <a href="{{url_for('mod_dashboard')}}">MOD Dashboard</a>
        <a href="{{url_for('mod_attendance', module='morning_pt')}}">Morning PT</a>
        <a href="{{url_for('mod_attendance', module='assembly')}}">Assembly</a>
        <a href="{{url_for('mod_attendance', module='mess')}}">Mess</a>
        <a href="{{url_for('mod_attendance', module='remedial')}}">Remedial</a>
        <a href="{{url_for('mod_attendance', module='evening_games')}}">Evening Games</a>
        <a href="{{url_for('mod_attendance', module='evening_study')}}">Evening Study</a>
        <a href="{{url_for('mod_attendance', module='night')}}">Night Attendance</a>

        {% else %}

        <a href="{{url_for('attendance')}}">Period Attendance</a>
        <a href="{{url_for('daily_attendance')}}">Daily Attendance</a>
        <a href="{{url_for('assembly_attendance')}}">Assembly</a>
        <a href="{{url_for('hostel_attendance')}}">Hostel/Night</a>
        <a href="{{url_for('mess_attendance')}}">Mess</a>

        <a href="{{url_for('special_attendance', kind='morning_pt')}}">Morning PT</a>
        <a href="{{url_for('special_attendance', kind='remedial')}}">Remedial</a>
        <a href="{{url_for('special_attendance', kind='evening_student')}}">Evening Student</a>
        <a href="{{url_for('special_attendance', kind='evening_games')}}">Evening Games</a>

        <a href="{{url_for('leave_management')}}">Leave/Permission</a>
        <a href="{{url_for('medical_management')}}">Medical</a>
        <a href="{{url_for('my_attendance')}}">My History</a>
        <a href="{{url_for('students_page')}}">My Students</a>

        {% endif %}

        <a href="{{url_for('change_password')}}">Password</a>
        <a href="{{url_for('logout')}}">Logout</a>

    </div>
</nav>
{% endif %}

<div class="wrap">

{% with messages=get_flashed_messages() %}
{% for m in messages %}
<div class="alert">{{m}}</div>
{% endfor %}
{% endwith %}

{{ content|safe }}

</div>

</body>
</html>
"""


def page(title, body):

    return render_template_string(
        BASE,
        title=title,
        me=current_user(),
        content=body
    )


# ============================================================
# HELP / ABOUT
# ============================================================

@app.route("/help")
@app.route("/help/")
@app.route("/help-center")
@app.route("/help-center/")
def help_page():
    body = """
    <div class="help-page">
        <div class="help-hero">
            <a class="btn gray" href="/" style="float:right">← Back to Login</a>
            <h1>JNV Attendance Management System — Help Center</h1>
            <p>A complete English guide to the application's purpose, user roles, attendance modules, reporting workflow, security features and support contact.</p>
            <p>Designed for practical school attendance management, teacher submission and Principal monitoring.</p>
        </div>

        <div class="help-grid">
            <div class="help-section">
                <h2>1. About the Application</h2>
                <p>The <b>JNV Attendance Management System</b> is a web-based attendance platform for Jawahar Navodaya Vidyalaya. It brings student, teacher/staff and special-routine attendance into one system.</p>
                <ul>
                    <li>Daily student attendance</li>
                    <li>Period and subject attendance</li>
                    <li>Morning Assembly and Morning PT</li>
                    <li>Hostel / Night attendance</li>
                    <li>Mess attendance</li>
                    <li>Remedial and evening activities</li>
                    <li>Leave, medical and late/early records</li>
                    <li>Teacher/staff attendance</li>
                    <li>Reports, monthly summaries and low attendance</li>
                    <li>CSV export and data backup</li>
                </ul>
            </div>

            <div class="help-section">
                <h2>2. User Roles</h2>
                <p><b>Principal</b> — Manages teachers, classes, subjects and assignments; monitors teacher-submitted attendance; manages MOD duties; views reports; exports data and creates backups.</p>
                <p><b>Teacher</b> — Enters attendance for assigned classes/subjects and submits routine attendance. Teachers can view their own attendance history and assigned students.</p>
                <p><b>MOD</b> — Uses the temporary MOD account created by the Principal for the assigned duty date and records designated routine attendance modules.</p>
            </div>

            <div class="help-section">
                <h2>3. Login Process</h2>
                <ol>
                    <li>Open the application login page.</li>
                    <li>Enter your assigned username.</li>
                    <li>Enter your password.</li>
                    <li>Use <b>Show</b> to verify the password when required.</li>
                    <li>Click <b>Login</b>.</li>
                    <li>The system opens the dashboard according to your role.</li>
                </ol>
                <p class="small">Never share your password with another user.</p>
            </div>

            <div class="help-section">
                <h2>4. Attendance Workflow</h2>
                <p>Teachers enter and submit attendance. The Principal monitoring area is designed to review teacher-submitted data rather than re-enter it.</p>
                <div class="flow">
                    <div class="flow-step">Teacher Login</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Select Class / Module</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Mark Attendance</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Save / Submit</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Principal Monitoring</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Reports / Export</div>
                </div>
            </div>

            <div class="help-section">
                <h2>5. Principal Flow</h2>
                <div class="flow">
                    <div class="flow-step">Principal Login</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Manage Teachers</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Manage Classes & Subjects</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Assign Teachers</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Monitor Submissions</div><div class="flow-arrow">→</div>
                    <div class="flow-step">Reports / Backup</div>
                </div>
            </div>

            <div class="help-section">
                <h2>6. MOD Flow</h2>
                <ol>
                    <li>Principal creates a MOD duty account for a teacher.</li>
                    <li>The system creates temporary MOD credentials.</li>
                    <li>When Gmail is configured, credentials can be emailed to the teacher.</li>
                    <li>The MOD logs in through the separate MOD Login.</li>
                    <li>MOD records the assigned routine attendance.</li>
                    <li>MOD access is valid for the assigned duty date.</li>
                    <li>Principal can monitor MOD attendance reports.</li>
                </ol>
            </div>

            <div class="help-section">
                <h2>7. Attendance Modules</h2>
                <ul>
                    <li><b>Period Attendance:</b> subject/period-wise student attendance.</li>
                    <li><b>Daily Attendance:</b> daily student status.</li>
                    <li><b>Morning Assembly:</b> assembly attendance.</li>
                    <li><b>Hostel / Night:</b> residential night attendance.</li>
                    <li><b>Mess:</b> meal-related attendance records.</li>
                    <li><b>Morning PT:</b> morning physical training attendance.</li>
                    <li><b>Remedial:</b> remedial class attendance.</li>
                    <li><b>Evening Student:</b> evening student attendance.</li>
                    <li><b>Evening Games:</b> games/activity attendance.</li>
                    <li><b>Medical / Leave:</b> student leave and medical records.</li>
                    <li><b>Late / Early Leave:</b> late arrival and early-leave records.</li>
                </ul>
            </div>

            <div class="help-section">
                <h2>8. Reports & Data</h2>
                <ul>
                    <li>Class-wise attendance</li>
                    <li>Student attendance history</li>
                    <li>Monthly reports</li>
                    <li>Low-attendance student list</li>
                    <li>House-wise reports</li>
                    <li>Teacher submission monitoring</li>
                    <li>MOD attendance reports</li>
                    <li>CSV attendance export</li>
                    <li>Full application data backup</li>
                </ul>
            </div>

            <div class="help-section">
                <h2>9. Password & Security</h2>
                <ul>
                    <li>Use a private password that other users cannot guess.</li>
                    <li>Do not share login credentials.</li>
                    <li>Use the <b>Password</b> menu after login to change your password.</li>
                    <li>The login page includes a <b>Show / Hide</b> password control.</li>
                    <li>Keep backups in a secure location.</li>
                </ul>
                <p class="small">The application stores its application data in JSON files and can optionally use Google Drive when the configured Google Drive settings are enabled.</p>
            </div>

            <div class="help-section">
                <h2>10. About the Developer</h2>
                <div class="contact-card">
                    <p><b>Name:</b> Ramchandra Raghunath Sakore</p>
                    <p><b>Role:</b> AI Teacher & Application Developer</p>
                    <p><b>Experience:</b> 3+ years</p>
                    <p><b>Mobile:</b> 8262848220</p>
                    <p><b>Email:</b> rsakore034@gmail.com</p>
                </div>
                <p>Ramchandra Raghunath Sakore is an AI-focused educator and application developer who combines teaching, educational technology and practical software development. His goal is to use AI and digital tools to simplify repetitive academic and administrative work and make technology useful for teachers and students.</p>
            </div>

            <div class="help-section">
                <h2>11. Support</h2>
                <p>For application questions, login issues, attendance workflow questions or feature suggestions, use the developer contact details below.</p>
                <div class="contact-card">
                    <p><b>Ramchandra Raghunath Sakore</b></p>
                    <p>AI Teacher & Application Developer</p>
                    <p>📞 8262848220</p>
                    <p>✉️ rsakore034@gmail.com</p>
                </div>
            </div>
        </div>

        <div class="help-section" style="margin-top:18px">
            <h2>Application Flow Chart</h2>
            <div class="flow">
                <div class="flow-step">🏫 JNV System</div><div class="flow-arrow">→</div>
                <div class="flow-step">🔐 Login</div><div class="flow-arrow">→</div>
                <div class="flow-step">👤 Principal / Teacher / MOD</div><div class="flow-arrow">→</div>
                <div class="flow-step">📝 Attendance Entry</div><div class="flow-arrow">→</div>
                <div class="flow-step">💾 Save Data</div><div class="flow-arrow">→</div>
                <div class="flow-step">👁 Principal Monitoring</div><div class="flow-arrow">→</div>
                <div class="flow-step">📊 Reports</div><div class="flow-arrow">→</div>
                <div class="flow-step">⬇ Export / 💾 Backup</div>
            </div>
        </div>

        <div class="help-footer">JNV Attendance Management System · Help Center · Developed by Ramchandra Raghunath Sakore</div>
    </div>
    """
    return page("Help & About", body)


# ============================================================
# LOGIN
# ============================================================

@app.route("/", methods=["GET", "POST"])
def login():

    if current_user():

        return redirect(
            url_for("dashboard")
        )

    if request.method == "POST":

        username = (
            request.form
            .get("username", "")
            .strip()
        )

        password = request.form.get(
            "password",
            ""
        )

        u = next(
            (
                x for x in users()
                if x.get("username") == username
                and x.get("password") == password
            ),
            None
        )

        if u:

            session["user_id"] = u["id"]

            if u.get("role") == "mod":
                if mod_current_user():
                    return redirect(url_for("mod_dashboard"))
                session.clear()
                flash("यह MOD login आज valid नहीं है.")
                return redirect(url_for("mod_login"))

            return redirect(
                url_for("dashboard")
            )

        flash(
            "Username या password गलत है."
        )

    body = """

    <main class="login-page">
      <div class="login-shell">
        <div class="card login">

            <button type="button" class="login-help-button" onclick="openHelp()" title="Open Help Center" aria-label="Open Help Center">
                ❓ Help
            </button>

            <div class="login-logo" aria-hidden="true">🏫</div>
            <h1 class="login-title">JNV Attendance</h1>
            <p class="login-subtitle">Jawahar Navodaya Vidyalaya<br>Attendance Management System</p>

            <form method="post" class="login-form">
                <input name="username" placeholder="Username" autocomplete="username" required aria-label="Username">

                <div class="login-password">
                    <input id="loginPassword" name="password" type="password" placeholder="Password" autocomplete="current-password" required aria-label="Password">
                    <button type="button" class="password-toggle" onclick="togglePassword('loginPassword', this)" aria-label="Show or hide password">Show</button>
                </div>

                <button class="login-submit" type="submit">Login</button>
            </form>

            <p class="login-help-note">Need help? Tap <b>❓ Help</b> above for instructions.</p>

            <hr class="login-divider">

            <div class="login-actions">
                <a class="btn purple" href="{{ url_for('mod_login') }}">🛡️ MOD Login</a>
                <a class="btn gray" href="{{ url_for('forgot_password') }}">🔐 Forgot Password</a>
                <a class="btn" href="{{ url_for('admin_login') }}">🛠️ Admin Login</a>
            </div>

            <div class="login-demo">
                <p class="login-demo-title">Demo Teacher Accounts</p>
                <div class="login-demo-row"><b>Teacher 1</b><code>teacher1 / teacher123</code></div>
                <div class="login-demo-row"><b>Teacher 2</b><code>teacher2 / teacher123</code></div>
            </div>

        </div>
      </div>
    </main>

    <div id="loginHelpModal" class="help-modal" onclick="if(event.target === this){ closeHelp(); }">
        <div class="help-modal-box">
            <div class="help-modal-head">
                <h2>JNV Attendance Management System — Help Center</h2>
                <button type="button" class="help-close" onclick="closeHelp()">Close ✕</button>
            </div>
            <div class="help-modal-content">
                <p><b>Welcome.</b> This Help Center explains the application, its modules, user roles, workflow, reports, security and developer support details.</p>

                <div class="help-mini-grid">
                    <div class="help-mini-card">
                        <h3>1. About the Application</h3>
                        <p>The JNV Attendance Management System is a web-based school attendance platform designed for Jawahar Navodaya Vidyalaya. It combines student attendance, special-routine attendance, teacher submissions, Principal monitoring and reporting in one system.</p>
                        <ul>
                            <li>Daily student attendance</li>
                            <li>Period / subject attendance</li>
                            <li>Morning Assembly and Morning PT</li>
                            <li>Hostel / Night and Mess attendance</li>
                            <li>Remedial, Evening Student and Evening Games</li>
                            <li>Medical, Leave, Late / Early Leave</li>
                            <li>Teacher / Staff attendance</li>
                            <li>Reports, CSV export and backup</li>
                        </ul>
                    </div>

                    <div class="help-mini-card">
                        <h3>2. User Roles</h3>
                        <p><b>Principal:</b> Manages teachers, classes, subjects and assignments; monitors teacher-submitted attendance; manages MOD duty; views reports; exports data and creates backups.</p>
                        <p><b>Teacher:</b> Records attendance for assigned classes and subjects, submits routine attendance and views assigned student/history information.</p>
                        <p><b>MOD:</b> Uses the temporary MOD account created by the Principal for the assigned duty date and records designated routine attendance modules.</p>
                    </div>

                    <div class="help-mini-card">
                        <h3>3. Login & Password</h3>
                        <ol>
                            <li>Enter your assigned username.</li>
                            <li>Enter your password.</li>
                            <li>Press <b>Show</b> to view the password when needed.</li>
                            <li>Press <b>Login</b>.</li>
                            <li>You are taken to the dashboard for your role.</li>
                        </ol>
                        <p><b>Important:</b> Never share your password with another person.</p>
                    </div>

                    <div class="help-mini-card">
                        <h3>4. Attendance Workflow</h3>
                        <div class="help-flow">
                            <div class="help-flow-step">Teacher Login</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Select Class / Module</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Mark Attendance</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Save / Submit</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Principal Monitoring</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Reports / Export</div>
                        </div>
                    </div>

                    <div class="help-mini-card">
                        <h3>5. Principal Flow</h3>
                        <div class="help-flow">
                            <div class="help-flow-step">Principal Login</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Manage Teachers</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Classes & Subjects</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Assign Teachers</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Monitor Submissions</div><div class="help-flow-arrow">→</div>
                            <div class="help-flow-step">Reports / Backup</div>
                        </div>
                    </div>

                    <div class="help-mini-card">
                        <h3>6. MOD Flow</h3>
                        <ol>
                            <li>Principal creates a MOD duty account for a teacher.</li>
                            <li>The system creates temporary credentials.</li>
                            <li>When Gmail is configured, the credentials can be sent to the teacher.</li>
                            <li>The teacher signs in through MOD Login.</li>
                            <li>MOD records the assigned routine attendance.</li>
                            <li>MOD access is valid for the assigned duty date.</li>
                            <li>Principal can review MOD attendance reports.</li>
                        </ol>
                    </div>

                    <div class="help-mini-card">
                        <h3>7. Attendance Modules</h3>
                        <ul>
                            <li>Period / Subject Attendance</li>
                            <li>Daily Attendance</li>
                            <li>Morning Assembly</li>
                            <li>Hostel / Night</li>
                            <li>Mess</li>
                            <li>Morning PT</li>
                            <li>Remedial</li>
                            <li>Evening Student</li>
                            <li>Evening Games</li>
                            <li>Medical / Sick Leave</li>
                            <li>Leave / Permission</li>
                            <li>Late / Early Leave</li>
                            <li>Teacher / Staff Attendance</li>
                        </ul>
                    </div>

                    <div class="help-mini-card">
                        <h3>8. Reports & Data</h3>
                        <ul>
                            <li>Class-wise reports</li>
                            <li>Student attendance history</li>
                            <li>Monthly reports</li>
                            <li>Low-attendance list</li>
                            <li>House-wise reports</li>
                            <li>Teacher submission monitoring</li>
                            <li>MOD attendance reports</li>
                            <li>CSV export</li>
                            <li>Complete data backup</li>
                        </ul>
                    </div>

                    <div class="help-mini-card">
                        <h3>9. Security & Storage</h3>
                        <ul>
                            <li>Use a private password.</li>
                            <li>Do not share login credentials.</li>
                            <li>Use the Password menu after login to change your password.</li>
                            <li>The login password field has a Show / Hide control.</li>
                            <li>Keep data backups secure.</li>
                            <li>Application data is stored in JSON files and can optionally be mirrored to Google Drive when configured.</li>
                        </ul>
                    </div>

                    <div class="help-mini-card">
                        <h3>10. Developer & Support</h3>
                        <div class="help-contact">
                            <p><b>Name:</b> Ramchandra Raghunath Sakore</p>
                            <p><b>Role:</b> AI Teacher & Application Developer</p>
                            <p><b>Experience:</b> 3+ years</p>
                            <p><b>Mobile:</b> 8262848220</p>
                            <p><b>Email:</b> rsakore034@gmail.com</p>
                        </div>
                        <p>Ramchandra Raghunath Sakore is an AI-focused educator and application developer who combines teaching, educational technology and practical software development. His work focuses on using AI and digital tools to simplify academic and administrative tasks and make technology practical for teachers and students.</p>
                    </div>
                </div>

                <div class="help-mini-card" style="margin-top:14px">
                    <h3>Complete Application Flow Chart</h3>
                    <div class="help-flow">
                        <div class="help-flow-step">🏫 JNV System</div><div class="help-flow-arrow">→</div>
                        <div class="help-flow-step">🔐 Login</div><div class="help-flow-arrow">→</div>
                        <div class="help-flow-step">👤 Principal / Teacher / MOD</div><div class="help-flow-arrow">→</div>
                        <div class="help-flow-step">📝 Attendance Entry</div><div class="help-flow-arrow">→</div>
                        <div class="help-flow-step">💾 Save Data</div><div class="help-flow-arrow">→</div>
                        <div class="help-flow-step">👁 Principal Monitoring</div><div class="help-flow-arrow">→</div>
                        <div class="help-flow-step">📊 Reports</div><div class="help-flow-arrow">→</div>
                        <div class="help-flow-step">⬇ Export / 💾 Backup</div>
                    </div>
                </div>

                <p class="center small" style="margin-top:16px">JNV Attendance Management System · Help Center · Developed by Ramchandra Raghunath Sakore</p>
            </div>
        </div>
    </div>

    """

    # Login body is intentionally kept as a normal HTML string.
    # Resolve Flask URLs here before sending it to page(); otherwise
    # Jinja expressions inside body would be shown literally in the browser.
    body = body.replace("{{ url_for('mod_login') }}", url_for("mod_login"))
    body = body.replace("{{ url_for('forgot_password') }}", url_for("forgot_password"))
    body = body.replace("{{ url_for('principal_register') }}", url_for("principal_register"))
    body = body.replace("{{ url_for('admin_login') }}", url_for("admin_login"))

    return page(
        "Login",
        body
    )


# ============================================================
# PRINCIPAL REGISTRATION + PASSWORD RESET
# ============================================================

@app.route("/principal-register", methods=["GET", "POST"])
@admin_required
def principal_register():
    return redirect(url_for("admin_create_principal"))


@app.route("/principal-register/verify", methods=["GET", "POST"])
@admin_required
def principal_verify_registration():
    return redirect(url_for("admin_verify_principal"))


# ============================================================
# ADMIN LOGIN / PRINCIPAL ACCOUNT MANAGEMENT
# ============================================================

@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    if admin_current_user():
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        username=request.form.get("username", "").strip()
        password=request.form.get("password", "")
        admin=next((a for a in admins() if a.get("username")==username and a.get("password")==password),None)
        if admin:
            session.pop("user_id", None)
            session["admin_id"]=admin.get("id")
            return redirect(url_for("admin_dashboard"))
        flash("Admin ID या password गलत है.")
    body=f'''
    <div class="card login" style="max-width:520px">
      <h1>🛠️ Admin Login</h1>
      <p class="small">यह Principal / Teacher / MOD login से अलग Admin login है.</p>
      <form method="post">
        <input name="username" placeholder="Admin ID" required autocomplete="username">
        <input name="password" type="password" placeholder="Admin Password" required autocomplete="current-password">
        <button style="width:100%">🔐 Admin Login</button>
      </form>
      <p class="center"><a class="btn gray" href="{url_for('login')}">← Back to Main Login</a></p>
    </div>'''
    return page("Admin Login", body)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    return redirect(url_for("login"))


@app.route("/admin")
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    principals=[u for u in users() if u.get("role")=="principal"]
    logs=safe_load("admin_credential_logs", [])
    principal_rows="".join(
        f"<tr><td>{u.get('name','')}</td><td>{u.get('username','')}</td><td>{u.get('email','')}</td><td>{u.get('school_name','')}</td><td>{u.get('district','')}</td><td><a class='btn gray' href='{url_for('admin_resend_credentials', user_id=u.get('id'))}'>📧 Resend Credentials</a></td></tr>"
        for u in principals
    ) or '<tr><td colspan="6">No Principal accounts created yet.</td></tr>'
    log_rows="".join(
        f"<tr><td>{x.get('principal_name','')}</td><td>{x.get('username','')}</td><td><code>{x.get('password','')}</code></td><td>{x.get('email','')}</td><td>{x.get('created_at','')}</td></tr>"
        for x in logs
    ) or '<tr><td colspan="5">No credential records yet.</td></tr>'
    body=f'''
    <div class="card">
      <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
        <div><h1>🛠️ Admin Panel</h1><p class="small">Principal accounts create/manage करो.</p></div>
        <div><a class="btn" href="{url_for('admin_create_principal')}">➕ Create Principal</a> <a class="btn gray" href="{url_for('admin_change_password')}">🔑 Change Admin Password</a> <a class="btn gray" href="{url_for('admin_logout')}">Logout</a></div>
      </div>
      <h2 style="margin-top:22px">Principal Accounts ({len(principals)})</h2>
      <div style="overflow:auto"><table>
      <thead><tr><th>Name</th><th>Username</th><th>Email</th><th>School</th><th>District</th><th>Actions</th></tr></thead>
      <tbody>{principal_rows}</tbody></table></div>
      <h2 style="margin-top:22px">Credential Records</h2>
      <p class="small">Admin ke paas banaye gaye Principal ke login credentials ka record saved रहेगा.</p>
      <div style="overflow:auto"><table><thead><tr><th>Principal</th><th>Username</th><th>Password</th><th>Email</th><th>Created</th></tr></thead><tbody>{log_rows}</tbody></table></div>
    </div>'''
    return page("Admin Panel", body)


@app.route("/admin/principal/create", methods=["GET", "POST"])
@admin_required
def admin_create_principal():
    # Admin creates Principal immediately. NO registration OTP is generated.
    if request.method == "POST":
        name = request.form.get("principal_name", "").strip()
        username = request.form.get("username", "").strip()
        email = normalize_email(request.form.get("email", ""))
        mobile = request.form.get("mobile", "").strip()
        school_name = request.form.get("school_name", "").strip()
        udise = request.form.get("udise", "").strip()
        district = request.form.get("district", "").strip()
        state = request.form.get("state", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        existing_username = next((u for u in users() if u.get("username") == username), None)
        existing_email = next((u for u in users() if normalize_email(u.get("email")) == email and u.get("role") == "principal"), None)
        if not all([name, username, school_name, district, state]) or not valid_email(email):
            flash("Principal name, username, valid email और school की जरूरी जानकारी भरें.")
        elif not re.fullmatch(r"[A-Za-z0-9_.@-]{4,40}", username):
            flash("Username 4-40 characters का रखें; केवल letters, numbers, _, ., @, - इस्तेमाल करें.")
        elif existing_username:
            flash("यह username पहले से मौजूद है. दूसरा username दें.")
        elif existing_email:
            flash("यह email पहले से किसी Principal account में registered है.")
        elif len(password) < 6:
            flash("Password कम से कम 6 characters का रखें.")
        elif password != confirm:
            flash("Password और confirm password अलग हैं.")
        else:
            us = users()
            new_user = {"id": next_id("U", us), "username": username, "password": password,
                        "role": "principal", "name": name, "email": email, "mobile": mobile,
                        "school_name": school_name, "udise": udise, "district": district, "state": state,
                        "email_verified": True, "created_by": admin_current_user().get("username"),
                        "created_at": datetime.now().isoformat(timespec="seconds")}
            us.append(new_user); save("users", us)
            logs = safe_load("admin_credential_logs", [])
            logs.append({"id": secrets.token_hex(16), "principal_user_id": new_user.get("id"),
                         "principal_name": name, "username": username, "password": password,
                         "email": email, "school_name": school_name,
                         "created_at": datetime.now().isoformat(timespec="seconds"),
                         "created_by": admin_current_user().get("username"),
                         "creation_method": "admin_direct_no_otp"})
            save("admin_credential_logs", logs)
            # Email is only a credential notification; it is not required for creation.
            ok, status = send_principal_credentials_email(email, name, username, password, school_name)
            if ok:
                flash("✅ Principal account तुरंत create हो गया. Login ID/password email पर भेज दिए गए हैं. कोई OTP नहीं भेजा गया.")
            else:
                flash(f"✅ Principal account तुरंत create हो गया. कोई OTP नहीं भेजा गया. Credentials email नहीं भेजा जा सका: {status}")
            return redirect(url_for("admin_dashboard"))
    body = f'''
    <div class="card login" style="max-width:760px">
      <h1>➕ Create Principal Account</h1>
      <p class="small" style="background:#eaf7ee;padding:12px;border-radius:8px"><b>Direct Admin Creation:</b> Admin details और password save करते ही Principal account तुरंत बन जाएगा. <b>Principal के email पर OTP नहीं जाएगा.</b> Gmail configured होने पर केवल Login ID और Password का notification email जाएगा.</p>
      <form method="post">
        <h3>Principal Information</h3>
        <input name="principal_name" placeholder="Principal Full Name" required>
        <input name="username" placeholder="Principal Login ID / Username" required>
        <input name="email" type="email" placeholder="Principal Email / Gmail" required>
        <input name="mobile" placeholder="Mobile Number">
        <h3>School Information</h3>
        <input name="school_name" placeholder="School Name" required>
        <input name="udise" placeholder="UDISE Code">
        <input name="district" placeholder="District" required>
        <input name="state" placeholder="State" required>
        <h3>Principal Login Password</h3>
        <input name="password" type="password" placeholder="Password (minimum 6 characters)" required autocomplete="new-password">
        <input name="confirm_password" type="password" placeholder="Confirm Password" required autocomplete="new-password">
        <button style="width:100%">✅ Create Principal Directly — No OTP</button>
      </form>
      <p class="center"><a class="btn gray" href="{url_for('admin_dashboard')}">← Back to Admin Panel</a></p>
    </div>'''
    return page("Create Principal", body)

# Backward-compatible route. New Admin creation never uses OTP verification.
@app.route("/admin/principal/verify", methods=["GET", "POST"])
@admin_required
def admin_verify_principal():
    flash("Principal verification OTP is disabled. Admin now creates Principal accounts directly.")
    return redirect(url_for("admin_create_principal"))


@app.route("/admin/change-password", methods=["GET", "POST"])
@admin_required
def admin_change_password():
    if request.method=="POST":
        new_username=request.form.get("new_username","").strip()
        old=request.form.get("old_password","")
        new=request.form.get("new_password","")
        confirm=request.form.get("confirm_password","")
        a=admin_current_user()
        duplicate=next((x for x in admins() if x.get("username")==new_username and x.get("id")!=a.get("id")),None)
        if not new_username:
            flash("New Admin ID खाली नहीं हो सकती.")
        elif duplicate:
            flash("यह Admin ID पहले से मौजूद है.")
        elif old!=a.get("password"): flash("Current Admin password गलत है.")
        elif len(new)<6: flash("New password कम से कम 6 characters का रखें.")
        elif new!=confirm: flash("New password और confirm password अलग हैं.")
        else:
            rows=admins(); target=next((x for x in rows if x.get("id")==a.get("id")),None)
            target["username"]=new_username; target["password"]=new; save("admins",rows)
            flash("✅ Admin ID और password successfully changed.")
            return redirect(url_for("admin_dashboard"))
    body=f'''
    <div class="card login" style="max-width:560px">
      <h1>🔐 Change Admin ID & Password</h1>
      <form method="post">
        <input name="new_username" placeholder="New Admin ID" required autocomplete="username">
        <input name="old_password" type="password" placeholder="Current Admin Password" required>
        <input name="new_password" type="password" placeholder="New Password (min 6 characters)" required autocomplete="new-password">
        <input name="confirm_password" type="password" placeholder="Confirm New Password" required autocomplete="new-password">
        <button style="width:100%">💾 Change Password</button>
      </form>
      <p class="center"><a class="btn gray" href="{url_for('admin_dashboard')}">← Back</a></p>
    </div>'''
    return page("Change Admin Password",body)


@app.route("/admin/principal/resend/<user_id>")
@admin_required
def admin_resend_credentials(user_id):
    principal=next((u for u in users() if u.get("id")==user_id and u.get("role")=="principal"),None)
    if not principal:
        flash("Principal account नहीं मिला.")
    else:
        ok,status=send_principal_credentials_email(principal.get("email"),principal.get("name","Principal"),principal.get("username"),principal.get("password"),principal.get("school_name"))
        flash(status if ok else f"Credentials resend failed: {status}")
    return redirect(url_for("admin_dashboard"))

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user():
        return redirect(url_for("dashboard"))
    if not principal_users():
        flash("अभी कोई Principal account मौजूद नहीं है. Admin से Principal account बनवाएं.")
        return redirect(url_for("admin_login"))
    if request.method == "POST":
        username=request.form.get("username", "").strip()
        email=normalize_email(request.form.get("email", ""))
        principal=principal_by_username(username)
        if not principal or not valid_email(email) or normalize_email(principal.get("email")) != email:
            flash("Principal username और registered email match नहीं करते.")
        else:
            otp=f"{secrets.randbelow(1000000):06d}"
            rows=cleanup_password_resets()
            rows=[x for x in rows if x.get("user_id") != principal.get("id")]
            rows.append({"id":secrets.token_hex(16),"user_id":principal.get("id"),"username":principal.get("username"),"email":email,"otp":otp,"expires_at":(datetime.now()+timedelta(minutes=10)).isoformat(timespec="seconds")})
            save("password_resets", rows)
            ok,status=send_principal_otp_email(email,principal.get("name","Principal"),otp,"reset")
            if ok:
                flash("OTP email पर भेज दिया गया है. अब OTP verify करके नया password सेट करें.")
                return redirect(url_for("reset_password"))
            save("password_resets", [x for x in rows if x.get("user_id") != principal.get("id")])
            flash(status)
    body=f"""
    <div class="card login">
      <h1>🔐 Principal Forgot Password</h1>
      <p class="small">Principal username और registered email डालें. OTP 10 मिनट valid रहेगा.</p>
      <form method="post">
        <input name="username" placeholder="Principal Username" required autocomplete="username">
        <input name="email" type="email" placeholder="Registered Principal Email" required autocomplete="email">
        <button style="width:100%">📧 Send OTP</button>
      </form>
      <p class="center"><a class="btn gray" href="{url_for('login')}">← Back to Login</a></p>
    </div>
    """
    return page("Forgot Password", body)


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    rows=cleanup_password_resets()
    if request.method == "POST":
        username=request.form.get("username", "").strip()
        otp=request.form.get("otp", "").strip()
        new_password=request.form.get("new_password", "")
        confirm=request.form.get("confirm_password", "")
        rec=next((x for x in rows if x.get("username")==username and x.get("otp")==otp),None)
        if not rec: flash("OTP गलत या expired है.")
        elif len(new_password)<6: flash("New password कम से कम 6 characters का रखें.")
        elif new_password!=confirm: flash("New password और confirm password अलग हैं.")
        else:
            us=users(); target=next((u for u in us if u.get("id")==rec.get("user_id") and u.get("role")=="principal"),None)
            if not target: flash("Principal account नहीं मिला.")
            else:
                target["password"]=new_password; save("users",us)
                save("password_resets",[x for x in rows if x.get("id")!=rec.get("id")])
                flash("Password successfully reset. अब नए password से login करें.")
                return redirect(url_for("login"))
    body=f"""
    <div class="card login">
      <h1>🔑 Reset Principal Password</h1>
      <form method="post">
        <input name="username" placeholder="Principal Username" required autocomplete="username">
        <input name="otp" placeholder="6-digit OTP" inputmode="numeric" maxlength="6" required>
        <input name="new_password" type="password" placeholder="New Password (min 6 characters)" required autocomplete="new-password">
        <input name="confirm_password" type="password" placeholder="Confirm New Password" required autocomplete="new-password">
        <button style="width:100%">🔐 Reset Password</button>
      </form>
      <p class="center"><a class="btn gray" href="{url_for('login')}">← Back to Login</a></p>
    </div>
    """
    return page("Reset Password", body)


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():

    u = current_user()

    if u and u.get("role") == "mod":
        return redirect(url_for("mod_dashboard"))

    st = students()

    today = str(date.today())

    daily = [
        r for r in daily_records()
        if r.get("date") == today
    ]

    period = [
        r for r in attendance_records()
        if r.get("date") == today
    ]

    leaves = [
        r for r in leave_records()
        if r.get("status") == "Approved"
    ]

    if u["role"] == "principal":

        present = sum(
            r.get("status") == "Present"
            for r in daily
        )

        absent = sum(
            r.get("status") == "Absent"
            for r in daily
        )

        late = sum(
            r.get("status") == "Late"
            for r in daily
        )

        total = len(daily)

        pct = percentage(
            present,
            total
        )

        stats = [

            ("Classes", len(classes())),

            ("Teachers", len(teachers())),

            ("Students", len(st)),

            ("Daily Present", present),

            ("Daily Absent", absent),

            ("Daily Late", late),

            ("Attendance %", f"{pct}%"),

            ("Period Records", len(period)),

            ("Approved Leaves", len(leaves)),

        ]

        rows = ""

        for c in classes():

            x = [
                r for r in daily
                if r.get("class_id")
                == c.get("id")
            ]

            p = sum(
                r.get("status") == "Present"
                for r in x
            )

            a = sum(
                r.get("status") == "Absent"
                for r in x
            )

            l = sum(
                r.get("status") == "Late"
                for r in x
            )

            q = percentage(
                p,
                len(x)
            )

            rows += f"""

            <tr>

            <td>
            {esc(class_name(c["id"]))}
            </td>

            <td>{len(x)}</td>

            <td>{p}</td>

            <td>{a}</td>

            <td>{l}</td>

            <td class="{status_class(q)}">
            {q}%
            </td>

            </tr>

            """

        extra = f"""

        <div class="card">

        <h2>
        Today's Daily Attendance
        —
        {today}
        </h2>

        <table>

        <thead>

        <tr>

        <th>Class</th>
        <th>Total</th>
        <th>Present</th>
        <th>Absent</th>
        <th>Late</th>
        <th>%</th>

        </tr>

        </thead>

        <tbody>
        {rows}
        </tbody>

        </table>

        </div>

        """

    else:

        t = teacher_profile(
            u["id"]
        )

        ats = [
            a for a in assignments()
            if t
            and a.get("teacher_id")
            == t["id"]
        ]

        cids = {
            a["class_id"]
            for a in ats
        }

        my = [
            r for r in attendance_records()
            if t
            and r.get("teacher_id")
            == t["id"]
        ]

        stats = [

            ("My Classes", len(cids)),

            (
                "My Subjects",
                len({
                    a["subject_id"]
                    for a in ats
                })
            ),

            (
                "My Students",
                len([
                    s for s in st
                    if s.get("class_id")
                    in cids
                ])
            ),

            ("My Period Records", len(my)),

        ]

        rows = ""

        for cid in cids:

            x = [
                r for r in my
                if r.get("class_id")
                == cid
            ]

            rows += f"""

            <tr>

            <td>
            {esc(class_name(cid))}
            </td>

            <td>{len(x)}</td>

            <td>
            {attendance_pct(
                class_id=cid,
                records=my
            )}%
            </td>

            </tr>

            """

        extra = f"""

        <div class="card">

        <h2>My Class Summary</h2>

        <table>

        <tr>
        <th>Class</th>
        <th>Records</th>
        <th>Present %</th>
        </tr>

        {rows}

        </table>

        </div>

        """

    if u["role"] == "principal":
        extra += f"""
        <div class="card">
          <h2>👁 Teacher Submission Monitoring</h2>
          <p class="small">Teachers submit attendance. Principal only monitors the submitted data module-wise.</p>
          <div class="toolbar">
            <a class="btn ok" href="{url_for('principal_period_report')}">Period Attendance</a>
            <a class="btn" href="{url_for('principal_daily_report')}">Daily Attendance</a>
            <a class="btn" href="{url_for('principal_assembly_report')}">Morning Assembly</a>
            <a class="btn" href="{url_for('principal_hostel_report')}">Hostel / Night</a>
            <a class="btn" href="{url_for('principal_pt_report')}">Morning PT</a>
            <a class="btn" href="{url_for('principal_remedial_report')}">Remedial</a>
            <a class="btn" href="{url_for('principal_evening_student_report')}">Evening Student</a>
            <a class="btn" href="{url_for('principal_evening_games_report')}">Evening Games</a>
          </div>
        </div>
        """

    if u["role"] == "principal":
        extra += principal_mod_credentials_card()

    cards = "".join(

        f"""
        <div class="stat">

        <span>{esc(k)}</span>

        <strong>{esc(v)}</strong>

        </div>
        """

        for k, v in stats
    )

    return page(

        "Dashboard",

        f"""

        <div class="card">

        <h1>
        Welcome,
        {esc(u["name"])}
        </h1>

        <div class="grid">
        {cards}
        </div>

        </div>

        {extra}

        """
    )


# ============================================================
# TEACHER MANAGEMENT
# ============================================================

@app.route(
    "/principal/teachers",
    methods=["GET", "POST"]
)
@principal_required
def manage_teachers():

    ts = teachers()
    us = users()

    if request.method == "POST":

        action = request.form.get(
            "action",
            "add"
        )

        if action == "delete":

            tid = request.form.get(
                "teacher_id"
            )

            t = next(
                (
                    x for x in ts
                    if x.get("id") == tid
                ),
                None
            )

            if t:

                ts = [
                    x for x in ts
                    if x.get("id") != tid
                ]

                us = [
                    x for x in us
                    if x.get("id")
                    != t.get("user_id")
                ]

                save("teachers", ts)
                save("users", us)

                flash(
                    "Teacher deleted."
                )

            return redirect(
                url_for("manage_teachers")
            )

        name = request.form.get(
            "name", ""
        ).strip()

        username = request.form.get(
            "username", ""
        ).strip()

        password = request.form.get(
            "password", ""
        ).strip()

        emp = request.form.get(
            "employee_id", ""
        ).strip()

        mobile = request.form.get(
            "mobile", ""
        ).strip()

        if not name or not username or not password:

            flash(
                "Name, username और password जरूरी हैं."
            )

        elif any(
            x.get("username") == username
            for x in us
        ):

            flash(
                "Username पहले से मौजूद है."
            )

        else:

            uid = next_id(
                "U",
                us
            )

            tid = next_id(
                "T",
                ts
            )

            us.append({

                "id": uid,
                "username": username,
                "password": password,
                "role": "teacher",
                "name": name

            })

            ts.append({

                "id": tid,
                "user_id": uid,
                "name": name,
                "employee_id": emp,
                "mobile": mobile

            })

            save("users", us)
            save("teachers", ts)

            flash(
                "Teacher account बनाया गया."
            )

        return redirect(
            url_for("manage_teachers")
        )

    rows = ""

    for t in ts:

        u = next(
            (
                x for x in us
                if x.get("id")
                == t.get("user_id")
            ),
            {}
        )

        rows += f"""

        <tr>

        <td>{esc(t.get("name",""))}</td>

        <td>{esc(u.get("username",""))}</td>

        <td>{esc(t.get("employee_id",""))}</td>

        <td>{esc(t.get("mobile",""))}</td>

        <td>

        <form method="post"
        onsubmit="return confirmDelete()">

        <input
        type="hidden"
        name="action"
        value="delete"
        >

        <input
        type="hidden"
        name="teacher_id"
        value="{esc(t["id"])}"
        >

        <button class="danger">
        Delete
        </button>

        </form>

        </td>

        </tr>

        """

    body = f"""

    <div class="card">

    <h1>Teacher / Staff Management</h1>

    <form method="post">

    <input
    name="name"
    placeholder="Teacher name"
    required
    >

    <input
    name="employee_id"
    placeholder="Employee ID"
    >

    <input
    name="mobile"
    placeholder="Mobile"
    >

    <input
    name="username"
    placeholder="Login username"
    required
    >

    <input
    name="password"
    placeholder="Password"
    required
    >

    <button>
    Add Teacher
    </button>

    </form>

    </div>

    <div class="card">

    <input
    id="tq"
    onkeyup="filterTable('tq','teacherTable')"
    placeholder="Search teacher..."
    >

    <table id="teacherTable">

    <thead>

    <tr>

    <th>Name</th>
    <th>Username</th>
    <th>Employee ID</th>
    <th>Mobile</th>
    <th>Action</th>

    </tr>

    </thead>

    <tbody>
    {rows}
    </tbody>

    </table>

    </div>

    """

    return page(
        "Teachers",
        body
    )


# ============================================================
# CLASS MANAGEMENT
# ============================================================

@app.route(
    "/principal/classes",
    methods=["GET", "POST"]
)
@principal_required
def manage_classes():

    cs = classes()

    if request.method == "POST":

        action = request.form.get(
            "action",
            "add"
        )

        if action == "delete":

            cid = request.form.get(
                "class_id"
            )

            if (
                any(
                    a.get("class_id") == cid
                    for a in assignments()
                )
                or
                any(
                    s.get("class_id") == cid
                    for s in students()
                )
            ):

                flash(
                    "Class has students/assignments. Delete those first."
                )

            else:

                cs = [
                    x for x in cs
                    if x.get("id") != cid
                ]

                save(
                    "classes",
                    cs
                )

                flash(
                    "Class deleted."
                )

            return redirect(
                url_for("manage_classes")
            )

        name = request.form.get(
            "name",
            ""
        ).strip()

        sec = request.form.get(
            "section",
            "A"
        ).strip()

        if name:

            cs.append({

                "id": next_id(
                    "C",
                    cs
                ),

                "name": name,

                "section": sec

            })

            save(
                "classes",
                cs
            )

            flash(
                "Class added."
            )

        return redirect(
            url_for("manage_classes")
        )

    rows = "".join(

        f"""

        <tr>

        <td>{esc(c["id"])}</td>

        <td>{esc(c["name"])}</td>

        <td>{esc(c.get("section",""))}</td>

        <td>

        <form method="post">

        <input
        type="hidden"
        name="action"
        value="delete"
        >

        <input
        type="hidden"
        name="class_id"
        value="{esc(c["id"])}"
        >

        <button class="danger">
        Delete
        </button>

        </form>

        </td>

        </tr>

        """

        for c in cs
    )

    return page(

        "Classes",

        f"""

        <div class="card">

        <h1>Class Management</h1>

        <form method="post">

        <input
        name="name"
        placeholder="Class name"
        required
        >

        <input
        name="section"
        value="A"
        placeholder="Section"
        >

        <button>
        Add Class
        </button>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>ID</th>
        <th>Class</th>
        <th>Section</th>
        <th>Action</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# SUBJECT MANAGEMENT
# ============================================================

@app.route(
    "/principal/subjects",
    methods=["GET", "POST"]
)
@principal_required
def manage_subjects():

    ss = subjects()

    if request.method == "POST":

        action = request.form.get(
            "action",
            "add"
        )

        if action == "delete":

            sid = request.form.get(
                "subject_id"
            )

            if any(
                a.get("subject_id") == sid
                for a in assignments()
            ):

                flash(
                    "Subject is assigned. Remove assignments first."
                )

            else:

                ss = [
                    x for x in ss
                    if x.get("id") != sid
                ]

                save(
                    "subjects",
                    ss
                )

                flash(
                    "Subject deleted."
                )

            return redirect(
                url_for("manage_subjects")
            )

        name = request.form.get(
            "name",
            ""
        ).strip()

        if name and not any(
            x.get("name", "").lower()
            == name.lower()
            for x in ss
        ):

            ss.append({

                "id": next_id(
                    "S",
                    ss
                ),

                "name": name

            })

            save(
                "subjects",
                ss
            )

            flash(
                "Subject added."
            )

        return redirect(
            url_for("manage_subjects")
        )

    rows = "".join(

        f"""

        <tr>

        <td>{esc(s["id"])}</td>

        <td>{esc(s["name"])}</td>

        <td>

        <form method="post">

        <input
        type="hidden"
        name="action"
        value="delete"
        >

        <input
        type="hidden"
        name="subject_id"
        value="{esc(s["id"])}"
        >

        <button class="danger">
        Delete
        </button>

        </form>

        </td>

        </tr>

        """

        for s in ss
    )

    return page(

        "Subjects",

        f"""

        <div class="card">

        <h1>Subject Management</h1>

        <form method="post">

        <input
        name="name"
        placeholder="Subject name"
        required
        >

        <button>
        Add Subject
        </button>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>ID</th>
        <th>Subject</th>
        <th>Action</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# ASSIGNMENTS
# ============================================================

@app.route(
    "/principal/assignments",
    methods=["GET", "POST"]
)
@principal_required
def manage_assignments():

    ats = assignments()

    if request.method == "POST":

        action = request.form.get(
            "action",
            "add"
        )

        if action == "delete":

            aid = request.form.get(
                "assignment_id"
            )

            ats = [
                x for x in ats
                if x.get("id") != aid
            ]

            save(
                "assignments",
                ats
            )

            flash(
                "Assignment removed."
            )

            return redirect(
                url_for("manage_assignments")
            )

        tid = request.form.get(
            "teacher_id"
        )

        cid = request.form.get(
            "class_id"
        )

        sid = request.form.get(
            "subject_id"
        )

        exists = any(

            a.get("teacher_id") == tid
            and a.get("class_id") == cid
            and a.get("subject_id") == sid

            for a in ats
        )

        if (
            tid
            and cid
            and sid
            and not exists
        ):

            ats.append({

                "id": next_id(
                    "TS",
                    ats
                ),

                "teacher_id": tid,
                "class_id": cid,
                "subject_id": sid

            })

            save(
                "assignments",
                ats
            )

            flash(
                "Assignment added."
            )

        return redirect(
            url_for("manage_assignments")
        )

    optsT = "".join(

        f"""
        <option value="{esc(t["id"])}">
        {esc(t["name"])}
        </option>
        """

        for t in teachers()
    )

    optsC = "".join(

        f"""
        <option value="{esc(c["id"])}">
        {esc(class_name(c["id"]))}
        </option>
        """

        for c in classes()
    )

    optsS = "".join(

        f"""
        <option value="{esc(s["id"])}">
        {esc(s["name"])}
        </option>
        """

        for s in subjects()
    )

    rows = ""

    for a in ats:

        rows += f"""

        <tr>

        <td>
        {esc(
            teacher_name(
                a["teacher_id"]
            )
        )}
        </td>

        <td>
        {esc(
            class_name(
                a["class_id"]
            )
        )}
        </td>

        <td>
        {esc(
            subject_name(
                a["subject_id"]
            )
        )}
        </td>

        <td>

        <form method="post">

        <input
        type="hidden"
        name="action"
        value="delete"
        >

        <input
        type="hidden"
        name="assignment_id"
        value="{esc(a["id"])}"
        >

        <button class="danger">
        Remove
        </button>

        </form>

        </td>

        </tr>

        """

    return page(

        "Assignments",

        f"""

        <div class="card">

        <h1>
        Teacher / Class / Subject Assignment
        </h1>

        <form method="post">

        <select name="teacher_id">
        {optsT}
        </select>

        <select name="class_id">
        {optsC}
        </select>

        <select name="subject_id">
        {optsS}
        </select>

        <button>
        Assign
        </button>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>Teacher</th>
        <th>Class</th>
        <th>Subject</th>
        <th>Action</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# STUDENT MANAGEMENT
# ============================================================

@app.route(
    "/students",
    methods=["GET", "POST"]
)
@login_required
def students_page():

    u = current_user()

    cs = classes()
    data = students()

    if u["role"] == "principal":

        visible_classes = {
            c.get("id")
            for c in cs
        }

    else:

        t = teacher_profile(
            u["id"]
        )

        visible_classes = {

            a.get("class_id")

            for a in assignments()

            if t
            and a.get("teacher_id")
            == t.get("id")

        }

    if request.method == "POST":

        if u["role"] != "principal":

            flash(
                "Only Principal can add/edit students."
            )

            return redirect(
                url_for("students_page")
            )

        action = request.form.get(
            "action",
            "add"
        )

        if action == "delete":

            sid = request.form.get(
                "student_id"
            )

            data = [
                x for x in data
                if x.get("id") != sid
            ]

            save(
                "students",
                data
            )

            flash(
                "Student deleted."
            )

            return redirect(
                url_for("students_page")
            )

        name = request.form.get(
            "name",
            ""
        ).strip()

        roll = request.form.get(
            "roll",
            ""
        ).strip()

        cid = request.form.get(
            "class_id"
        )

        mobile = request.form.get(
            "mobile",
            ""
        ).strip()

        admission = request.form.get(
            "admission_no",
            ""
        ).strip()

        father = request.form.get(
            "father_name",
            ""
        ).strip()

        mother = request.form.get(
            "mother_name",
            ""
        ).strip()

        gender = request.form.get(
            "gender",
            "Male"
        )

        house = request.form.get(
            "house",
            ""
        ).strip()

        hostel = request.form.get(
            "hostel",
            "Yes"
        )

        if not name or not roll or not cid:

            flash(
                "Name, Roll No और Class जरूरी हैं."
            )

        else:

            data.append({

                "id": next_id(
                    "ST",
                    data
                ),

                "name": name,

                "roll": roll,

                "class_id": cid,

                "section": next(
                    (
                        c.get("section", "")
                        for c in cs
                        if c.get("id") == cid
                    ),
                    ""
                ),

                "gender": gender,

                "mobile": mobile,

                "admission_no": admission,

                "father_name": father,

                "mother_name": mother,

                "house": house,

                "hostel": hostel,

                "status": "Active"

            })

            save(
                "students",
                data
            )

            flash(
                "Student added."
            )

        return redirect(
            url_for("students_page")
        )

    visible = [
        s for s in data
        if s.get("class_id")
        in visible_classes
    ]

    opts = "".join(

        f"""
        <option value="{esc(c["id"])}">
        {esc(class_name(c["id"]))}
        </option>
        """

        for c in cs
    )

    add = ""

    if u["role"] == "principal":

        add = f"""

        <form method="post">

        <input
        name="name"
        placeholder="Student name"
        required
        >

        <input
        name="roll"
        placeholder="Roll No"
        required
        >

        <select name="class_id">
        {opts}
        </select>

        <select name="gender">

        <option>Male</option>
        <option>Female</option>
        <option>Other</option>

        </select>

        <input
        name="admission_no"
        placeholder="Admission No"
        >

        <input
        name="father_name"
        placeholder="Father Name"
        >

        <input
        name="mother_name"
        placeholder="Mother Name"
        >

        <input
        name="mobile"
        placeholder="Mobile"
        >

        <input
        name="house"
        placeholder="House"
        >

        <select name="hostel">

        <option>Yes</option>
        <option>No</option>

        </select>

        <button>
        Add Student
        </button>

        </form>

        """

    else:

        add = """

        <p class="small">
        Teacher can view only assigned classes.
        </p>

        """

    rows = ""

    for s in visible:

        action = ""

        if u["role"] == "principal":

            action = f"""

            <form
            method="post"
            onsubmit="return confirmDelete()"
            style="display:inline"
            >

            <input
            type="hidden"
            name="action"
            value="delete"
            >

            <input
            type="hidden"
            name="student_id"
            value="{esc(s["id"])}"
            >

            <button class="danger">
            Delete
            </button>

            </form>

            """

        rows += f"""

        <tr>

        <td>{esc(s.get("roll",""))}</td>

        <td>{esc(s.get("name",""))}</td>

        <td>{esc(s.get("gender",""))}</td>

        <td>{esc(s.get("admission_no",""))}</td>

        <td>
        {esc(
            class_name(
                s.get("class_id","")
            )
        )}
        </td>

        <td>{esc(s.get("house",""))}</td>

        <td>{esc(s.get("hostel",""))}</td>

        <td>{esc(s.get("father_name",""))}</td>

        <td>{esc(s.get("mobile",""))}</td>

        <td>{action}</td>

        </tr>

        """

    return page(

        "Students",

        f"""

        <div class="card">

        <h1>Student Management</h1>

        {add}

        </div>

        <div class="card">

        <input
        id="sq"
        onkeyup="filterTable('sq','studentTable')"
        placeholder="Search student..."
        >

        <table id="studentTable">

        <thead>

        <tr>

        <th>Roll</th>
        <th>Name</th>
        <th>Gender</th>
        <th>Admission</th>
        <th>Class</th>
        <th>House</th>
        <th>Hostel</th>
        <th>Father</th>
        <th>Mobile</th>
        <th>Action</th>

        </tr>

        </thead>

        <tbody>

        {rows}

        </tbody>

        </table>

        </div>

        """
    )


# ============================================================
# 1. PERIOD / SUBJECT ATTENDANCE
# ============================================================

@app.route("/attendance", methods=["GET", "POST"])
@login_required
def attendance():
    u = current_user()
    if u.get("role") == "principal":
        flash("Principal केवल teacher-submitted attendance monitor कर सकता है; attendance submit नहीं कर सकता.")
        return redirect(url_for("dashboard"))
    all_classes = classes()
    if u["role"] == "teacher":
        t = teacher_profile(u["id"])
        allowed = [a for a in assignments() if t and a.get("teacher_id") == t.get("id")]
        visible_classes = [c for c in all_classes if c.get("id") in {a.get("class_id") for a in allowed}]
    else:
        allowed = assignments()
        visible_classes = all_classes

    if not visible_classes:
        return page("Period Attendance", '<div class="card"><h1>No class assigned</h1><p class="small">Please ask Principal to assign a class.</p></div>')

    class_id = request.form.get("class_id") or request.args.get("class_id") or visible_classes[0]["id"]
    if class_id not in {c["id"] for c in visible_classes}:
        class_id = visible_classes[0]["id"]

    if u["role"] == "teacher":
        subject_ids = [a.get("subject_id") for a in allowed if a.get("class_id") == class_id]
        visible_subjects = [x for x in subjects() if x.get("id") in subject_ids]
    else:
        subject_ids = [a.get("subject_id") for a in assignments() if a.get("class_id") == class_id]
        visible_subjects = [x for x in subjects() if not subject_ids or x.get("id") in subject_ids]

    if not visible_subjects:
        visible_subjects = subjects()
    subject_id = request.form.get("subject_id") or request.args.get("subject_id") or visible_subjects[0]["id"]
    if subject_id not in {x["id"] for x in visible_subjects}:
        subject_id = visible_subjects[0]["id"]

    period = request.form.get("period") or request.args.get("period") or "1"
    adate = request.form.get("att_date") or request.args.get("att_date") or str(date.today())

    default_total = CLASS_TOTALS.get(class_id, 40)
    data = attendance_records()
    existing = next((r for r in reversed(data) if r.get("summary") and r.get("class_id") == class_id and r.get("subject_id") == subject_id and str(r.get("period")) == str(period) and r.get("date") == adate), None)

    total = int(existing.get("total", default_total)) if existing else default_total
    present = int(existing.get("present", max(0, total - 1))) if existing else max(0, total - 1)
    total = max(0, total)
    present = min(max(0, present), total)
    absent = total - present

    if request.method == "POST":
        try:
            total = max(0, int(request.form.get("total_students", default_total)))
            present = max(0, int(request.form.get("present_students", 0)))
        except ValueError:
            flash("Total and Present must be valid numbers.")
            return redirect(url_for("attendance", class_id=class_id, subject_id=subject_id, period=period, att_date=adate))
        if present > total:
            flash("Present students cannot be greater than Total students.")
            return redirect(url_for("attendance", class_id=class_id, subject_id=subject_id, period=period, att_date=adate))
        absent = total - present
        teacher_name = u.get("name", "")
        teacher_id = ""
        if u["role"] == "teacher":
            tp = teacher_profile(u["id"])
            if tp:
                teacher_name = tp.get("name", teacher_name)
                teacher_id = tp.get("id", "")
        now = datetime.now()
        data = [r for r in data if not (r.get("summary") and r.get("class_id") == class_id and r.get("subject_id") == subject_id and str(r.get("period")) == str(period) and r.get("date") == adate)]
        data.append({
            "id": next_id("PA", data), "summary": True, "module": "Period Attendance",
            "date": adate, "time": now.strftime("%H:%M:%S"), "submitted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "teacher_id": teacher_id, "teacher_name": teacher_name, "teacher_username": u.get("username", ""),
            "class_id": class_id, "subject_id": subject_id, "period": str(period),
            "total": total, "present": present, "absent": absent,
        })
        save("attendance", data)
        flash(f"Period attendance saved: Total {total}, Present {present}, Absent {absent}.")
        return redirect(url_for("attendance", class_id=class_id, subject_id=subject_id, period=period, att_date=adate))

    class_opts = "".join(f'<option value="{esc(c["id"])}" {"selected" if c["id"] == class_id else ""}>{esc(class_name(c["id"]))}</option>' for c in visible_classes)
    subject_opts = "".join(f'<option value="{esc(x["id"])}" {"selected" if x["id"] == subject_id else ""}>{esc(x.get("name", ""))}</option>' for x in visible_subjects)
    period_opts = "".join(f'<option value="{i}" {"selected" if str(i) == str(period) else ""}>Period {i}</option>' for i in range(1, 9))
    teacher_display = esc(u.get("name", ""))

    return page("Period Attendance", f'''\
    <div class="card">
      <h1>📝 Period Attendance</h1>
      <p class="small">Teacher केवल class का total और present भरेगा। Absent अपने-आप निकलेगा।</p>
      <form method="get" class="toolbar">
        <label><b>Class</b></label><select name="class_id" onchange="this.form.submit()">{class_opts}</select>
        <label><b>Subject</b></label><select name="subject_id">{subject_opts}</select>
        <label><b>Period</b></label><select name="period">{period_opts}</select>
        <label><b>Date</b></label><input type="date" name="att_date" value="{esc(adate)}">
        <button>Load</button>
      </form>
    </div>
    <div class="grid">
      <div class="stat">Teacher<strong>{teacher_display}</strong></div>
      <div class="stat">Total Student<strong id="ptTotal">{total}</strong></div>
      <div class="stat">Present Student<strong id="ptPresent">{present}</strong></div>
      <div class="stat">Absent Student<strong id="ptAbsent">{absent}</strong></div>
    </div>
    <div class="card">
      <form method="post">
        <input type="hidden" name="class_id" value="{esc(class_id)}"><input type="hidden" name="subject_id" value="{esc(subject_id)}"><input type="hidden" name="period" value="{esc(period)}"><input type="hidden" name="att_date" value="{esc(adate)}">
        <div class="grid">
          <div><label><b>Total Student</b></label><input id="ptTotalInput" type="number" min="0" name="total_students" value="{total}" oninput="calcPeriod()" required></div>
          <div><label><b>Present Student</b></label><input id="ptPresentInput" type="number" min="0" name="present_students" value="{present}" oninput="calcPeriod()" required></div>
          <div><label><b>Absent Student (Automatic)</b></label><input id="ptAbsentInput" type="number" value="{absent}" readonly></div>
        </div>
        <br><button class="ok" type="submit">💾 Submit Period Attendance</button>
      </form>
    </div>
    <script>
    function calcPeriod() {{ let t=Math.max(0,parseInt(document.getElementById('ptTotalInput').value)||0); let p=Math.max(0,parseInt(document.getElementById('ptPresentInput').value)||0); if(p>t)p=t; document.getElementById('ptPresentInput').value=p; let a=t-p; document.getElementById('ptAbsentInput').value=a; document.getElementById('ptTotal').innerText=t; document.getElementById('ptPresent').innerText=p; document.getElementById('ptAbsent').innerText=a; }}
    </script>''')


# 2. SCHOOL DAILY ATTENDANCE

# ============================================================

@app.route(
    "/daily-attendance",
    methods=["GET", "POST"]
)
@login_required
def daily_attendance():

    u = current_user()
    if u.get("role") == "principal":
        flash("Principal केवल teacher-submitted Daily Attendance monitor कर सकता है; attendance submit नहीं कर सकता.")
        return redirect(url_for("dashboard"))
    cs = classes()

    # Teachers can only mark their assigned classes.
    if u["role"] == "teacher":
        t = teacher_profile(u["id"])
        allowed = {
            a.get("class_id")
            for a in assignments()
            if t and a.get("teacher_id") == t.get("id")
        }
        cs = [c for c in cs if c.get("id") in allowed]

    cid = request.form.get("class_id") or request.args.get("class_id")
    subject_id = request.form.get("subject_id") or request.args.get("subject_id") or (subjects()[0]["id"] if subjects() else "")
    period = request.form.get("period") or request.args.get("period") or "1"
    adate = (
        request.form.get("att_date")
        or request.args.get("att_date")
        or str(date.today())
    )

    if not cid and cs:
        cid = cs[0]["id"]

    selected_students = [
        s for s in students()
        if s.get("class_id") == cid
        and str(s.get("status", "Active")).lower() != "inactive"
    ]

    # Save / update one complete class attendance sheet.
    if request.method == "POST":
        if not cid:
            flash("Please select a class.")
            return redirect(url_for("daily_attendance"))

        data = daily_records()

        # Remove the old sheet for this class/date so the new sheet
        # becomes an update rather than creating duplicate records.
        data = [
            r for r in data
            if not (
                r.get("class_id") == cid
                and r.get("date") == adate
                and str(r.get("subject_id", "")) == str(subject_id)
                and str(r.get("period", "1")) == str(period)
            )
        ]

        teacher_id = ""
        teacher_name_value = u.get("name", "")

        if u["role"] == "teacher":
            t = teacher_profile(u["id"])
            teacher_id = t.get("id", "") if t else ""
            teacher_name_value = t.get("name", u.get("name", "")) if t else u.get("name", "")

        for s in selected_students:
            status = request.form.get(
                "status_" + s["id"],
                "Absent"
            )

            if status not in {
                "Present", "Absent", "Late", "Leave", "Medical"
            }:
                status = "Absent"

            data.append({
                "id": next_id("DA", data),
                "date": adate,
                "class_id": cid,
                "student_id": s["id"],
                "student_name": s["name"],
                "roll": s.get("roll", ""),
                "status": status,
                "teacher_id": teacher_id,
                "teacher_name": teacher_name_value,
                "subject_id": subject_id,
                "period": str(period),
                "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "time": datetime.now().strftime("%H:%M:%S")
            })

        save("daily_attendance", data)

        flash(
            f"School attendance saved for "
            f"{class_name(cid)} on {adate}."
        )

        return redirect(
            url_for(
                "daily_attendance",
                class_id=cid,
                subject_id=subject_id,
                period=period,
                att_date=adate
            )
        )

    # Load already-saved attendance for editing.
    existing = {
        r.get("student_id"): r
        for r in daily_records()
        if r.get("class_id") == cid
        and r.get("date") == adate
        and str(r.get("subject_id", "")) == str(subject_id)
        and str(r.get("period", "1")) == str(period)
    }

    counts = {
        "Present": 0,
        "Absent": 0,
        "Late": 0,
        "Leave": 0,
        "Medical": 0
    }

    for s in selected_students:
        old = existing.get(s.get("id"))
        if old and old.get("status") in counts:
            counts[old["status"]] += 1

    total = len(selected_students)
    marked = sum(counts.values())
    present_equivalent = counts["Present"] + counts["Late"]
    pct = percentage(present_equivalent, total)

    opts = "".join(
        f"""
        <option value="{esc(c["id"])}"
        {"selected" if c["id"] == cid else ""}>
        {esc(class_name(c["id"]))}
        </option>
        """
        for c in cs
    )

    rows = ""

    for s in selected_students:
        sid = s["id"]
        old_status = existing.get(sid, {}).get("status", "Absent")

        rows += f"""
        <tr>
            <td>{esc(s.get("roll", ""))}</td>
            <td>
                <b>{esc(s.get("name", ""))}</b>
                <div class="small">
                    {esc(s.get("admission_no", ""))}
                </div>
            </td>
            <td>{esc(s.get("gender", ""))}</td>
            <td>
                <select
                    class="att-status"
                    name="status_{esc(sid)}"
                    onchange="updateAttendanceSummary()"
                >
                    <option {"selected" if old_status == "Present" else ""}>Present</option>
                    <option {"selected" if old_status == "Absent" else ""}>Absent</option>
                    <option {"selected" if old_status == "Late" else ""}>Late</option>
                    <option {"selected" if old_status == "Leave" else ""}>Leave</option>
                    <option {"selected" if old_status == "Medical" else ""}>Medical</option>
                </select>
            </td>
        </tr>
        """

    if not selected_students:
        rows = """
        <tr>
            <td colspan="4" class="center">
                No active students found in this class.
            </td>
        </tr>
        """

    return page(
        "School Daily Attendance",
        f"""
        <div class="card">
            <h1>🏫 School Daily Attendance</h1>

            <p class="small">
                Jawahar Navodaya Vidyalaya — daily student attendance.
                Select class and date, mark attendance, then save.
                Existing attendance can be opened and corrected.
            </p>

            <form method="get" class="toolbar">
                <label><b>Class:</b></label>
                <select name="class_id" required>
                    {opts}
                </select>

                <label><b>Subject:</b></label>
                <select name="subject_id">
                    {"".join(f'<option value="{esc(x["id"])}" {"selected" if x["id"] == subject_id else ""}>{esc(x.get("name", ""))}</option>' for x in subjects())}
                </select>

                <label><b>Period:</b></label>
                <select name="period">
                    {"".join(f'<option value="{i}" {"selected" if str(i) == str(period) else ""}>Period {i}</option>' for i in range(1,9))}
                </select>

                <label><b>Date:</b></label>
                <input
                    type="date"
                    name="att_date"
                    value="{esc(adate)}"
                    required
                >

                <button>Load Attendance</button>
            </form>
        </div>

        <div class="grid">
            <div class="stat">
                Total Students
                <strong id="totalCount">{total}</strong>
            </div>
            <div class="stat">
                Present
                <strong id="presentCount">{counts["Present"]}</strong>
            </div>
            <div class="stat">
                Absent
                <strong id="absentCount">{counts["Absent"]}</strong>
            </div>
            <div class="stat">
                Late
                <strong id="lateCount">{counts["Late"]}</strong>
            </div>
            <div class="stat">
                Leave
                <strong id="leaveCount">{counts["Leave"]}</strong>
            </div>
            <div class="stat">
                Medical
                <strong id="medicalCount">{counts["Medical"]}</strong>
            </div>
            <div class="stat">
                Attendance %
                <strong id="attendancePct">{pct}%</strong>
            </div>
        </div>

        <div class="card">
            <div class="toolbar">
                <button
                    type="button"
                    class="ok"
                    onclick="markAll('Present');updateAttendanceSummary()"
                >
                    ✓ All Present
                </button>

                <button
                    type="button"
                    class="danger"
                    onclick="markAll('Absent');updateAttendanceSummary()"
                >
                    ✕ All Absent
                </button>

                <button
                    type="button"
                    class="orange"
                    onclick="markAll('Late');updateAttendanceSummary()"
                >
                    ⏰ All Late
                </button>

                <button
                    type="button"
                    class="gray"
                    onclick="markAll('Leave');updateAttendanceSummary()"
                >
                    Leave
                </button>

                <button
                    type="button"
                    class="purple"
                    onclick="markAll('Medical');updateAttendanceSummary()"
                >
                    Medical
                </button>
            </div>

            <form method="post">
                <input
                    type="hidden"
                    name="class_id"
                    value="{esc(cid or '')}"
                >

                <input
                    type="hidden"
                    name="att_date"
                    value="{esc(adate)}"
                >
                <input type="hidden" name="subject_id" value="{esc(subject_id)}">
                <input type="hidden" name="period" value="{esc(period)}">

                <div style="overflow:auto">
                    <table id="schoolAttendanceTable">
                        <thead>
                            <tr>
                                <th>Roll</th>
                                <th>Student</th>
                                <th>Gender</th>
                                <th>Status</th>
                            </tr>
                        </thead>

                        <tbody>
                            {rows}
                        </tbody>
                    </table>
                </div>

                <br>

                <button
                    type="submit"
                    class="ok"
                    style="font-size:16px;padding:12px 18px"
                >
                    💾 Save / Update School Attendance
                </button>

                <span class="small">
                    Previously saved attendance will be replaced for
                    the selected class and date.
                </span>
            </form>
        </div>

        <div class="card">
            <h2>Attendance Status Guide</h2>
            <table>
                <tr>
                    <th>Status</th>
                    <th>Meaning</th>
                </tr>
                <tr>
                    <td><b>Present</b></td>
                    <td>Student attended school.</td>
                </tr>
                <tr>
                    <td><b>Absent</b></td>
                    <td>Student was absent.</td>
                </tr>
                <tr>
                    <td><b>Late</b></td>
                    <td>Student attended but arrived late.</td>
                </tr>
                <tr>
                    <td><b>Leave</b></td>
                    <td>Approved / recorded leave.</td>
                </tr>
                <tr>
                    <td><b>Medical</b></td>
                    <td>Medical absence/status.</td>
                </tr>
            </table>
        </div>

        <script>
        function updateAttendanceSummary() {{
            const selects = document.querySelectorAll(
                '#schoolAttendanceTable select.att-status'
            );

            const counts = {{
                Present: 0,
                Absent: 0,
                Late: 0,
                Leave: 0,
                Medical: 0
            }};

            selects.forEach(function(s) {{
                if (counts[s.value] !== undefined) {{
                    counts[s.value]++;
                }}
            }});

            const total = selects.length;
            const attended = counts.Present + counts.Late;
            const pct = total
                ? Math.round(attended * 1000 / total) / 10
                : 0;

            document.getElementById('totalCount').innerText = total;
            document.getElementById('presentCount').innerText = counts.Present;
            document.getElementById('absentCount').innerText = counts.Absent;
            document.getElementById('lateCount').innerText = counts.Late;
            document.getElementById('leaveCount').innerText = counts.Leave;
            document.getElementById('medicalCount').innerText = counts.Medical;
            document.getElementById('attendancePct').innerText = pct + '%';
        }}

        updateAttendanceSummary();
        </script>
        """
    )


# 3. MORNING ASSEMBLY ATTENDANCE
# ============================================================

@app.route("/assembly-attendance", methods=["GET", "POST"])
@login_required
def assembly_attendance():
    u=current_user()
    if u.get("role") == "principal":
        flash("Principal Morning Assembly attendance submit नहीं कर सकता; केवल submitted data देख सकता है.")
        return redirect(url_for("dashboard"))
    cs=classes()
    if u["role"]=="teacher":
        tp=teacher_profile(u["id"])
        allowed={x.get("class_id") for x in assignments() if tp and x.get("teacher_id")==tp.get("id")}
        cs=[c for c in cs if c.get("id") in allowed]
    if not cs:
        return page("Morning Assembly", '<div class="card"><h1>No class assigned</h1></div>')
    cid=request.form.get("class_id") or request.args.get("class_id") or cs[0]["id"]
    if cid not in {c["id"] for c in cs}: cid=cs[0]["id"]
    adate=request.form.get("att_date") or request.args.get("att_date") or str(date.today())
    defaults=CLASS_GENDER_TOTALS.get(cid,{"boys":20,"girls":20})
    total=int(CLASS_TOTALS.get(cid,40))
    data=assembly_records()
    existing=next((r for r in reversed(data) if r.get("summary") and r.get("class_id")==cid and r.get("date")==adate),None)
    if existing:
        total=int(existing.get("total",total))
        boys_total=int(existing.get("boys_total",min(defaults["boys"],total)))
        girls_total=int(existing.get("girls_total",max(0,total-boys_total)))
        pb=int(existing.get("present_boys",0)); pg=int(existing.get("present_girls",0))
        lb=int(existing.get("leave_boys",0)); lg=int(existing.get("leave_girls",0))
    else:
        boys_total=min(defaults["boys"],total); girls_total=max(0,total-boys_total)
        pb=boys_total; pg=girls_total; lb=0; lg=0
    if request.method=="POST":
        try:
            total=max(0,int(request.form.get("total_students",total)))
            boys_total=min(defaults["boys"],total); girls_total=max(0,total-boys_total)
            pb=max(0,int(request.form.get("present_boys",0))); pg=max(0,int(request.form.get("present_girls",0)))
            lb=max(0,int(request.form.get("leave_boys",0))); lg=max(0,int(request.form.get("leave_girls",0)))
        except ValueError:
            flash("Please enter valid numbers.")
            return redirect(url_for("assembly_attendance",class_id=cid,att_date=adate))
        if pb+lb>boys_total or pg+lg>girls_total:
            flash("Present + Leave cannot be greater than the class gender total.")
            return redirect(url_for("assembly_attendance",class_id=cid,att_date=adate))
        ab=boys_total-pb-lb; ag=girls_total-pg-lg; now=datetime.now()
        data=[r for r in data if not(r.get("summary") and r.get("class_id")==cid and r.get("date")==adate)]
        data.append({"id":next_id("AS",data),"summary":True,"module":"Morning Assembly","date":adate,
            "time":now.strftime("%H:%M:%S"),"submitted_at":now.strftime("%Y-%m-%d %H:%M:%S"),
            "teacher_id":u.get("id",""),"teacher_name":u.get("name",""),"class_id":cid,
            "total":total,"boys_total":boys_total,"girls_total":girls_total,
            "present_boys":pb,"present_girls":pg,"leave_boys":lb,"leave_girls":lg,
            "absent_boys":ab,"absent_girls":ag,"present":pb+pg,"leave":lb+lg,"absent":ab+ag})
        save("assembly_attendance",data)
        flash(f"Assembly saved: Total {total}, Present {pb+pg}, Leave {lb+lg}, Absent {ab+ag}.")
        return redirect(url_for("assembly_attendance",class_id=cid,att_date=adate))
    opts=''.join(f'<option value="{esc(c["id"])}" {"selected" if c["id"]==cid else ""}>{esc(class_name(c["id"]))}</option>' for c in cs)
    ab=boys_total-pb-lb; ag=girls_total-pg-lg
    return page("Morning Assembly",f'''<div class="card"><h1>🏫 Morning Assembly</h1>
    <p class="small">Present और Leave दोनों manually बदल सकते हैं. Absent automatic रहेगा.</p>
    <form method="get" class="toolbar"><label><b>Class</b></label><select name="class_id">{opts}</select>
    <label><b>Date</b></label><input type="date" name="att_date" value="{esc(adate)}"><button>Load</button></form></div>
    <div class="grid"><div class="stat">Total Student<strong id="asTotal">{total}</strong></div>
    <div class="stat">Present<strong id="asPresent">{pb+pg}</strong></div><div class="stat">Leave<strong id="asLeave">{lb+lg}</strong></div>
    <div class="stat">Absent<strong id="asAbsent">{ab+ag}</strong></div></div>
    <div class="card" style="overflow:auto"><form method="post"><input type="hidden" name="class_id" value="{esc(cid)}">
    <input type="hidden" name="att_date" value="{esc(adate)}"><table><thead><tr><th>Gender</th><th>Total</th><th>Present</th><th>Leave</th><th>Absent</th></tr></thead>
    <tbody><tr><td><b>Boys</b></td><td><input id="asBTotal" readonly value="{boys_total}"></td>
    <td><input id="asPBInput" name="present_boys" type="number" min="0" value="{pb}" oninput="calcAssembly()"></td>
    <td><input id="asLBInput" name="leave_boys" type="number" min="0" value="{lb}" oninput="calcAssembly()"></td><td><input id="asABInput" readonly value="{ab}"></td></tr>
    <tr><td><b>Girls</b></td><td><input id="asGTotal" readonly value="{girls_total}"></td>
    <td><input id="asPGInput" name="present_girls" type="number" min="0" value="{pg}" oninput="calcAssembly()"></td>
    <td><input id="asLGInput" name="leave_girls" type="number" min="0" value="{lg}" oninput="calcAssembly()"></td><td><input id="asAGInput" readonly value="{ag}"></td></tr></tbody></table>
    <input type="hidden" name="total_students" value="{total}"><br><button class="ok">💾 Submit Assembly</button></form></div>
    <script>function calcAssembly(){{let b=Number(document.getElementById('asBTotal').value)||0,g=Number(document.getElementById('asGTotal').value)||0;
    let pb=Math.min(b,Math.max(0,+document.getElementById('asPBInput').value||0)),pg=Math.min(g,Math.max(0,+document.getElementById('asPGInput').value||0));
    let lb=Math.min(b-pb,Math.max(0,+document.getElementById('asLBInput').value||0)),lg=Math.min(g-pg,Math.max(0,+document.getElementById('asLGInput').value||0));
    document.getElementById('asPBInput').value=pb;document.getElementById('asPGInput').value=pg;document.getElementById('asLBInput').value=lb;document.getElementById('asLGInput').value=lg;
    let ab=b-pb-lb,ag=g-pg-lg;document.getElementById('asABInput').value=ab;document.getElementById('asAGInput').value=ag;
    document.getElementById('asPresent').innerText=pb+pg;document.getElementById('asLeave').innerText=lb+lg;document.getElementById('asAbsent').innerText=ab+ag;}}</script>''')


# ============================================================
# 4. HOSTEL / NIGHT ATTENDANCE
# ============================================================

@app.route("/hostel-attendance", methods=["GET", "POST"])
@login_required
def hostel_attendance():
    u=current_user()
    if u.get("role") == "principal":
        flash("Principal Hostel/Night attendance submit नहीं कर सकता; केवल submitted data देख सकता है.")
        return redirect(url_for("dashboard"))
    adate=request.form.get("att_date") or request.args.get("att_date") or str(date.today()); group=request.form.get("house_group") or request.args.get("house_group") or list(HOUSE_TOTALS)[0]; total=int(HOUSE_TOTALS.get(group,60)); data=hostel_records(); existing=next((r for r in reversed(data) if r.get("summary") and r.get("house_group")==group and r.get("date")==adate),None); present=int(existing.get("present",40)) if existing else 40; present=min(max(0,present),total); leave=total-present
    if request.method=="POST":
        try: total=max(0,int(request.form.get("total_students",total))); present=max(0,int(request.form.get("present_students",0)))
        except ValueError: flash("Please enter valid numbers."); return redirect(url_for("hostel_attendance",house_group=group,att_date=adate))
        if present>total: flash("Present cannot be greater than Total."); return redirect(url_for("hostel_attendance",house_group=group,att_date=adate))
        leave=total-present; now=datetime.now(); data=[r for r in data if not(r.get("summary") and r.get("house_group")==group and r.get("date")==adate)]; data.append({"id":next_id("HA",data),"summary":True,"module":"Hostel / Night","date":adate,"time":now.strftime("%H:%M:%S"),"submitted_at":now.strftime("%Y-%m-%d %H:%M:%S"),"teacher_id":u.get("id",""),"teacher_name":u.get("name",""),"house_group":group,"house":group.split()[0],"stage":group.split()[1],"gender":group.split()[2],"total":total,"present":present,"leave":leave,"absent":0}); save("hostel_attendance",data); flash(f"Hostel saved: Total {total}, Present {present}, Leave {leave}."); return redirect(url_for("hostel_attendance",house_group=group,att_date=adate))
    opts=''.join(f'<option value="{esc(g)}" {"selected" if g==group else ""}>{esc(g)}</option>' for g in HOUSE_TOTALS)
    return page("Hostel / Night",f'''<div class="card"><h1>🛏 Hostel / Night Attendance</h1><form method="get" class="toolbar"><label><b>House / Group</b></label><select name="house_group">{opts}</select><label><b>Date</b></label><input type="date" name="att_date" value="{esc(adate)}"><button>Load</button></form></div><div class="grid"><div class="stat">House<strong>{esc(group)}</strong></div><div class="stat">Total<strong id="hTotal">{total}</strong></div><div class="stat">Present<strong id="hPresent">{present}</strong></div><div class="stat">Leave<strong id="hLeave">{leave}</strong></div></div><div class="card"><form method="post"><input type="hidden" name="house_group" value="{esc(group)}"><input type="hidden" name="att_date" value="{esc(adate)}"><div class="grid"><div><label><b>Total</b></label><input id="hTotalInput" type="number" min="0" name="total_students" value="{total}" oninput="calcHouse()"></div><div><label><b>Present</b></label><input id="hPresentInput" type="number" min="0" name="present_students" value="{present}" oninput="calcHouse()"></div><div><label><b>Leave (Automatic)</b></label><input id="hLeaveInput" readonly value="{leave}"></div></div><br><button class="ok">💾 Submit Hostel/Night</button></form></div><script>function calcHouse(){{let t=Math.max(0,+document.getElementById('hTotalInput').value||0);let p=Math.min(t,Math.max(0,+document.getElementById('hPresentInput').value||0));document.getElementById('hPresentInput').value=p;let l=t-p;document.getElementById('hLeaveInput').value=l;document.getElementById('hTotal').innerText=t;document.getElementById('hPresent').innerText=p;document.getElementById('hLeave').innerText=l;}}</script>''')

# ============================================================
# 5. MESS ATTENDANCE
# ============================================================

@app.route(
    "/mess-attendance",
    methods=["GET", "POST"]
)
@login_required
def mess_attendance():

    if request.method == "POST":

        adate = request.form.get(
            "att_date"
        ) or str(date.today())

        meal = request.form.get(
            "meal",
            "Breakfast"
        )

        data = mess_records()

        ss = students()

        data = [

            r for r in data

            if not (
                r.get("date") == adate
                and r.get("meal") == meal
            )

        ]

        for s in ss:

            data.append({

                "id": next_id(
                    "MA",
                    data
                ),

                "date": adate,

                "meal": meal,

                "student_id": s["id"],

                "student_name": s["name"],

                "class_id": s["class_id"],

                "status": request.form.get(
                    "status_" + s["id"],
                    "Taken"
                ),

                "time": datetime.now()
                .strftime("%H:%M:%S")

            })

        save(
            "mess_attendance",
            data
        )

        flash(
            f"{meal} mess attendance saved."
        )

        return redirect(
            url_for("mess_attendance")
        )

    meal = request.args.get(
        "meal",
        "Breakfast"
    )

    rows = ""

    for s in students():

        rows += f"""

        <tr>

        <td>{esc(s["roll"])}</td>

        <td>{esc(s["name"])}</td>

        <td>
        {esc(class_name(s["class_id"]))}
        </td>

        <td>

        <select
        class="att-status"
        name="status_{esc(s["id"])}"
        >

        <option>Taken</option>
        <option>Not Taken</option>
        <option>Leave</option>
        <option>Medical</option>

        </select>

        </td>

        </tr>

        """

    return page(

        "Mess Attendance",

        f"""

        <div class="card">

        <h1>
        🍽 Mess Attendance
        </h1>

        <form method="post">

        <input
        type="date"
        name="att_date"
        value="{date.today()}"
        >

        <select name="meal">

        <option
        {"selected" if meal=="Breakfast" else ""}
        >
        Breakfast
        </option>

        <option
        {"selected" if meal=="Lunch" else ""}
        >
        Lunch
        </option>

        <option
        {"selected" if meal=="Dinner" else ""}
        >
        Dinner
        </option>

        </select>

        <button
        type="button"
        class="ok"
        onclick="markAll('Taken')"
        >
        All Taken
        </button>

        <table>

        <tr>

        <th>Roll</th>
        <th>Student</th>
        <th>Class</th>
        <th>Meal</th>

        </tr>

        {rows}

        </table>

        <br>

        <button>
        Save Mess Attendance
        </button>

        </form>

        </div>

        """
    )


# ============================================================
# 6. JNV ROUTINE / SPECIAL ATTENDANCE
# ============================================================

SPECIAL_ATTENDANCE_CONFIG = {
    "morning_pt": {
        "title": "Morning PT Attendance",
        "heading": "🌅 Morning PT Attendance",
        "kind": "morning_pt_attendance",
        "status_title": "PT Status",
        "help": "Morning physical training attendance for JNV students.",
    },
    "remedial": {
        "title": "Remedial Attendance",
        "heading": "📚 Remedial Attendance",
        "kind": "remedial_attendance",
        "status_title": "Remedial Status",
        "help": "Record attendance for students attending remedial classes.",
    },
    "evening_student": {
        "title": "Evening Student Attendance",
        "heading": "🌆 Evening Student Attendance",
        "kind": "evening_student_attendance",
        "status_title": "Evening Status",
        "help": "Evening student attendance / roll call.",
    },
    "evening_games": {
        "title": "Evening Games Attendance",
        "heading": "🏃 Evening Games Attendance",
        "kind": "evening_games_attendance",
        "status_title": "Games Status",
        "help": "Record student participation/attendance during evening games.",
    },
    "cdd_hm_hm": {
        "title": "CDD HM/HM Attendance",
        "heading": "🗂 CDD HM/HM Attendance",
        "kind": "cdd_hm_hm_attendance",
        "status_title": "Status",
        "help": "Special attendance register labelled CDD HM/HM as shown in your design.",
    },
}


@app.route("/special-attendance/<kind>", methods=["GET", "POST"])
@login_required
def special_attendance(kind):
    u=current_user()
    if u.get("role") == "principal":
        flash("Principal इस module में attendance submit नहीं कर सकता; केवल teacher-submitted data monitor कर सकता है.")
        return redirect(url_for("dashboard"))
    cfg=SPECIAL_ATTENDANCE_CONFIG.get(kind)
    if not cfg: return redirect(url_for("dashboard"))
    u=current_user(); cs=classes()
    if u["role"]=="teacher":
        tp=teacher_profile(u["id"]); allowed={a.get("class_id") for a in assignments() if tp and a.get("teacher_id")==tp.get("id")}; cs=[c for c in cs if c.get("id") in allowed]
    if not cs: return page(cfg["title"],'<div class="card"><h1>No class assigned</h1></div>')
    adate=request.form.get("att_date") or request.args.get("att_date") or str(date.today())

    # House based modules: Morning PT and Evening Games.
    if kind in {"morning_pt","evening_games"}:
        group=request.form.get("house_group") or request.args.get("house_group") or list(HOUSE_TOTALS)[0]; total=int(HOUSE_TOTALS.get(group,60)); data=special_attendance_records(cfg["kind"]); existing=next((r for r in reversed(data) if r.get("summary") and r.get("house_group")==group and r.get("date")==adate),None); present=int(existing.get("present",40)) if existing else 40; present=min(max(0,present),total); leave=total-present
        if request.method=="POST":
            try: total=max(0,int(request.form.get("total_students",total))); present=max(0,int(request.form.get("present_students",0)))
            except ValueError: flash("Please enter valid numbers."); return redirect(url_for("special_attendance",kind=kind,house_group=group,att_date=adate))
            if present>total: flash("Present cannot be greater than Total."); return redirect(url_for("special_attendance",kind=kind,house_group=group,att_date=adate))
            leave=total-present; now=datetime.now(); data=[r for r in data if not(r.get("summary") and r.get("house_group")==group and r.get("date")==adate)]; data.append({"id":next_id("SP",data),"summary":True,"module":cfg["title"],"date":adate,"time":now.strftime("%H:%M:%S"),"submitted_at":now.strftime("%Y-%m-%d %H:%M:%S"),"teacher_id":u.get("id",""),"teacher_name":u.get("name",""),"house_group":group,"total":total,"present":present,"leave":leave,"absent":0}); save(cfg["kind"],data); flash(f"{cfg['title']} saved: Total {total}, Present {present}, Leave {leave}."); return redirect(url_for("special_attendance",kind=kind,house_group=group,att_date=adate))
        opts=''.join(f'<option value="{esc(g)}" {"selected" if g==group else ""}>{esc(g)}</option>' for g in HOUSE_TOTALS)
        return page(cfg["title"],f'''<div class="card"><h1>{cfg["heading"]}</h1><form method="get" class="toolbar"><label><b>House / Group</b></label><select name="house_group">{opts}</select><label><b>Date</b></label><input type="date" name="att_date" value="{esc(adate)}"><button>Load</button></form></div><div class="grid"><div class="stat">Group<strong>{esc(group)}</strong></div><div class="stat">Total<strong id="xTotal">{total}</strong></div><div class="stat">Present<strong id="xPresent">{present}</strong></div><div class="stat">Leave<strong id="xLeave">{leave}</strong></div></div><div class="card"><form method="post"><input type="hidden" name="house_group" value="{esc(group)}"><input type="hidden" name="att_date" value="{esc(adate)}"><div class="grid"><div><label><b>Total</b></label><input id="xTotalInput" name="total_students" type="number" min="0" value="{total}" oninput="calcX()"></div><div><label><b>Present</b></label><input id="xPresentInput" name="present_students" type="number" min="0" value="{present}" oninput="calcX()"></div><div><label><b>Leave (Automatic)</b></label><input id="xLeaveInput" readonly value="{leave}"></div></div><br><button class="ok">💾 Submit {cfg["title"]}</button></form></div><script>function calcX(){{let t=Math.max(0,+document.getElementById('xTotalInput').value||0);let p=Math.min(t,Math.max(0,+document.getElementById('xPresentInput').value||0));document.getElementById('xPresentInput').value=p;let l=t-p;document.getElementById('xLeaveInput').value=l;document.getElementById('xTotal').innerText=t;document.getElementById('xPresent').innerText=p;document.getElementById('xLeave').innerText=l;}}</script>''')

    # Remedial: class-wise summary, total comes from class master (demo 30).
    if kind=="remedial":
        cid=request.form.get("class_id") or request.args.get("class_id") or cs[0]["id"]; cid=cid if cid in {c["id"] for c in cs} else cs[0]["id"]; total=REMEDIAL_TOTALS.get(cid,30); data=special_attendance_records(cfg["kind"]); existing=next((r for r in reversed(data) if r.get("summary") and r.get("class_id")==cid and r.get("date")==adate),None); present=int(existing.get("present",20)) if existing else min(20,total); absent=total-present
        if request.method=="POST":
            try: present=max(0,int(request.form.get("present_students",0)))
            except ValueError: flash("Present must be a valid number."); return redirect(url_for("special_attendance",kind=kind,class_id=cid,att_date=adate))
            if present>total: flash("Present cannot be greater than Total."); return redirect(url_for("special_attendance",kind=kind,class_id=cid,att_date=adate))
            absent=total-present; now=datetime.now(); data=[r for r in data if not(r.get("summary") and r.get("class_id")==cid and r.get("date")==adate)]; data.append({"id":next_id("RM",data),"summary":True,"module":"Remedial Attendance","date":adate,"time":now.strftime("%H:%M:%S"),"submitted_at":now.strftime("%Y-%m-%d %H:%M:%S"),"teacher_id":u.get("id",""),"teacher_name":u.get("name",""),"class_id":cid,"total":total,"present":present,"absent":absent}); save(cfg["kind"],data); flash(f"Remedial saved: Total {total}, Present {present}, Absent {absent}."); return redirect(url_for("special_attendance",kind=kind,class_id=cid,att_date=adate))
        opts=''.join(f'<option value="{esc(c["id"])}" {"selected" if c["id"]==cid else ""}>{esc(class_name(c["id"]))}</option>' for c in cs)
        return page("Remedial Attendance",f'''<div class="card"><h1>📚 Remedial Attendance</h1><form method="get" class="toolbar"><label><b>Class</b></label><select name="class_id">{opts}</select><label><b>Date</b></label><input type="date" name="att_date" value="{esc(adate)}"><button>Load</button></form></div><div class="grid"><div class="stat">Class<strong>{esc(class_name(cid))}</strong></div><div class="stat">Total<strong>{total}</strong></div><div class="stat">Present<strong id="rPresent">{present}</strong></div><div class="stat">Absent<strong id="rAbsent">{absent}</strong></div></div><div class="card"><form method="post"><input type="hidden" name="class_id" value="{esc(cid)}"><input type="hidden" name="att_date" value="{esc(adate)}"><div class="grid"><div><label><b>Total (Automatic)</b></label><input readonly value="{total}"></div><div><label><b>Present Student</b></label><input id="rPresentInput" name="present_students" type="number" min="0" value="{present}" oninput="calcR()"></div><div><label><b>Absent Student (Automatic)</b></label><input id="rAbsentInput" readonly value="{absent}"></div></div><br><button class="ok">💾 Submit Remedial</button></form></div><script>function calcR(){{let t={total};let p=Math.min(t,Math.max(0,+document.getElementById('rPresentInput').value||0));document.getElementById('rPresentInput').value=p;document.getElementById('rAbsentInput').value=t-p;document.getElementById('rPresent').innerText=p;document.getElementById('rAbsent').innerText=t-p;}}</script>''')

    # Evening Student: multiple class + one gender per row.
    selected=request.form.getlist("selected_class") if request.method=="POST" else request.args.getlist("selected_class")
    if not selected: selected=[cs[0]["id"]]
    selected=[x for x in selected if x in {c["id"] for c in cs}]
    if not selected: selected=[cs[0]["id"]]
    data=special_attendance_records(cfg["kind"])
    if request.method=="POST":
        rows=[]; now=datetime.now()
        for cid in selected:
            gender=request.form.get(f"gender_{cid}","Boys")
            totals=CLASS_GENDER_TOTALS.get(cid,{"boys":20,"girls":20}); total=totals["boys"] if gender=="Boys" else totals["girls"]
            try: present=max(0,int(request.form.get(f"present_{cid}",0)))
            except ValueError: present=0
            present=min(present,total); rows.append({"id":next_id("ES",data),"summary":True,"module":"Evening Student Attendance","date":adate,"time":now.strftime("%H:%M:%S"),"submitted_at":now.strftime("%Y-%m-%d %H:%M:%S"),"teacher_id":u.get("id",""),"teacher_name":u.get("name",""),"class_id":cid,"gender":gender,"total":total,"present":present,"absent":total-present})
        data=[r for r in data if not(r.get("summary") and r.get("date")==adate and r.get("teacher_id")==u.get("id",""))]+rows; save(cfg["kind"],data); flash("Evening Student attendance submitted for all selected classes."); return redirect(url_for("special_attendance",kind=kind,att_date=adate,**{"selected_class":selected}))
    class_options=''.join(f'<option value="{esc(c["id"])}">{esc(class_name(c["id"]))}</option>' for c in cs)
    cards=''.join(f'''<div class="card duty-row"><input type="hidden" name="selected_class" value="{esc(cid)}"><h3>{esc(class_name(cid))}</h3><label><b>Gender</b></label><select name="gender_{esc(cid)}" onchange="updateGender(this,'{cid}')"><option>Boys</option><option>Girls</option></select><div class="grid"><div><label>Total</label><input id="total_{cid}" readonly value="20"></div><div><label>Present</label><input name="present_{cid}" id="present_{cid}" type="number" min="0" value="20" oninput="calcE('{cid}')"></div><div><label>Absent (Automatic)</label><input id="absent_{cid}" readonly value="0"></div></div></div>''' for cid in selected)
    return page("Evening Student Attendance",f'''<div class="card"><h1>🌆 Evening Student Attendance</h1><p class="small">Teacher multiple classes add कर सकता है। हर class में Boys या Girls में से केवल एक चुनें।</p><form method="get" class="toolbar"><label><b>Class</b></label><select id="classPicker">{class_options}</select><button type="button" onclick="addClassDuty()">＋ Add Class</button><label><b>Date</b></label><input type="date" name="att_date" value="{esc(adate)}"></form></div><form method="post"><input type="hidden" name="att_date" value="{esc(adate)}"><div id="dutyCards">{cards}</div><button class="ok" type="submit">💾 Submit All Evening Student Attendance</button></form><script>function addClassDuty(){{let id=document.getElementById('classPicker').value;let exists=[...document.querySelectorAll('input[name=selected_class]')].some(x=>x.value===id);if(exists)return;let n=document.createElement('div');n.className='card duty-row';n.innerHTML=`<input type="hidden" name="selected_class" value="${{id}}"><h3>${{document.getElementById('classPicker').selectedOptions[0].text}}</h3><label><b>Gender</b></label><select name="gender_${{id}}" onchange="updateGender(this,'${{id}}')"><option>Boys</option><option>Girls</option></select><div class="grid"><div><label>Total</label><input id="total_${{id}}" readonly value="20"></div><div><label>Present</label><input name="present_${{id}}" id="present_${{id}}" type="number" value="20" min="0" oninput="calcE('${{id}}')"></div><div><label>Absent (Automatic)</label><input id="absent_${{id}}" readonly value="0"></div></div>`;document.getElementById('dutyCards').appendChild(n)}} function updateGender(sel,id){{document.getElementById('total_'+id).value=20;let p=document.getElementById('present_'+id);p.value=Math.min(20,+p.value||0);calcE(id)}} function calcE(id){{let t=+document.getElementById('total_'+id).value||0;let p=Math.min(t,Math.max(0,+document.getElementById('present_'+id).value||0));document.getElementById('present_'+id).value=p;document.getElementById('absent_'+id).value=t-p}}</script>''')

    return redirect(url_for("dashboard"))


# MORNING PT HOUSE-WISE REPORT
# ============================================================

@app.route("/morning-pt-report", methods=["GET"])
@principal_required
def morning_pt_report():

    adate = request.args.get("att_date") or str(date.today())
    records = special_attendance_records("morning_pt_attendance")
    active_students = [s for s in students() if s.get("status", "active") != "inactive"]

    houses = sorted({s.get("house", "") for s in active_students if s.get("house", "")})
    report = []
    for house in houses:
        hs = [s for s in active_students if s.get("house", "") == house]
        ids = {s.get("id") for s in hs}
        rr = [r for r in records if r.get("date") == adate and r.get("student_id") in ids]
        by_id = {r.get("student_id"): r for r in rr}
        present = sum(by_id.get(s.get("id"), {}).get("status", "Absent") == "Present" for s in hs)
        leave = sum(by_id.get(s.get("id"), {}).get("status", "Absent") == "Leave" for s in hs)
        absent = len(hs) - present - leave
        report.append((house, len(hs), present, leave, absent, percentage(present, len(hs))))

    rows = "".join(
        f"<tr><td>{esc(h)}</td><td>{total}</td><td>{present}</td><td>{leave}</td><td>{absent}</td><td>{pct}%</td></tr>"
        for h,total,present,leave,absent,pct in report
    )

    if not rows:
        rows = '<tr><td colspan="6" class="center">No house/student data available.</td></tr>'

    return page("Morning PT House Report", f'''
    <div class="card">
        <h1>📊 Morning PT Attendance — House-wise Report</h1>
        <form method="get" class="toolbar">
            <label><b>Date:</b></label>
            <input type="date" name="att_date" value="{esc(adate)}">
            <button>Load Report</button>
            <a class="btn" href="{url_for('special_attendance', kind='morning_pt', att_date=adate)}">Open Morning PT</a>
        </form>
    </div>
    <div class="card">
        <table>
            <thead><tr><th>House Name</th><th>Total Student</th><th>Present</th><th>Leave</th><th>Absent / Other</th><th>Attendance %</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    ''')


# ============================================================
# 6. STAFF ATTENDANCE
# ============================================================

@app.route(
    "/staff-attendance",
    methods=["GET", "POST"]
)
@principal_required
def staff_attendance():
    """Daily staff marking + complete teacher/staff history report."""
    if request.method == "POST":
        adate = request.form.get("att_date") or str(date.today())
        data = [r for r in staff_records() if r.get("date") != adate]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for t in teachers():
            status = request.form.get("status_" + t["id"], "Present")
            data.append({
                "id": next_id("STA", data),
                "date": adate,
                "teacher_id": t["id"],
                "teacher_name": t["name"],
                "employee_id": t.get("employee_id", ""),
                "status": status,
                "time": now.split(" ")[1],
                "submitted_at": now,
            })
        save("staff_attendance", data)
        flash("Staff attendance saved.")
        return redirect(url_for("staff_attendance", date=adate))

    selected_date = request.args.get("date", "").strip()
    mark_date = selected_date or str(date.today())
    all_records = staff_records()
    current = {r.get("teacher_id"): r for r in all_records if r.get("date") == mark_date}

    rows = ""
    for t in teachers():
        old = current.get(t["id"], {})
        status = old.get("status", "Present")
        opts = "".join(f'<option {"selected" if status==x else ""}>{x}</option>' for x in ["Present","Absent","Late","Leave","Duty"])
        rows += f'<tr><td>{esc(t.get("employee_id",""))}</td><td>{esc(t.get("name",""))}</td><td><select class="att-status" name="status_{esc(t["id"])}">{opts}</select></td></tr>'

    counts = {}
    for r in all_records:
        tid = r.get("teacher_id")
        c = counts.setdefault(tid, {"days":0,"present":0,"absent":0,"late":0,"leave":0,"duty":0})
        c["days"] += 1
        st = str(r.get("status","")).strip().lower()
        if st in c:
            c[st] += 1

    summary_rows = ""
    for t in teachers():
        c = counts.get(t["id"], {"days":0,"present":0,"absent":0,"late":0,"leave":0,"duty":0})
        summary_rows += f'<tr><td>{esc(t.get("employee_id",""))}</td><td><b>{esc(t.get("name",""))}</b></td><td>{c["days"]}</td><td>{c["present"]}</td><td>{c["absent"]}</td><td>{c["late"]}</td><td>{c["leave"]}</td><td>{c["duty"]}</td></tr>'

    history = sorted(all_records, key=lambda r:(str(r.get("date","")),str(r.get("submitted_at",r.get("time","")))), reverse=True)
    history_rows = "".join(f'<tr><td>{esc(r.get("date",""))}</td><td>{esc(r.get("teacher_name",""))}</td><td>{esc(r.get("employee_id",""))}</td><td>{esc(r.get("status",""))}</td><td>{esc(r.get("submitted_at") or r.get("time",""))}</td></tr>' for r in history)
    if not history_rows:
        history_rows = '<tr><td colspan="5" class="center">No staff attendance history yet.</td></tr>'

    return page(
        "Staff Attendance & Report",
        f'''
        <div class="card"><h1>👨‍🏫 Staff Attendance</h1>
        <p class="small">हर teacher का daily status save होता है. Leave Days और पूरा history नीचे है.</p>
        <form method="post"><input type="date" name="att_date" value="{esc(mark_date)}">
        <button type="button" class="ok" onclick="markAll('Present')">All Present</button>
        <button type="button" class="danger" onclick="markAll('Absent')">All Absent</button>
        <table><thead><tr><th>Employee ID</th><th>Teacher</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table><br>
        <button>💾 Save Staff Attendance</button></form></div>

        <div class="card" style="overflow:auto"><h2>📊 Teacher-wise Staff Summary</h2>
        <table><thead><tr><th>Employee ID</th><th>Teacher</th><th>Total Days</th><th>Present</th><th>Absent</th><th>Late</th><th>Leave Days</th><th>Duty</th></tr></thead>
        <tbody>{summary_rows}</tbody></table></div>

        <div class="card" style="overflow:auto"><h2>📅 Complete Staff Attendance History</h2>
        <input id="staffq" onkeyup="filterTable('staffq','staffHistory')" placeholder="Search teacher/date/status...">
        <table id="staffHistory"><thead><tr><th>Date</th><th>Teacher</th><th>Employee ID</th><th>Status</th><th>Submitted At</th></tr></thead>
        <tbody>{history_rows}</tbody></table></div>
        '''
    )

# ============================================================
# 7. LEAVE MANAGEMENT
# ============================================================

@app.route(
    "/leave-management",
    methods=["GET", "POST"]
)
@login_required
def leave_management():

    u = current_user()

    if request.method == "POST":

        data = leave_records()

        sid = request.form.get(
            "student_id"
        )

        student = student_by_id(
            sid
        )

        if not student:

            flash(
                "Student not found."
            )

            return redirect(
                url_for("leave_management")
            )

        record = {

            "id": next_id(
                "LV",
                data
            ),

            "student_id": sid,

            "student_name":
                student["name"],

            "class_id":
                student["class_id"],

            "from_date":
                request.form.get(
                    "from_date"
                ),

            "to_date":
                request.form.get(
                    "to_date"
                ),

            "reason":
                request.form.get(
                    "reason",
                    ""
                ).strip(),

            "leave_type":
                request.form.get(
                    "leave_type",
                    "General"
                ),

            "status":
                "Approved"
                if u["role"] == "principal"
                else "Pending",

            "created_at":
                datetime.now()
                .isoformat()

        }

        data.append(record)

        save(
            "leave_records",
            data
        )

        flash(
            "Leave request saved."
        )

        return redirect(
            url_for("leave_management")
        )

    if u["role"] == "principal":

        visible = leave_records()

    else:

        visible = [

            r for r in leave_records()

            if r.get("student_id")
            in {
                s["id"]
                for s in students()
            }

        ]

    student_opts = "".join(

        f"""
        <option value="{esc(s["id"])}">
        {esc(s["name"])}
        -
        {esc(class_name(s["class_id"]))}
        </option>
        """

        for s in students()
    )

    rows = ""

    for r in reversed(visible):

        action = ""

        if u["role"] == "principal":

            action = f"""

            <form method="post"
            action="{url_for('approve_leave')}">

            <input
            type="hidden"
            name="leave_id"
            value="{esc(r["id"])}"
            >

            <button class="ok">
            Approve
            </button>

            </form>

            """

        rows += f"""

        <tr>

        <td>{esc(r.get("student_name"))}</td>

        <td>
        {esc(class_name(r.get("class_id")))}
        </td>

        <td>{esc(r.get("from_date"))}</td>

        <td>{esc(r.get("to_date"))}</td>

        <td>{esc(r.get("leave_type"))}</td>

        <td>{esc(r.get("reason"))}</td>

        <td>{esc(r.get("status"))}</td>

        <td>{action}</td>

        </tr>

        """

    return page(

        "Leave Management",

        f"""

        <div class="card">

        <h1>
        📝 Student Leave / Permission
        </h1>

        <form method="post">

        <select name="student_id">
        {student_opts}
        </select>

        <input
        type="date"
        name="from_date"
        required
        >

        <input
        type="date"
        name="to_date"
        required
        >

        <select name="leave_type">

        <option>General</option>
        <option>Medical</option>
        <option>Home Leave</option>
        <option>Emergency</option>
        <option>Permission</option>
        <option>Other</option>

        </select>

        <input
        name="reason"
        placeholder="Reason"
        required
        >

        <button>
        Submit Leave
        </button>

        </form>

        </div>

        <div class="card">

        <h2>Leave Records</h2>

        <table>

        <tr>

        <th>Student</th>
        <th>Class</th>
        <th>From</th>
        <th>To</th>
        <th>Type</th>
        <th>Reason</th>
        <th>Status</th>
        <th>Action</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


@app.route(
    "/approve-leave",
    methods=["POST"]
)
@principal_required
def approve_leave():

    lid = request.form.get(
        "leave_id"
    )

    data = leave_records()

    for r in data:

        if r.get("id") == lid:

            r["status"] = "Approved"

    save(
        "leave_records",
        data
    )

    flash(
        "Leave approved."
    )

    return redirect(
        url_for("leave_management")
    )


# ============================================================
# 8. MEDICAL / SICK RECORD
# ============================================================

@app.route(
    "/medical",
    methods=["GET", "POST"]
)
@login_required
def medical_management():

    if request.method == "POST":

        data = medical_records()

        sid = request.form.get(
            "student_id"
        )

        s = student_by_id(
            sid
        )

        if not s:

            flash(
                "Student not found."
            )

            return redirect(
                url_for("medical_management")
            )

        data.append({

            "id": next_id(
                "MED",
                data
            ),

            "student_id": sid,

            "student_name": s["name"],

            "class_id": s["class_id"],

            "date": request.form.get(
                "date"
            ) or str(date.today()),

            "complaint": request.form.get(
                "complaint",
                ""
            ),

            "doctor": request.form.get(
                "doctor",
                ""
            ),

            "remarks": request.form.get(
                "remarks",
                ""
            ),

            "status": request.form.get(
                "status",
                "Sick"
            ),

        })

        save(
            "medical_records",
            data
        )

        flash(
            "Medical record added."
        )

        return redirect(
            url_for("medical_management")
        )

    student_opts = "".join(

        f"""
        <option value="{esc(s["id"])}">
        {esc(s["name"])}
        -
        {esc(class_name(s["class_id"]))}
        </option>
        """

        for s in students()
    )

    rows = ""

    for r in reversed(
        medical_records()
    ):

        rows += f"""

        <tr>

        <td>
        {esc(r.get("date"))}
        </td>

        <td>
        {esc(r.get("student_name"))}
        </td>

        <td>
        {esc(class_name(r.get("class_id")))}
        </td>

        <td>
        {esc(r.get("complaint"))}
        </td>

        <td>
        {esc(r.get("doctor"))}
        </td>

        <td>
        {esc(r.get("remarks"))}
        </td>

        <td>
        {esc(r.get("status"))}
        </td>

        </tr>

        """

    return page(

        "Medical",

        f"""

        <div class="card">

        <h1>
        🏥 Medical / Sick Record
        </h1>

        <form method="post">

        <select name="student_id">
        {student_opts}
        </select>

        <input
        type="date"
        name="date"
        value="{date.today()}"
        >

        <input
        name="complaint"
        placeholder="Complaint / Reason"
        required
        >

        <input
        name="doctor"
        placeholder="Doctor / Medical Officer"
        >

        <input
        name="remarks"
        placeholder="Remarks"
        >

        <select name="status">

        <option>Sick</option>
        <option>Recovered</option>
        <option>Referred</option>
        <option>Admitted</option>

        </select>

        <button>
        Save Medical Record
        </button>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>Date</th>
        <th>Student</th>
        <th>Class</th>
        <th>Complaint</th>
        <th>Doctor</th>
        <th>Remarks</th>
        <th>Status</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# 9. LATE / EARLY LEAVE
# ============================================================

@app.route(
    "/late-records",
    methods=["GET", "POST"]
)
@login_required
def late_management():

    if request.method == "POST":

        data = late_records()

        sid = request.form.get(
            "student_id"
        )

        s = student_by_id(
            sid
        )

        if not s:

            flash(
                "Student not found."
            )

            return redirect(
                url_for("late_management")
            )

        data.append({

            "id": next_id(
                "LATE",
                data
            ),

            "date": request.form.get(
                "date"
            ) or str(date.today()),

            "student_id": sid,

            "student_name": s["name"],

            "class_id": s["class_id"],

            "type": request.form.get(
                "type",
                "Late Arrival"
            ),

            "time": request.form.get(
                "time",
                ""
            ),

            "reason": request.form.get(
                "reason",
                ""
            ),

            "remarks": request.form.get(
                "remarks",
                ""
            )

        })

        save(
            "late_records",
            data
        )

        flash(
            "Late / early-leave record saved."
        )

        return redirect(
            url_for("late_management")
        )

    student_opts = "".join(

        f"""
        <option value="{esc(s["id"])}">
        {esc(s["name"])}
        -
        {esc(class_name(s["class_id"]))}
        </option>
        """

        for s in students()
    )

    rows = ""

    for r in reversed(
        late_records()
    ):

        rows += f"""

        <tr>

        <td>{esc(r.get("date"))}</td>

        <td>{esc(r.get("student_name"))}</td>

        <td>
        {esc(class_name(r.get("class_id")))}
        </td>

        <td>{esc(r.get("type"))}</td>

        <td>{esc(r.get("time"))}</td>

        <td>{esc(r.get("reason"))}</td>

        <td>{esc(r.get("remarks"))}</td>

        </tr>

        """

    return page(

        "Late Records",

        f"""

        <div class="card">

        <h1>
        ⏰ Late / Early Leave Register
        </h1>

        <form method="post">

        <select name="student_id">
        {student_opts}
        </select>

        <input
        type="date"
        name="date"
        value="{date.today()}"
        >

        <select name="type">

        <option>Late Arrival</option>
        <option>Early Leave</option>
        <option>Late Return from Leave</option>
        <option>Other</option>

        </select>

        <input
        type="time"
        name="time"
        >

        <input
        name="reason"
        placeholder="Reason"
        >

        <input
        name="remarks"
        placeholder="Remarks"
        >

        <button>
        Save Record
        </button>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>Date</th>
        <th>Student</th>
        <th>Class</th>
        <th>Type</th>
        <th>Time</th>
        <th>Reason</th>
        <th>Remarks</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# PRINCIPAL TEACHER-SUBMISSION MONITORING
# Principal only monitors teacher submissions. Principal does NOT fill
# these attendance forms.
# ============================================================

PRINCIPAL_MONITOR_SOURCES = {
    "period": ("Period Attendance", "attendance"),
    "assembly": ("Morning Assembly", "assembly_attendance"),
    "hostel": ("Hostel / Night", "hostel_attendance"),
    "morning_pt": ("Morning PT", "morning_pt_attendance"),
    "remedial": ("Remedial", "remedial_attendance"),
    "evening_student": ("Evening Student", "evening_student_attendance"),
    "evening_games": ("Evening Games", "evening_games_attendance"),
}


def _principal_monitor_page(kind):
    """Principal module report with real submission totals."""
    label, key = PRINCIPAL_MONITOR_SOURCES[kind]
    records = [r for r in safe_load(key, []) if r.get("summary")]
    records.sort(key=lambda r: r.get("submitted_at", "") or f"{r.get('date','')} {r.get('time','')}", reverse=True)

    selected_date = request.args.get("date", "").strip()
    if selected_date:
        records = [r for r in records if str(r.get("date", "")) == selected_date]

    def n(r, field):
        try:
            return max(0, int(r.get(field, 0) or 0))
        except (TypeError, ValueError):
            return 0

    total = sum(n(r, "total") for r in records)
    present = sum(n(r, "present") for r in records)
    leave = sum(n(r, "leave") for r in records)
    absent = sum(n(r, "absent") for r in records)

    if kind != "period":
        absent = sum(
            (n(r, "absent") if n(r, "absent") else max(0, n(r, "total") - n(r, "present") - n(r, "leave")))
            for r in records
        )

    submitted_count = len(records)
    pct = percentage(present, total)

    rows = []
    for r in records:
        teacher = r.get("teacher_name") or r.get("teacher_username") or "Unknown Teacher"
        submitted = r.get("submitted_at") or f"{r.get('date','')} {r.get('time','')}"
        cls = class_name(r.get("class_id")) if r.get("class_id") else r.get("house_group", "")
        if not cls:
            cls = r.get("house", "")

        if kind == "period":
            rows.append(f"<tr><td>{esc(r.get('date',''))}</td><td>{esc(teacher)}</td>"
                        f"<td>Period {esc(r.get('period',''))}</td><td>{esc(subject_name(r.get('subject_id')))}</td>"
                        f"<td>{esc(cls)}</td><td>{n(r,'total')}</td><td>{n(r,'present')}</td>"
                        f"<td>{n(r,'absent')}</td><td>{esc(submitted)}</td></tr>")
        elif kind == "assembly":
            rows.append(f"<tr><td>{esc(r.get('date',''))}</td><td>{esc(teacher)}</td><td>{esc(cls)}</td>"
                        f"<td>{n(r,'total')}</td><td>{n(r,'present_boys')}</td><td>{n(r,'present_girls')}</td>"
                        f"<td>{n(r,'absent_boys')}</td><td>{n(r,'absent_girls')}</td>"
                        f"<td>{n(r,'present')}</td><td>{n(r,'absent')}</td><td>{esc(submitted)}</td></tr>")
        else:
            place = cls + (f" / {r.get('gender')}" if r.get("gender") else "")
            rows.append(f"<tr><td>{esc(r.get('date',''))}</td><td>{esc(teacher)}</td><td>{esc(place)}</td>"
                        f"<td>{n(r,'total')}</td><td>{n(r,'present')}</td><td>{n(r,'absent')}</td>"
                        f"<td>{n(r,'leave')}</td><td>{esc(submitted)}</td></tr>")

    if not rows:
        colspan = 9 if kind == "period" else 11 if kind == "assembly" else 8
        body_rows = f'<tr><td colspan="{colspan}" class="center">No teacher submission yet.</td></tr>'
    else:
        body_rows = "".join(rows)

    if kind == "period":
        table = f'''<table><thead><tr><th>Date</th><th>Teacher</th><th>Period</th><th>Subject</th><th>Class</th><th>Total Student</th><th>Present Student</th><th>Absent Student</th><th>Submitted At</th></tr></thead><tbody>{body_rows}</tbody></table>'''
        summary = f'''<div class="grid"><div class="stat"><span>Total</span><strong>{total}</strong></div><div class="stat"><span>Present</span><strong>{present}</strong></div><div class="stat"><span>Absent</span><strong>{absent}</strong></div><div class="stat"><span>Submissions</span><strong>{submitted_count}</strong></div><div class="stat"><span>Attendance %</span><strong>{pct}%</strong></div></div>'''
    elif kind == "assembly":
        table = f'''<table><thead><tr><th>Date</th><th>Teacher</th><th>Class</th><th>Total</th><th>Present Boys</th><th>Present Girls</th><th>Absent Boys</th><th>Absent Girls</th><th>Total Present</th><th>Total Absent</th><th>Submitted At</th></tr></thead><tbody>{body_rows}</tbody></table>'''
        summary = f'''<div class="grid"><div class="stat"><span>Total</span><strong>{total}</strong></div><div class="stat"><span>Present</span><strong>{present}</strong></div><div class="stat"><span>Absent</span><strong>{absent}</strong></div><div class="stat"><span>Leave</span><strong>{leave}</strong></div><div class="stat"><span>Submissions</span><strong>{submitted_count}</strong></div><div class="stat"><span>Attendance %</span><strong>{pct}%</strong></div></div>'''
    else:
        table = f'''<table><thead><tr><th>Date</th><th>Teacher</th><th>Class / House</th><th>Total</th><th>Present</th><th>Absent</th><th>Leave</th><th>Submitted At</th></tr></thead><tbody>{body_rows}</tbody></table>'''
        summary = f'''<div class="grid"><div class="stat"><span>Total</span><strong>{total}</strong></div><div class="stat"><span>Present</span><strong>{present}</strong></div><div class="stat"><span>Absent</span><strong>{absent}</strong></div><div class="stat"><span>Leave</span><strong>{leave}</strong></div><div class="stat"><span>Submissions</span><strong>{submitted_count}</strong></div><div class="stat"><span>Attendance %</span><strong>{pct}%</strong></div></div>'''

    buttons = ''.join(
        f'<a class="btn {"ok" if k==kind else "gray"}" href="{url_for(route)}">{esc(lbl)}</a>'
        for k, lbl, route in [
            ("period","Period Attendance","principal_period_report"),
            ("assembly","Morning Assembly","principal_assembly_report"),
            ("hostel","Hostel / Night","principal_hostel_report"),
            ("morning_pt","Morning PT","principal_pt_report"),
            ("remedial","Remedial","principal_remedial_report"),
            ("evening_student","Evening Student","principal_evening_student_report"),
            ("evening_games","Evening Games","principal_evening_games_report"),
        ]
    )

    date_form = f'''<form method="get" class="toolbar"><label><b>Date</b></label><input type="date" name="date" value="{esc(selected_date)}"><button>Load</button><a class="btn gray" href="{url_for(request.endpoint)}">All Dates</a></form>'''

    return page(
        f"Principal - {label}",
        f'''<div class="card"><h1>👁 Principal Monitoring — {esc(label)}</h1>
        <p class="small">Teacher द्वारा submitted summary records. Principal यहाँ monitor करता है.</p>
        <div class="toolbar">{buttons}</div>{date_form}</div>
        {summary}
        <div class="card" style="overflow:auto"><h2>{esc(label)} — Teacher Submissions</h2>{table}</div>'''
    )

@app.route("/principal/period-attendance")
@principal_required
def principal_period_report():
    return _principal_monitor_page("period")


@app.route("/principal/daily-attendance")
@principal_required
def principal_daily_report():
    records = daily_records()
    records.sort(key=lambda r: r.get("submitted_at", "") or f"{r.get('date','')} {r.get('time','')}", reverse=True)
    rows=[]
    for r in records:
        rows.append(f"<tr><td>{esc(r.get('date',''))}</td><td>{esc(r.get('teacher_name','Unknown Teacher'))}</td><td>{esc(class_name(r.get('class_id')))}</td><td>{esc(subject_name(r.get('subject_id')))}</td><td>{esc(r.get('period','1'))}</td><td>{esc(r.get('student_name',''))}</td><td>{esc(r.get('status',''))}</td><td>{esc(r.get('submitted_at') or r.get('time',''))}</td></tr>")
    body=''.join(rows) or '<tr><td colspan="8" class="center">No Daily Attendance submitted by teachers yet.</td></tr>'
    buttons=''.join(f'<a class="btn {"ok" if k=="daily" else "gray"}" href="{url_for(route)}">{lbl}</a>' for k,lbl,route in [("daily","Daily Attendance","principal_daily_report"),("period","Period Attendance","principal_period_report"),("assembly","Morning Assembly","principal_assembly_report"),("hostel","Hostel / Night","principal_hostel_report"),("morning_pt","Morning PT","principal_pt_report"),("remedial","Remedial","principal_remedial_report"),("evening_student","Evening Student","principal_evening_student_report"),("evening_games","Evening Games","principal_evening_games_report")])
    return page("Principal - Daily Attendance", f'''<div class="card"><h1>📅 Principal — Daily Attendance</h1><p class="small">Teacher द्वारा भेजे गए student-wise records.</p><div class="toolbar">{buttons}</div></div><div class="card" style="overflow:auto"><table><thead><tr><th>Date</th><th>Teacher</th><th>Class</th><th>Subject</th><th>Period</th><th>Student</th><th>Status</th><th>Submitted At</th></tr></thead><tbody>{body}</tbody></table></div>''')


@app.route("/principal/assembly")
@principal_required
def principal_assembly_report():
    return _principal_monitor_page("assembly")


@app.route("/principal/hostel-night")
@principal_required
def principal_hostel_report():
    return _principal_monitor_page("hostel")


@app.route("/principal/morning-pt")
@principal_required
def principal_pt_report():
    return _principal_monitor_page("morning_pt")


@app.route("/principal/remedial")
@principal_required
def principal_remedial_report():
    return _principal_monitor_page("remedial")


@app.route("/principal/evening-student")
@principal_required
def principal_evening_student_report():
    return _principal_monitor_page("evening_student")


@app.route("/principal/evening-games")
@principal_required
def principal_evening_games_report():
    return _principal_monitor_page("evening_games")


# ============================================================
# 10. PRINCIPAL ALL ATTENDANCE
# ============================================================

@app.route("/principal/submissions")
@principal_required
def principal_submissions():
    # Kept only as a compatibility redirect; the old all-in-one table is removed.
    return redirect(url_for("principal_period_report"))


@principal_required
def principal_submissions():
    sources=[("Period Attendance","attendance"),("Morning Assembly","assembly_attendance"),("Hostel / Night","hostel_attendance"),("Morning PT","morning_pt_attendance"),("Remedial","remedial_attendance"),("Evening Student","evening_student_attendance"),("Evening Games","evening_games_attendance")]
    all_rows=[]
    for label,key in sources:
        for r in safe_load(key,[]):
            if r.get("summary"):
                all_rows.append((label,r))
    all_rows.sort(key=lambda x:x[1].get("submitted_at","") or f"{x[1].get('date','')} {x[1].get('time','')}", reverse=True)
    rows=""
    for label,r in all_rows:
        place=class_name(r.get("class_id")) if r.get("class_id") else r.get("house_group","")
        if r.get("module")=="Period Attendance": place=f"{place} / {subject_name(r.get('subject_id'))} / Period {r.get('period')}"
        elif r.get("gender"): place=f"{place} / {r.get('gender')}"
        rows+=f'<tr><td>{esc(label)}</td><td>{esc(r.get("teacher_name", ""))}</td><td>{esc(place)}</td><td>{esc(r.get("date", ""))}</td><td>{esc(r.get("submitted_at", ""))}</td><td>{esc(r.get("total",0))}</td><td>{esc(r.get("present",0))}</td><td>{esc(r.get("absent", r.get("leave",0)))}</td><td>{esc(r.get("leave",0))}</td></tr>'
    if not rows: rows='<tr><td colspan="9" class="center">No teacher submissions yet.</td></tr>'
    return page("Teacher Submissions",f'''<div class="card"><h1>👨‍🏫 Teacher Submitted Attendance</h1><p class="small">Principal को हर teacher का नाम और exact submission date/time दिखाई देगा।</p><div class="toolbar"><a class="btn" href="{url_for('export_all_attendance')}">⬇ Download CSV</a></div></div><div class="card" style="overflow:auto"><table><thead><tr><th>Module</th><th>Teacher</th><th>Class / House</th><th>Date</th><th>Submitted At</th><th>Total</th><th>Present</th><th>Absent</th><th>Leave</th></tr></thead><tbody>{rows}</tbody></table></div>''')


@app.route(
    "/principal/attendance"
)
@principal_required
def principal_attendance():

    rs = attendance_records()

    date_filter = request.args.get(
        "date",
        ""
    )

    class_filter = request.args.get(
        "class_id",
        ""
    )

    subject_filter = request.args.get(
        "subject_id",
        ""
    )

    if date_filter:

        rs = [
            r for r in rs
            if r.get("date")
            == date_filter
        ]

    if class_filter:

        rs = [
            r for r in rs
            if r.get("class_id")
            == class_filter
        ]

    if subject_filter:

        rs = [
            r for r in rs
            if r.get("subject_id")
            == subject_filter
        ]

    optsC = (
        '<option value="">All Classes</option>'
        +
        "".join(

            f"""
            <option value="{esc(c["id"])}">
            {esc(class_name(c["id"]))}
            </option>
            """

            for c in classes()
        )
    )

    optsS = (
        '<option value="">All Subjects</option>'
        +
        "".join(

            f"""
            <option value="{esc(s["id"])}">
            {esc(s["name"])}
            </option>
            """

            for s in subjects()
        )
    )

    rows = ""

    for r in reversed(rs):

        rows += f"""

        <tr>

        <td>{esc(r.get("date"))}</td>

        <td>{esc(r.get("teacher_name"))}</td>

        <td>
        {esc(class_name(r.get("class_id")))}
        </td>

        <td>
        {esc(subject_name(r.get("subject_id")))}
        </td>

        <td>{esc(r.get("period"))}</td>

        <td>{esc(r.get("student_name"))}</td>

        <td>{esc(r.get("status"))}</td>

        </tr>

        """

    return page(

        "All Attendance",

        f"""

        <div class="card">

        <h1>
        📋 All Period Attendance
        </h1>

        <form class="toolbar"
        method="get">

        <input
        type="date"
        name="date"
        value="{esc(date_filter)}"
        >

        <select name="class_id">
        {optsC}
        </select>

        <select name="subject_id">
        {optsS}
        </select>

        <button>
        Filter
        </button>

        <a
        class="btn gray"
        href="{url_for('principal_attendance')}"
        >
        Reset
        </a>

        <a
        class="btn ok"
        href="{url_for('export_attendance')}"
        >
        Export CSV
        </a>

        </form>

        <p class="small">
        Showing {len(rs)} records.
        </p>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>Date</th>
        <th>Teacher</th>
        <th>Class</th>
        <th>Subject</th>
        <th>Period</th>
        <th>Student</th>
        <th>Status</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# 11. CLASS REPORT
# ============================================================

@app.route(
    "/principal/report"
)
@principal_required
def principal_report():

    rs = attendance_records()

    rows = ""

    for c in classes():

        x = [

            r for r in rs

            if r.get("class_id")
            == c.get("id")

        ]

        total = len(x)

        p = sum(
            r.get("status")
            == "Present"
            for r in x
        )

        a = sum(
            r.get("status")
            == "Absent"
            for r in x
        )

        l = sum(
            r.get("status")
            == "Late"
            for r in x
        )

        pct = percentage(
            p,
            total
        )

        rows += f"""

        <tr>

        <td>
        {esc(c["name"])}
        </td>

        <td>
        {esc(c.get("section",""))}
        </td>

        <td>{total}</td>

        <td>{p}</td>

        <td>{a}</td>

        <td>{l}</td>

        <td class="{status_class(pct)}">
        {pct}%
        </td>

        </tr>

        """

    return page(

        "Reports",

        f"""

        <div class="card">

        <h1>
        📊 Class-wise Attendance Report
        </h1>

        <table>

        <tr>

        <th>Class</th>
        <th>Section</th>
        <th>Total</th>
        <th>Present</th>
        <th>Absent</th>
        <th>Late</th>
        <th>Present %</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# 12. STUDENT REPORT
# ============================================================

@app.route(
    "/principal/student-report"
)
@principal_required
def student_report():

    st = students()

    rs = attendance_records()

    q = request.args.get(
        "q",
        ""
    ).strip().lower()

    cid = request.args.get(
        "class_id",
        ""
    )

    minpct = request.args.get(
        "minpct",
        ""
    )

    visible = [

        s for s in st

        if (
            not cid
            or s.get("class_id")
            == cid
        )

        and (

            not q

            or q in s.get(
                "name",
                ""
            ).lower()

            or q in s.get(
                "roll",
                ""
            ).lower()

            or q in s.get(
                "admission_no",
                ""
            ).lower()

        )

    ]

    rows = ""

    for s in visible:

        student_rows = [

            r for r in rs

            if r.get("student_id")
            == s["id"]

        ]

        total = len(
            student_rows
        )

        p = sum(
            r.get("status")
            == "Present"
            for r in student_rows
        )

        ab = sum(
            r.get("status")
            == "Absent"
            for r in student_rows
        )

        la = sum(
            r.get("status")
            == "Late"
            for r in student_rows
        )

        pct = percentage(
            p,
            total
        )

        if minpct:

            try:

                if pct >= float(minpct):
                    continue

            except:
                pass

        rows += f"""

        <tr>

        <td>{esc(s.get("roll"))}</td>

        <td>{esc(s.get("name"))}</td>

        <td>
        {esc(class_name(s.get("class_id")))}
        </td>

        <td>{total}</td>

        <td>{p}</td>

        <td>{ab}</td>

        <td>{la}</td>

        <td class="{status_class(pct)}">
        {pct}%
        </td>

        </tr>

        """

    opts = (
        '<option value="">All Classes</option>'
        +
        "".join(

            f"""
            <option
            value="{esc(c["id"])}"
            {"selected" if c["id"] == cid else ""}
            >
            {esc(class_name(c["id"]))}
            </option>
            """

            for c in classes()
        )
    )

    return page(

        "Student Report",

        f"""

        <div class="card">

        <h1>
        👨‍🎓 Student Attendance Report
        </h1>

        <form
        class="toolbar"
        method="get"
        >

        <input
        name="q"
        value="{esc(q)}"
        placeholder="Search name/roll/admission"
        >

        <select name="class_id">
        {opts}
        </select>

        <input
        name="minpct"
        type="number"
        step="0.1"
        min="0"
        max="100"
        placeholder="Below %"
        value="{esc(minpct)}"
        >

        <button>
        Filter
        </button>

        <a
        class="btn gray"
        href="{url_for('student_report')}"
        >
        Reset
        </a>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>Roll</th>
        <th>Student</th>
        <th>Class</th>
        <th>Total</th>
        <th>Present</th>
        <th>Absent</th>
        <th>Late</th>
        <th>%</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# 13. MONTHLY ATTENDANCE REPORT
# ============================================================

@app.route(
    "/monthly-report"
)
@login_required
def monthly_report():

    month = request.args.get(
        "month",
        date.today().strftime("%Y-%m")
    )

    rs = attendance_records()

    rs = [

        r for r in rs

        if str(r.get("date", ""))
        .startswith(month)

    ]

    rows = ""

    for s in students():

        x = [

            r for r in rs

            if r.get("student_id")
            == s["id"]

        ]

        total = len(x)

        p = sum(
            r.get("status")
            == "Present"
            for r in x
        )

        a = sum(
            r.get("status")
            == "Absent"
            for r in x
        )

        l = sum(
            r.get("status")
            == "Late"
            for r in x
        )

        pct = percentage(
            p,
            total
        )

        rows += f"""

        <tr>

        <td>{esc(s["roll"])}</td>

        <td>{esc(s["name"])}</td>

        <td>
        {esc(class_name(s["class_id"]))}
        </td>

        <td>{total}</td>

        <td>{p}</td>

        <td>{a}</td>

        <td>{l}</td>

        <td class="{status_class(pct)}">
        {pct}%
        </td>

        </tr>

        """

    return page(

        "Monthly Report",

        f"""

        <div class="card">

        <h1>
        📅 Monthly Attendance Report
        </h1>

        <form>

        <input
        type="month"
        name="month"
        value="{esc(month)}"
        >

        <button>
        Load Month
        </button>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>Roll</th>
        <th>Student</th>
        <th>Class</th>
        <th>Total Periods</th>
        <th>Present</th>
        <th>Absent</th>
        <th>Late</th>
        <th>%</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# 14. LOW ATTENDANCE
# ============================================================

@app.route(
    "/low-attendance"
)
@login_required
def low_attendance():

    try:

        limit = float(
            request.args.get(
                "limit",
                "75"
            )
        )

    except:

        limit = 75

    rs = attendance_records()

    rows = ""

    for s in students():

        x = [

            r for r in rs

            if r.get("student_id")
            == s["id"]

        ]

        if not x:
            continue

        p = sum(
            r.get("status")
            == "Present"
            for r in x
        )

        pct = percentage(
            p,
            len(x)
        )

        if pct < limit:

            rows += f"""

            <tr>

            <td>
            {esc(s["roll"])}
            </td>

            <td>
            {esc(s["name"])}
            </td>

            <td>
            {esc(
                class_name(
                    s["class_id"]
                )
            )}
            </td>

            <td>{len(x)}</td>

            <td>{p}</td>

            <td class="bad">
            {pct}%
            </td>

            </tr>

            """

    return page(

        "Low Attendance",

        f"""

        <div class="card">

        <h1>
        ⚠️ Low Attendance Students
        </h1>

        <form>

        <input
        type="number"
        name="limit"
        min="0"
        max="100"
        step="0.1"
        value="{limit}"
        >

        <button>
        Show Below %
        </button>

        </form>

        </div>

        <div class="card">

        <table>

        <tr>

        <th>Roll</th>
        <th>Student</th>
        <th>Class</th>
        <th>Total</th>
        <th>Present</th>
        <th>Attendance %</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )


# ============================================================
# 15. MY ATTENDANCE HISTORY
# ============================================================

@app.route(
    "/my-attendance"
)
@login_required
def my_attendance():

    u = current_user()

    if u["role"] == "principal":

        return redirect(
            url_for(
                "principal_attendance"
            )
        )

    t = teacher_profile(
        u["id"]
    )

    rs = [

        r for r in attendance_records()

        if t
        and r.get("teacher_id")
        == t.get("id")

    ]

    rows = "".join(

        f"""

        <tr>

        <td>{esc(r.get("date"))}</td>

        <td>{esc(r.get("period"))}</td>

        <td>
        {esc(
            class_name(
                r.get("class_id")
            )
        )}
        </td>

        <td>
        {esc(
            subject_name(
                r.get("subject_id")
            )
        )}
        </td>

        <td>{esc(r.get("student_name"))}</td>

        <td>{esc(r.get("status"))}</td>

        </tr>

        """

        for r in reversed(rs)
    )

    return page(

        "My Attendance",

        f"""

        <div class="card">

        <h1>
        My Attendance History
        </h1>

        <input
        id="hq"
        onkeyup="filterTable('hq','historyTable')"
        placeholder="Search..."
        >

        <table id="historyTable">

        <tr>

        <th>Date</th>
        <th>Period</th>
        <th>Class</th>
        <th>Subject</th>
        <th>Student</th>
        <th>Status</th>

        </tr>

        {rows}

        </table>

        </div>

        """
    )



# ============================================================
# 15B. COMPLETE ATTENDANCE REPORTS
# ============================================================

REPORT_SOURCES = {
    "daily": ("Student Daily Attendance", "daily_attendance"),
    "period": ("Period / Subject Attendance", "attendance"),
    "assembly": ("Morning Assembly", "assembly_attendance"),
    "hostel": ("Hostel / Night", "hostel_attendance"),
    "mess": ("Mess Attendance", "mess_attendance"),
    "morning_pt": ("Morning PT", "morning_pt_attendance"),
    "remedial": ("Remedial", "remedial_attendance"),
    "evening_student": ("Evening Student", "evening_student_attendance"),
    "evening_games": ("Evening Games", "evening_games_attendance"),
    "staff": ("Staff Attendance", "staff_attendance"),
    "leave": ("Leave / Permission", "leave_records"),
    "medical": ("Medical Records", "medical_records"),
}


def report_records(source):
    info = REPORT_SOURCES.get(source)
    if not info:
        return []
    return safe_load(info[1], [])


def report_attended(status, source):
    status = str(status or "").strip().lower()

    if source == "mess":
        return status in {"taken", "present", "yes", "served"}

    if source == "staff":
        return status in {"present", "late"}

    if source in {"leave", "medical"}:
        return False

    return status in {"present", "late"}


@app.route("/attendance-reports")
@principal_required
def attendance_reports():

    source = request.args.get("source", "daily")
    if source not in REPORT_SOURCES:
        source = "daily"

    label, kind = REPORT_SOURCES[source]
    records = report_records(source)

    selected_date = request.args.get("date", "")
    if selected_date:
        records = [
            r for r in records
            if str(r.get("date", "")) == selected_date
        ]

    # Teachers see only their assigned classes where the record has class_id.
    u = current_user()
    allowed_classes = None
    if u.get("role") == "teacher":
        t = teacher_profile(u.get("id"))
        allowed_classes = {
            a.get("class_id")
            for a in assignments()
            if t and a.get("teacher_id") == t.get("id")
        }
        records = [
            r for r in records
            if not r.get("class_id") or r.get("class_id") in allowed_classes
        ]

    present = sum(report_attended(r.get("status"), source) for r in records)
    absent = sum(
        str(r.get("status", "")).lower() in {"absent", "not taken", "no"}
        for r in records
    )
    late = sum(str(r.get("status", "")).lower() == "late" for r in records)
    total = len(records)
    pct = percentage(present, total)

    # Module-wise summary for the complete system.
    summary_rows = []
    for key, (name, _) in REPORT_SOURCES.items():
        rr = report_records(key)
        if selected_date:
            rr = [r for r in rr if str(r.get("date", "")) == selected_date]
        if allowed_classes is not None:
            rr = [
                r for r in rr
                if not r.get("class_id") or r.get("class_id") in allowed_classes
            ]

        rp = sum(report_attended(r.get("status"), key) for r in rr)
        ra = sum(
            str(r.get("status", "")).lower() in {"absent", "not taken", "no"}
            for r in rr
        )
        summary_rows.append(
            f"""
            <tr>
                <td>{esc(name)}</td>
                <td>{len(rr)}</td>
                <td>{rp}</td>
                <td>{ra}</td>
                <td>{percentage(rp, len(rr))}%</td>
            </tr>
            """
        )

    source_opts = "".join(
        f'<option value="{esc(k)}" {"selected" if k == source else ""}>{esc(v[0])}</option>'
        for k, v in REPORT_SOURCES.items()
    )

    return page("All Attendance Reports", f"""
    <div class="card">
        <h1>📊 Complete Attendance Reports</h1>
        <p class="small">
            One report screen for Daily, Period, Assembly, Hostel/Night,
            Mess, Morning PT, Remedial, Evening Student, Evening Games,
            Staff, Leave and Medical records.
        </p>

        <form method="get" class="toolbar">
            <select name="source">{source_opts}</select>
            <input type="date" name="date" value="{esc(selected_date)}">
            <button>Load Report</button>
            <a class="btn gray" href="{url_for('attendance_reports')}">Reset</a>
            <a class="btn ok" href="{url_for('export_all_attendance')}">CSV Export</a>
        </form>
    </div>

    <div class="grid">
        <div class="stat"><span>Selected Module</span><strong>{esc(label)}</strong></div>
        <div class="stat"><span>Total Records</span><strong>{total}</strong></div>
        <div class="stat"><span>Present/Attended</span><strong>{present}</strong></div>
        <div class="stat"><span>Absent</span><strong>{absent}</strong></div>
        <div class="stat"><span>Late</span><strong>{late}</strong></div>
        <div class="stat"><span>Attendance %</span><strong>{pct}%</strong></div>
    </div>

    <div class="card">
        <h2 class="section-title">Module Summary</h2>
        <table>
            <thead>
                <tr>
                    <th>Module</th>
                    <th>Records</th>
                    <th>Present/Attended</th>
                    <th>Absent</th>
                    <th>%</th>
                </tr>
            </thead>
            <tbody>
                {''.join(summary_rows) or '<tr><td colspan="5" class="center">No records found.</td></tr>'}
            </tbody>
        </table>
    </div>
    """)


# ============================================================
# 15C. HOUSE-WISE ATTENDANCE
# ============================================================

@app.route("/house-wise-report")
@principal_required
def house_wise_report():
    """House-wise report supporting student and summary attendance records."""
    source = request.args.get("source", "daily")
    allowed_sources = {"daily","period","assembly","hostel","morning_pt","remedial","evening_student","evening_games"}
    if source not in allowed_sources:
        source = "daily"

    adate = request.args.get("date", "") or str(date.today())
    records = [r for r in report_records(source) if str(r.get("date","")) == adate]

    students_data = [
        s for s in students()
        if str(s.get("status","Active")).lower() != "inactive"
        and str(s.get("house","")).strip()
    ]
    by_student = {r.get("student_id"): r for r in records if r.get("student_id")}
    summary_records = [r for r in records if r.get("summary")]

    houses = {str(s.get("house","")).strip() for s in students_data}
    for r in summary_records:
        h = str(r.get("house","")).strip() or str(r.get("house_group","")).strip().split(" ")[0]
        if h:
            houses.add(h)

    rows = []
    grand = {"total":0,"present":0,"absent":0,"leave":0}

    for house in sorted(houses):
        hs = [s for s in students_data if str(s.get("house","")).strip() == house]
        house_summary = [r for r in summary_records if str(r.get("house","")).strip() == house or str(r.get("house_group","")).strip().split(" ")[0] == house]

        if house_summary and not by_student:
            total = sum(max(0,int(r.get("total",0) or 0)) for r in house_summary)
            present = sum(max(0,int(r.get("present",0) or 0)) for r in house_summary)
            leave = sum(max(0,int(r.get("leave",0) or 0)) for r in house_summary)
            explicit_absent = sum(max(0,int(r.get("absent",0) or 0)) for r in house_summary)
            absent = explicit_absent if explicit_absent else max(0,total-present-leave)
        else:
            total, present, leave, absent = len(hs), 0, 0, 0
            for st in hs:
                r = by_student.get(st.get("id"), {})
                status = str(r.get("status","Absent")).strip().lower()
                if status in {"present","late"}:
                    present += 1
                elif status == "leave":
                    leave += 1
                else:
                    absent += 1

        grand["total"] += total
        grand["present"] += present
        grand["absent"] += absent
        grand["leave"] += leave
        pct = percentage(present,total)
        rows.append(f'<tr><td><b>{esc(house)}</b></td><td>{total}</td><td>{present}</td><td>{absent}</td><td>{leave}</td><td class="{status_class(pct)}">{pct}%</td></tr>')

    gpct = percentage(grand["present"], grand["total"])
    source_opts = "".join(f'<option value="{esc(k)}" {"selected" if k==source else ""}>{esc(v[0])}</option>' for k,v in REPORT_SOURCES.items() if k in allowed_sources)

    return page("House-wise Attendance", f'''
    <div class="card"><h1>🏠 House-wise Attendance</h1>
    <p class="small">House-wise report अब summary records (Hostel/PT/Remedial/Evening) और student-level records दोनों से calculation करता है.</p>
    <form method="get" class="toolbar"><select name="source">{source_opts}</select><input type="date" name="date" value="{esc(adate)}"><button>Load House Report</button><a class="btn gray" href="{url_for("house_wise_report")}">Today</a></form></div>

    <div class="grid"><div class="stat"><span>Grand Total</span><strong>{grand["total"]}</strong></div>
    <div class="stat"><span>Present</span><strong>{grand["present"]}</strong></div>
    <div class="stat"><span>Absent</span><strong>{grand["absent"]}</strong></div>
    <div class="stat"><span>Leave</span><strong>{grand["leave"]}</strong></div>
    <div class="stat"><span>Attendance %</span><strong>{gpct}%</strong></div></div>

    <div class="card" style="overflow:auto"><table><thead><tr><th>House</th><th>Total Students</th><th>Present</th><th>Absent / Not Marked</th><th>Leave</th><th>Attendance %</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="6" class="center">No house/student data available for this date.</td></tr>'}</tbody></table></div>
    ''')

# ============================================================
# 15D. CSV EXPORT - ALL MODULES
# ============================================================

@app.route("/export/all-attendance.csv")
@principal_required
def export_all_attendance():

    out = io.StringIO()
    writer = csv.writer(out)

    writer.writerow([
        "Module", "Date", "Time", "Class", "Section",
        "House", "Student", "Roll", "Employee ID",
        "Meal", "Subject", "Period", "Status",
        "Marked By", "Reason/Complaint", "Remarks"
    ])

    student_map = {s.get("id"): s for s in students()}
    class_map = {c.get("id"): c for c in classes()}
    teacher_map = {t.get("id"): t for t in teachers()}

    for source, (label, kind) in REPORT_SOURCES.items():
        records = report_records(source)

        for r in records:
            sid = r.get("student_id")
            tid = r.get("teacher_id")
            s = student_map.get(sid, {})
            c = class_map.get(r.get("class_id"), {})
            t = teacher_map.get(tid, {})

            writer.writerow([
                label,
                r.get("date", ""),
                r.get("time", ""),
                c.get("name", ""),
                c.get("section", ""),
                r.get("house", s.get("house", "")),
                r.get("student_name", s.get("name", "")),
                r.get("roll", s.get("roll", "")),
                r.get("employee_id", t.get("employee_id", "")),
                r.get("meal", ""),
                subject_name(r.get("subject_id", "")) if r.get("subject_id") else "",
                r.get("period", ""),
                r.get("status", ""),
                r.get("marked_by", r.get("teacher_name", "")),
                r.get("reason", r.get("complaint", "")),
                r.get("remarks", r.get("remark", "")),
            ])

    return Response(
        "\ufeff" + out.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=jnv_complete_attendance_report.csv"
        }
    )


# ============================================================
# 15E. FRIENDLY ERROR HANDLING
# ============================================================

def error_page(code, title, message):
    return render_template_string("""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{{ code }} - JNV Attendance</title>
        <style>
            body{margin:0;background:#f3f6fb;font-family:Segoe UI,Arial,sans-serif;color:#172033}
            .box{max-width:650px;margin:90px auto;background:#fff;padding:35px;border-radius:16px;
                 box-shadow:0 4px 18px #00000012;text-align:center}
            .code{font-size:64px;font-weight:800;color:#1769aa}
            a{display:inline-block;padding:10px 16px;border-radius:8px;background:#1769aa;color:#fff;
              text-decoration:none;margin:5px}
            .gray{background:#64748b}
        </style>
    </head>
    <body>
        <div class="box">
            <div class="code">{{ code }}</div>
            <h1>{{ title }}</h1>
            <p>{{ message }}</p>
            <a href="{{ url_for('dashboard') if me else url_for('login') }}">
                🏠 Go to Home
            </a>
            {% if me %}
            <a class="gray" href="{{ url_for('logout') }}">Logout</a>
            {% endif %}
        </div>
    </body>
    </html>
    """, code=code, title=title, message=message, me=current_user())


@app.errorhandler(404)
def handle_404(error):
    return error_page(
        404,
        "Page not found",
        "The page you requested does not exist or the link is no longer available."
    ), 404


@app.errorhandler(500)
def handle_500(error):
    # Keep the user-facing message clean; details remain in Flask's server log.
    return error_page(
        500,
        "Something went wrong",
        "The system could not complete this request. Please return to the dashboard and try again."
    ), 500


@app.errorhandler(400)
def handle_400(error):
    return error_page(
        400,
        "Invalid request",
        "Some submitted data was invalid or incomplete. Please go back and try again."
    ), 400



# ============================================================
# 16. CSV EXPORT
# ============================================================

@app.route(
    "/export/attendance.csv"
)
@principal_required
def export_attendance():

    out = io.StringIO()

    w = csv.writer(out)

    w.writerow([

        "Date",
        "Time",
        "Teacher",
        "Teacher Username",
        "Class",
        "Subject",
        "Student",
        "Roll",
        "Status",
        "Period"

    ])

    st = {
        s["id"]: s
        for s in students()
    }

    for r in attendance_records():

        s = st.get(
            r.get("student_id"),
            {}
        )

        w.writerow([

            r.get("date"),

            r.get("time"),

            r.get("teacher_name"),

            r.get("teacher_username"),

            class_name(
                r.get("class_id")
            ),

            subject_name(
                r.get("subject_id")
            ),

            r.get("student_name"),

            s.get(
                "roll",
                ""
            ),

            r.get("status"),

            r.get("period")

        ])

    return Response(

        "\ufeff"
        + out.getvalue(),

        mimetype="text/csv",

        headers={
            "Content-Disposition":
            "attachment; "
            "filename=attendance_report.csv"
        }

    )


# ============================================================
# 17. BACKUP
# ============================================================

@app.route("/backup")
@principal_required
def backup_data():

    folder = backup_all()

    flash(
        "Backup created: " + folder
    )

    return redirect(
        url_for("dashboard")
    )


# ============================================================
# 18. CHANGE PASSWORD
# ============================================================

@app.route(
    "/change-password",
    methods=["GET", "POST"]
)
@login_required
def change_password():

    u = current_user()

    if request.method == "POST":

        old = request.form.get(
            "old",
            ""
        )

        new = request.form.get(
            "new",
            ""
        )

        confirm = request.form.get(
            "confirm",
            ""
        )

        if old != u.get("password"):

            flash(
                "Old password गलत है."
            )

        elif len(new) < 4:

            flash(
                "New password कम से कम 4 characters का रखें."
            )

        elif new != confirm:

            flash(
                "New password और confirm password अलग हैं."
            )

        else:

            us = users()

            for x in us:

                if x.get("id") == u.get("id"):

                    x["password"] = new

            save(
                "users",
                us
            )

            flash(
                "Password changed successfully."
            )

            return redirect(
                url_for("dashboard")
            )

    body = """

    <div class="card login">

    <h1>
    Change Password
    </h1>

    <form method="post">

    <input
    name="old"
    type="password"
    placeholder="Old password"
    required
    >

    <input
    name="new"
    type="password"
    placeholder="New password"
    required
    >

    <input
    name="confirm"
    type="password"
    placeholder="Confirm new password"
    required
    >

    <button>
    Change Password
    </button>

    </form>

    </div>

    """

    return page(
        "Change Password",
        body
    )



# ============================================================
# 19. MOD DUTY / TEMPORARY MOD LOGIN
# ============================================================
# 19. MOD DUTY / TEMPORARY MOD LOGIN + EMAIL CREDENTIALS
# ============================================================
# MOD account is created by Principal.
# Principal can see Teacher, Gmail, Login ID and Password.
# The same Login ID + Password are sent to the MOD teacher's email.
# MOD login is valid on the assigned duty date and remains valid
# for that date only.
#
# Gmail configuration:
#   JNV_GMAIL_ADDRESS       = sender Gmail address
#   JNV_GMAIL_APP_PASSWORD  = Gmail 16-character App Password
#
# Windows PowerShell example:
#   $env:JNV_GMAIL_ADDRESS="yourgmail@gmail.com"
#   $env:JNV_GMAIL_APP_PASSWORD="abcdefghijklmnop"
#
# IMPORTANT: Use a Google App Password, NOT your normal Gmail password.

MOD_HOUSES = [
    "Aravali Jr Boys", "Aravali Jr Girls",
    "Aravali Sr Boys", "Aravali Sr Girls",
    "Nilgiri Jr Boys", "Nilgiri Jr Girls",
    "Nilgiri Sr Boys", "Nilgiri Sr Girls",
    "Shivalik Jr Boys", "Shivalik Jr Girls",
    "Shivalik Sr Boys", "Shivalik Sr Girls",
    "Udayagiri Jr Boys", "Udayagiri Jr Girls",
]

MOD_CLASSES = [
    f"{cls}{sec}"
    for cls in range(6, 13)
    for sec in ("A", "B")
]

MOD_MODULES = {
    "morning_pt": "Morning PT",
    "assembly": "Assembly",
    "mess": "Mess",
    "remedial": "Remedial",
    "evening_games": "Evening Games",
    "evening_study": "Evening Study",
    "night": "Night Attendance",
}


def mods():
    return safe_load("mods", [])


def mod_attendance_records():
    return safe_load("mod_attendance", [])


def mod_current_user():
    u = current_user()

    if not u or u.get("role") != "mod":
        return None

    duty = next(
        (
            m for m in mods()
            if m.get("user_id") == u.get("id")
        ),
        None
    )

    if not duty:
        return None

    try:
        duty_date = str(duty.get("duty_date", "")).strip()

        # MOD login is valid only on the assigned date.
        if str(date.today()) != duty_date:
            return None

    except Exception:
        return None

    return duty


def mod_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):

        duty = mod_current_user()

        if not duty:
            session.pop("user_id", None)

            flash(
                "MOD login expired or duty date is not valid."
            )

            return redirect(
                url_for("mod_login")
            )

        return fn(*args, **kwargs)

    return wrapper


def mod_login_user(username, password):

    username = (username or "").strip()
    password = password or ""

    for u in users():

        if (
            u.get("role") == "mod"
            and u.get("username") == username
            and u.get("password") == password
        ):
            return u

    return None


def gmail_settings():
    """
    Read Gmail sender and App Password from environment variables.

    We intentionally do NOT use a normal Gmail password.
    Google App Password is required when SMTP is used this way.
    """

    sender = os.environ.get(
        "JNV_GMAIL_ADDRESS",
        ""
    ).strip()

    app_password = os.environ.get(
        "JNV_GMAIL_APP_PASSWORD",
        ""
    ).strip()

    # Google displays App Passwords with spaces sometimes.
    app_password = app_password.replace(" ", "")

    return sender, app_password


def send_mod_email(
    to_email,
    teacher_name,
    duty_date,
    username,
    password
):
    """
    Send MOD ID/password by Gmail.

    First tries Gmail STARTTLS on port 587.
    If that fails, tries Gmail SSL on port 465.
    """

    to_email = (to_email or "").strip()
    teacher_name = (teacher_name or "").strip()

    sender, app_password = gmail_settings()

    if not to_email:
        return False, "MOD teacher Gmail address is empty."

    if "@" not in to_email:
        return False, "Invalid MOD teacher Gmail address."

    if not sender:
        return False, (
            "Sender Gmail is not configured. "
            "Set JNV_GMAIL_ADDRESS."
        )

    if not app_password:
        return False, (
            "Gmail App Password is not configured. "
            "Set JNV_GMAIL_APP_PASSWORD."
        )

    login_url = (
        request.host_url.rstrip("/")
        + "/mod-login"
    )

    msg = EmailMessage()

    msg["Subject"] = (
        "JNV MOD Duty - Login ID & Password"
    )

    msg["From"] = sender
    msg["To"] = to_email

    msg.set_content(
        f"""
Jawahar Navodaya Vidyalaya
MOD DUTY LOGIN DETAILS

Dear {teacher_name},

You have been assigned MOD duty.

MOD Duty Date:
{duty_date}

MOD Login ID:
{username}

MOD Password:
{password}

MOD Login Page:
{login_url}

Important:
- This login is valid only on the assigned duty date.
- Keep your Login ID and Password confidential.
- Do not share these credentials with another person.

JNV Attendance Management System
"""
    )

    errors = []

    # Method 1: Gmail SMTP STARTTLS / 587
    try:

        with smtplib.SMTP(
            "smtp.gmail.com",
            587,
            timeout=25
        ) as smtp:

            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()

            smtp.login(
                sender,
                app_password
            )

            smtp.send_message(msg)

        return True, (
            f"ID और Password successfully sent to "
            f"{to_email}"
        )

    except Exception as e:

        errors.append(
            f"SMTP 587: {str(e)}"
        )

    # Method 2: Gmail SMTP SSL / 465
    try:

        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=25
        ) as smtp:

            smtp.ehlo()

            smtp.login(
                sender,
                app_password
            )

            smtp.send_message(msg)

        return True, (
            f"ID और Password successfully sent to "
            f"{to_email}"
        )

    except Exception as e:

        errors.append(
            f"SMTP SSL 465: {str(e)}"
        )

    return False, (
        "Gmail send failed. "
        + " | ".join(errors)
    )


def principal_mod_credentials_card():
    """
    Small MOD credentials summary shown on Principal dashboard.
    """

    ms = sorted(
        mods(),
        key=lambda x: (
            str(x.get("duty_date", "")),
            str(x.get("created_at", ""))
        ),
        reverse=True
    )

    if not ms:
        return """
        <div class="card">
            <h2>🛡️ MOD Login Credentials</h2>
            <p class="small">
                अभी कोई MOD account create नहीं हुआ है.
            </p>
            <a class="btn purple"
               href="/principal/mod">
                Create MOD Login
            </a>
        </div>
        """

    rows = ""

    today = str(date.today())

    for m in ms:

        duty_date = str(
            m.get("duty_date", "")
        )

        if duty_date == today:
            status = (
                '<span class="badge ok">'
                'ACTIVE TODAY'
                '</span>'
            )
        elif duty_date > today:
            status = (
                '<span class="badge">'
                'UPCOMING'
                '</span>'
            )
        else:
            status = (
                '<span class="badge gray">'
                'EXPIRED'
                '</span>'
            )

        rows += f"""
        <tr>
            <td>{esc(m.get("teacher_name", ""))}</td>
            <td>{esc(m.get("email", ""))}</td>
            <td>{esc(duty_date)}</td>
            <td>
                <b>{esc(m.get("username", ""))}</b>
            </td>
            <td>
                <b>{esc(m.get("password", ""))}</b>
            </td>
            <td>{status}</td>
        </tr>
        """

    return f"""
    <div class="card" style="overflow:auto">
        <div class="toolbar">
            <h2 style="margin-right:auto">
                🛡️ MOD Login Credentials
            </h2>

            <a class="btn purple"
               href="{url_for('principal_mod_management')}">
                Manage MOD
            </a>

            <a class="btn"
               href="{url_for('principal_mod_report')}">
                MOD Attendance
            </a>
        </div>

        <p class="small">
            Principal को सभी MOD की Login ID और Password यहां
            दिखाई देंगे. वही credentials MOD teacher के Gmail पर
            भी भेजे जाते हैं.
        </p>

        <table>
            <thead>
                <tr>
                    <th>Teacher</th>
                    <th>Gmail</th>
                    <th>Duty Date</th>
                    <th>MOD Login ID</th>
                    <th>Password</th>
                    <th>Status</th>
                </tr>
            </thead>

            <tbody>
                {rows}
            </tbody>
        </table>
    </div>
    """


@app.route(
    "/mod-login",
    methods=["GET", "POST"]
)
def mod_login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        u = mod_login_user(
            username,
            password
        )

        if not u:

            flash(
                "MOD ID या password गलत है."
            )

        else:

            duty = next(
                (
                    m for m in mods()
                    if m.get("user_id")
                    == u.get("id")
                ),
                None
            )

            if not duty:

                flash(
                    "MOD duty record नहीं मिला."
                )

            elif str(date.today()) != str(
                duty.get("duty_date", "")
            ):

                flash(
                    "यह MOD login आज valid नहीं है. "
                    f"Duty date: {duty.get('duty_date', '')}"
                )

            else:

                session.clear()
                session["user_id"] = u["id"]

                return redirect(
                    url_for("mod_dashboard")
                )

    return page(
        "MOD Login",
        """
        <div class="card login">

            <h1 class="center">
                🛡️ MOD Login
            </h1>

            <p class="center small">
                Assigned MOD duty date पर ही login valid है.
            </p>

            <form method="post">

                <input
                    name="username"
                    placeholder="MOD Login ID"
                    style="width:100%"
                    required
                >

                <input
                    name="password"
                    type="password"
                    placeholder="MOD Password"
                    style="width:100%"
                    required
                >

                <button
                    style="width:100%;margin-top:8px"
                >
                    MOD Login
                </button>

            </form>

            <p class="center">
                <a class="btn gray"
                   href="/"
                >
                    Principal / Teacher Login
                </a>
            </p>

        </div>
        """
    )


@app.route(
    "/principal/mod",
    methods=["GET", "POST"]
)
@principal_required
def principal_mod_management():

    us = users()
    ts = teachers()
    ms = mods()

    if request.method == "POST":

        action = request.form.get(
            "action",
            "create"
        ).strip()

        # --------------------------------------------------------
        # RESEND EXISTING MOD CREDENTIALS
        # --------------------------------------------------------
        if action == "resend":

            mod_id = request.form.get(
                "mod_id",
                ""
            ).strip()

            m = next(
                (
                    x for x in ms
                    if x.get("id") == mod_id
                ),
                None
            )

            if not m:

                flash(
                    "MOD record नहीं मिला."
                )

                return redirect(
                    url_for("principal_mod_management")
                )

            email_ok, email_status = send_mod_email(
                m.get("email", ""),
                m.get("teacher_name", ""),
                m.get("duty_date", ""),
                m.get("username", ""),
                m.get("password", "")
            )

            if email_ok:
                flash(
                    "✅ " + email_status
                )
            else:
                flash(
                    "❌ " + email_status
                )

            return redirect(
                url_for("principal_mod_management")
            )

        # --------------------------------------------------------
        # DELETE MOD ACCOUNT
        # --------------------------------------------------------
        if action == "delete":

            mod_id = request.form.get(
                "mod_id",
                ""
            ).strip()

            m = next(
                (
                    x for x in ms
                    if x.get("id") == mod_id
                ),
                None
            )

            if m:

                ms = [
                    x for x in ms
                    if x.get("id") != mod_id
                ]

                us = [
                    x for x in us
                    if x.get("id")
                    != m.get("user_id")
                ]

                save("mods", ms)
                save("users", us)

                flash(
                    "MOD account deleted."
                )

            return redirect(
                url_for("principal_mod_management")
            )

        # --------------------------------------------------------
        # CREATE MOD ACCOUNT
        # --------------------------------------------------------
        teacher_id = request.form.get(
            "teacher_id",
            ""
        ).strip()

        duty_date = request.form.get(
            "duty_date",
            ""
        ).strip()

        teacher_email = request.form.get(
            "teacher_email",
            ""
        ).strip().lower()

        teacher = next(
            (
                t for t in ts
                if t.get("id") == teacher_id
            ),
            None
        )

        if not teacher or not duty_date or not teacher_email:

            flash(
                "Teacher, MOD duty date और Gmail address जरूरी हैं."
            )

            return redirect(
                url_for("principal_mod_management")
            )

        try:

            datetime.strptime(
                duty_date,
                "%Y-%m-%d"
            )

        except ValueError:

            flash(
                "Valid MOD duty date चुनें."
            )

            return redirect(
                url_for("principal_mod_management")
            )

        if duty_date < str(date.today()):

            flash(
                "Past date की MOD duty नहीं बनाई जा सकती."
            )

            return redirect(
                url_for("principal_mod_management")
            )

        # One active MOD login per teacher/date.
        old = next(
            (
                m for m in ms
                if m.get("teacher_id")
                == teacher_id
                and str(m.get("duty_date", ""))
                == duty_date
            ),
            None
        )

        if old:

            flash(
                "इस teacher की इस date के लिए "
                "MOD login पहले से बना हुआ है. "
                "नीचे Resend button से credentials फिर भेज सकते हैं."
            )

            return redirect(
                url_for("principal_mod_management")
            )

        stamp = datetime.now().strftime(
            "%Y%m%d%H%M%S"
        )

        uid = next_id(
            "U",
            us
        )

        mid = next_id(
            "M",
            ms
        )

        username = (
            f"mod_{stamp}"
        )

        # Unique password for each MOD.
        password = (
            "Mod@"
            + datetime.now().strftime(
                "%d%m%H%M%S"
            )
        )

        now = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        us.append({

            "id": uid,

            "username": username,

            "password": password,

            "role": "mod",

            "name":
                f"MOD - {teacher.get('name', '')}"

        })

        ms.append({

            "id": mid,

            "user_id": uid,

            "teacher_id": teacher_id,

            "teacher_name":
                teacher.get("name", ""),

            "email":
                teacher_email,

            "duty_date":
                duty_date,

            "username":
                username,

            "password":
                password,

            "email_status":
                "Sending...",

            "created_at":
                now,

            "last_email_sent_at":
                ""

        })

        save(
            "users",
            us
        )

        save(
            "mods",
            ms
        )

        # Send exact same credentials to the MOD teacher.
        email_ok, email_status = send_mod_email(
            teacher_email,
            teacher.get("name", ""),
            duty_date,
            username,
            password
        )

        # Update email status.
        ms = mods()

        created = next(
            (
                x for x in ms
                if x.get("id") == mid
            ),
            None
        )

        if created:

            created["email_status"] = (
                "Sent"
                if email_ok
                else email_status
            )

            if email_ok:

                created["last_email_sent_at"] = (
                    datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

            save(
                "mods",
                ms
            )

        return page(
            "MOD Created",
            f"""
            <div class="card">

                <h1>
                    {'✅' if email_ok else '⚠️'}
                    MOD Login Created
                </h1>

                <p>
                    <b>Teacher:</b>
                    {esc(teacher.get("name", ""))}
                </p>

                <p>
                    <b>Duty Date:</b>
                    {esc(duty_date)}
                </p>

                <p>
                    <b>Teacher Gmail:</b>
                    {esc(teacher_email)}
                </p>

                <hr>

                <p>
                    <b>MOD Login ID:</b>
                    <span class="badge">
                        {esc(username)}
                    </span>
                </p>

                <p>
                    <b>Password:</b>
                    <span class="badge">
                        {esc(password)}
                    </span>
                </p>

                <div class="alert">
                    <b>Email Status:</b>
                    {esc(email_status)}
                </div>

                <p class="small">
                    Principal के MOD page और dashboard पर
                    यही ID/password दिखाई देंगे.
                    MOD teacher को भी यही credentials email किए गए हैं.
                </p>

                <a
                    class="btn"
                    href="{url_for('principal_mod_management')}"
                >
                    MOD Management
                </a>

                <a
                    class="btn purple"
                    href="{url_for('principal_mod_report')}"
                >
                    MOD Attendance Report
                </a>

            </div>
            """
        )

    teacher_opts = "".join(
        f"""
        <option value="{esc(t["id"])}">
            {esc(t.get("name", ""))}
        </option>
        """
        for t in ts
    )

    rows = ""

    for m in sorted(
        ms,
        key=lambda x: (
            str(x.get("duty_date", "")),
            str(x.get("created_at", ""))
        ),
        reverse=True
    ):

        status = str(
            m.get("email_status", "")
        )

        if status == "Sent":

            email_badge = (
                '<span class="badge ok">'
                'EMAIL SENT'
                '</span>'
            )

        elif status:

            email_badge = (
                '<span class="badge">'
                'EMAIL ERROR'
                '</span>'
            )

        else:

            email_badge = (
                '<span class="badge">'
                'NOT SENT'
                '</span>'
            )

        rows += f"""
        <tr>

            <td>
                {esc(m.get("teacher_name", ""))}
            </td>

            <td>
                {esc(m.get("duty_date", ""))}
            </td>

            <td>
                {esc(m.get("email", ""))}
            </td>

            <td>
                <b>
                    {esc(m.get("username", ""))}
                </b>
            </td>

            <td>
                <b>
                    {esc(m.get("password", ""))}
                </b>
            </td>

            <td>
                {email_badge}
                <br>
                <span class="small">
                    {esc(m.get("last_email_sent_at", ""))}
                </span>
            </td>

            <td>

                <form
                    method="post"
                    style="display:inline"
                >

                    <input
                        type="hidden"
                        name="action"
                        value="resend"
                    >

                    <input
                        type="hidden"
                        name="mod_id"
                        value="{esc(m.get("id", ""))}"
                    >

                    <button class="ok">
                        📧 Resend
                    </button>

                </form>

                <form
                    method="post"
                    style="display:inline"
                    onsubmit="return confirmDelete()"
                >

                    <input
                        type="hidden"
                        name="action"
                        value="delete"
                    >

                    <input
                        type="hidden"
                        name="mod_id"
                        value="{esc(m.get("id", ""))}"
                    >

                    <button class="danger">
                        Delete
                    </button>

                </form>

            </td>

        </tr>
        """

    if not rows:

        rows = """
        <tr>
            <td colspan="7" class="center">
                No MOD duty created yet.
            </td>
        </tr>
        """

    return page(
        "MOD Management",
        f"""
        <div class="card">

            <h1>
                🛡️ MOD Duty Management
            </h1>

            <p class="small">
                Principal teacher की MOD duty और
                date-based temporary login बना सकता है.
                Create करने पर Login ID + Password
                Principal को दिखाई देंगे और उसी MOD teacher
                के Gmail पर भी भेजे जाएंगे.
            </p>

            <form method="post">

                <input
                    type="hidden"
                    name="action"
                    value="create"
                >

                <label>
                    <b>MOD Teacher</b>
                </label>

                <select
                    name="teacher_id"
                    required
                >
                    {teacher_opts}
                </select>

                <label>
                    <b>MOD Duty Date</b>
                </label>

                <input
                    type="date"
                    name="duty_date"
                    min="{date.today()}"
                    required
                >

                <label>
                    <b>Teacher Gmail</b>
                </label>

                <input
                    type="email"
                    name="teacher_email"
                    placeholder="teacher@gmail.com"
                    required
                >

                <br>

                <button class="ok">
                    🛡️ Create MOD Login + Send Email
                </button>

            </form>

        </div>

        <div class="card">

            <h2>
                🔐 Existing MOD Credentials
            </h2>

            <p class="small">
                Principal के लिए Login ID और Password
                यहां हमेशा दिखाई देंगे. Resend से वही
                credentials फिर से Gmail पर भेज सकते हैं.
            </p>

            <div style="overflow:auto">

                <table>

                    <thead>

                        <tr>
                            <th>Teacher</th>
                            <th>Duty Date</th>
                            <th>Gmail</th>
                            <th>MOD Login ID</th>
                            <th>Password</th>
                            <th>Email Status</th>
                            <th>Action</th>
                        </tr>

                    </thead>

                    <tbody>
                        {rows}
                    </tbody>

                </table>

            </div>

        </div>

        <div class="card">

            <h2>
                📧 Gmail Setup
            </h2>

            <p class="small">
                Email भेजने के लिए Gmail App Password
                configure होना जरूरी है.
                Normal Gmail password का इस्तेमाल न करें.
            </p>

            <p>
                <b>Environment variables:</b>
                JNV_GMAIL_ADDRESS
                और
                JNV_GMAIL_APP_PASSWORD
            </p>

        </div>
        """
    )


@app.route("/mod")
@mod_required
def mod_dashboard():

    duty = mod_current_user()

    return page(
        "MOD Dashboard",
        f"""
        <div class="card">

            <h1>
                🛡️ MOD Attendance
            </h1>

            <p>
                <b>Teacher:</b>
                {esc(duty.get("teacher_name", ""))}
            </p>

            <p>
                <b>Duty Date:</b>
                {esc(duty.get("duty_date", ""))}
            </p>

            <p>
                <b>MOD Login ID:</b>
                {esc(duty.get("username", ""))}
            </p>

            <div class="grid">

                {''.join(
                    f'<a class="btn" href="{url_for("mod_attendance", module=k)}">'
                    f'{esc(v)}</a>'
                    for k, v in MOD_MODULES.items()
                )}

            </div>

        </div>

        <div class="card">

            <p class="small">
                Save करने पर data JSON में store होगा और
                Principal के MOD Attendance Report में
                दिखाई देगा.
            </p>

            <a
                class="btn gray"
                href="{url_for('logout')}"
            >
                Logout
            </a>

        </div>
        """
    )


@app.route(
    "/mod/attendance/<module>",
    methods=["GET", "POST"]
)
@mod_required
def mod_attendance(module):

    duty = mod_current_user()

    if module not in MOD_MODULES:

        return redirect(
            url_for("mod_dashboard")
        )

    adate = str(date.today())

    if request.method == "POST":

        adate = request.form.get(
            "att_date",
            str(date.today())
        ).strip()

    if adate != str(date.today()):

        flash(
            "MOD attendance केवल assigned duty date "
            "पर save हो सकती है."
        )

        return redirect(
            url_for("mod_dashboard")
        )

    data = mod_attendance_records()

    existing = next(
        (
            r for r in reversed(data)
            if r.get("module") == module
            and r.get("date") == adate
            and r.get("mod_user_id")
            == duty.get("user_id")
        ),
        None
    )

    if module in {
        "morning_pt",
        "evening_games",
        "night"
    }:

        groups = MOD_HOUSES
        defaults = (60, 50)

    elif module in {
        "remedial",
        "assembly"
    }:

        groups = MOD_CLASSES
        defaults = (40, 35)

    elif module == "evening_study":

        groups = []

        for c in MOD_CLASSES:

            groups += [
                f"{c} Boys",
                f"{c} Girls"
            ]

        defaults = (20, 18)

    else:

        groups = None

    if groups is not None:

        saved = {
            x.get("name"): x
            for x in (
                existing.get("rows", [])
                if existing
                else []
            )
        }

        rows = []

        for name in groups:

            x = saved.get(
                name,
                {}
            )

            try:
                t = max(
                    0,
                    int(
                        x.get(
                            "total",
                            defaults[0]
                        ) or defaults[0]
                    )
                )

                p = min(
                    t,
                    max(
                        0,
                        int(
                            x.get(
                                "present",
                                defaults[1]
                            ) or defaults[1]
                        )
                    )
                )

                l = min(
                    t - p,
                    max(
                        0,
                        int(
                            x.get(
                                "leave",
                                t - p
                            ) or 0
                        )
                    )
                )

            except (TypeError, ValueError):

                t, p, l = (
                    defaults[0],
                    min(defaults[0], defaults[1]),
                    0
                )

            rows.append({
                "name": name,
                "total": t,
                "present": p,
                "leave": l
            })

        if request.method == "POST":

            rows = []

            for i, name in enumerate(groups):

                try:

                    t = max(
                        0,
                        int(
                            request.form.get(
                                f"total_{i}",
                                str(defaults[0])
                            )
                        )
                    )

                    p = max(
                        0,
                        int(
                            request.form.get(
                                f"present_{i}",
                                str(defaults[1])
                            )
                        )
                    )

                    l = max(
                        0,
                        int(
                            request.form.get(
                                f"leave_{i}",
                                "0"
                            )
                        )
                    )

                except (TypeError, ValueError):

                    t, p, l = 0, 0, 0

                p = min(p, t)
                l = min(l, t - p)

                rows.append({
                    "name": name,
                    "total": t,
                    "present": p,
                    "leave": l
                })

            data = [
                r for r in data
                if not (
                    r.get("module") == module
                    and r.get("date") == adate
                    and r.get("mod_user_id")
                    == duty.get("user_id")
                )
            ]

            now = datetime.now()

            data.append({

                "id":
                    next_id("MA", data),

                "module":
                    module,

                "module_name":
                    MOD_MODULES[module],

                "date":
                    adate,

                "submitted_at":
                    now.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),

                "mod_user_id":
                    duty.get("user_id"),

                "teacher_id":
                    duty.get("teacher_id"),

                "teacher_name":
                    duty.get("teacher_name"),

                "rows":
                    rows,

                "total":
                    sum(
                        x["total"]
                        for x in rows
                    ),

                "present":
                    sum(
                        x["present"]
                        for x in rows
                    ),

                "leave":
                    sum(
                        x["leave"]
                        for x in rows
                    ),

                "absent":
                    sum(
                        x["total"]
                        - x["present"]
                        - x["leave"]
                        for x in rows
                    )

            })

            save(
                "mod_attendance",
                data
            )

            flash(
                f"{MOD_MODULES[module]} saved successfully."
            )

            return redirect(
                url_for("mod_dashboard")
            )

        return mod_rows_page(
            module,
            duty,
            adate,
            rows,
            (
                "House"
                if module in {
                    "morning_pt",
                    "evening_games",
                    "night"
                }
                else (
                    "Class / Gender"
                    if module == "evening_study"
                    else "Class"
                )
            )
        )

    if module == "mess":

        meals = [
            "Breakfast",
            "Lunch",
            "Dinner"
        ]

        old = (
            existing.get("meals", [])
            if existing
            else []
        )

        sm = {
            x.get("meal"): x
            for x in old
        }

        meal_rows = []

        for m in meals:

            x = sm.get(
                m,
                {}
            )

            try:

                t = max(
                    0,
                    int(
                        x.get(
                            "total",
                            60
                        ) or 60
                    )
                )

                p = min(
                    t,
                    max(
                        0,
                        int(
                            x.get(
                                "present",
                                50
                            ) or 50
                        )
                    )
                )

                l = min(
                    t - p,
                    max(
                        0,
                        int(
                            x.get(
                                "leave",
                                t - p
                            ) or 0
                        )
                    )
                )

            except (TypeError, ValueError):

                t, p, l = 60, 50, 0

            meal_rows.append({
                "meal": m,
                "total": t,
                "present": p,
                "leave": l
            })

        if request.method == "POST":

            meal_rows = []

            for i, m in enumerate(meals):

                try:

                    t = max(
                        0,
                        int(
                            request.form.get(
                                f"m_total_{i}",
                                "60"
                            )
                        )
                    )

                    p = max(
                        0,
                        int(
                            request.form.get(
                                f"m_present_{i}",
                                "0"
                            )
                        )
                    )

                    l = max(
                        0,
                        int(
                            request.form.get(
                                f"m_leave_{i}",
                                "0"
                            )
                        )
                    )

                except (TypeError, ValueError):

                    t, p, l = 0, 0, 0

                p = min(p, t)
                l = min(l, t - p)

                meal_rows.append({
                    "meal": m,
                    "total": t,
                    "present": p,
                    "leave": l
                })

            data = [
                r for r in data
                if not (
                    r.get("module") == module
                    and r.get("date") == adate
                    and r.get("mod_user_id")
                    == duty.get("user_id")
                )
            ]

            now = datetime.now()

            data.append({

                "id":
                    next_id("MA", data),

                "module":
                    "mess",

                "module_name":
                    "Mess",

                "date":
                    adate,

                "submitted_at":
                    now.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),

                "mod_user_id":
                    duty.get("user_id"),

                "teacher_id":
                    duty.get("teacher_id"),

                "teacher_name":
                    duty.get("teacher_name"),

                "meals":
                    meal_rows,

                "total":
                    sum(
                        x["total"]
                        for x in meal_rows
                    ),

                "present":
                    sum(
                        x["present"]
                        for x in meal_rows
                    ),

                "leave":
                    sum(
                        x["leave"]
                        for x in meal_rows
                    ),

                "absent":
                    sum(
                        x["total"]
                        - x["present"]
                        - x["leave"]
                        for x in meal_rows
                    )

            })

            save(
                "mod_attendance",
                data
            )

            flash(
                "Mess attendance saved successfully."
            )

            return redirect(
                url_for("mod_dashboard")
            )

        tr = "".join(
            f"""
            <tr>
                <td>
                    <b>{esc(x["meal"])}</b>
                </td>

                <td>
                    <input
                        name="m_total_{i}"
                        type="number"
                        min="0"
                        value="{x["total"]}"
                        oninput="calcMeal({i})"
                    >
                </td>

                <td>
                    <input
                        name="m_present_{i}"
                        type="number"
                        min="0"
                        value="{x["present"]}"
                        oninput="calcMeal({i})"
                    >
                </td>

                <td>
                    <input
                        name="m_leave_{i}"
                        type="number"
                        min="0"
                        value="{x["leave"]}"
                        oninput="calcMeal({i})"
                    >
                </td>

                <td>
                    <input
                        id="ma{i}"
                        readonly
                        value="{x["total"] - x["present"] - x["leave"]}"
                    >
                </td>
            </tr>
            """
            for i, x in enumerate(meal_rows)
        )

        return page(
            "Mess",
            f"""
            <div class="card">

                <h1>
                    🍽️ Mess Attendance
                </h1>

                <p>
                    <b>Teacher:</b>
                    {esc(duty.get("teacher_name", ""))}
                    &nbsp;
                    <b>Date:</b>
                    {esc(adate)}
                </p>

            </div>

            <div class="card" style="overflow:auto">

                <form method="post">

                    <input
                        type="hidden"
                        name="att_date"
                        value="{esc(adate)}"
                    >

                    <table>

                        <thead>
                            <tr>
                                <th>Meal</th>
                                <th>Total</th>
                                <th>Present</th>
                                <th>Leave</th>
                                <th>Absent</th>
                            </tr>
                        </thead>

                        <tbody>
                            {tr}
                        </tbody>

                    </table>

                    <br>

                    <button class="ok">
                        💾 Save
                    </button>

                </form>

            </div>

            <script>
            function calcMeal(i) {{

                let t = Math.max(
                    0,
                    +document.querySelector(
                        '[name="m_total_' + i + '"]'
                    ).value || 0
                );

                let p = Math.min(
                    t,
                    Math.max(
                        0,
                        +document.querySelector(
                            '[name="m_present_' + i + '"]'
                        ).value || 0
                    )
                );

                let l = Math.min(
                    t - p,
                    Math.max(
                        0,
                        +document.querySelector(
                            '[name="m_leave_' + i + '"]'
                        ).value || 0
                    )
                );

                document.querySelector(
                    '[name="m_present_' + i + '"]'
                ).value = p;

                document.querySelector(
                    '[name="m_leave_' + i + '"]'
                ).value = l;

                document.getElementById(
                    'ma' + i
                ).value = t - p - l;

            }}
            </script>
            """
        )

    return redirect(
        url_for("mod_dashboard")
    )


def mod_rows_page(
    module,
    duty,
    adate,
    rows,
    label
):

    T = sum(
        x["total"]
        for x in rows
    )

    P = sum(
        x["present"]
        for x in rows
    )

    L = sum(
        x["leave"]
        for x in rows
    )

    tr = "".join(
        f"""
        <tr>

            <td>
                <b>{esc(x["name"])}</b>
            </td>

            <td>
                <input
                    name="total_{i}"
                    type="number"
                    min="0"
                    value="{x["total"]}"
                    oninput="calcRow({i})"
                >
            </td>

            <td>
                <input
                    name="present_{i}"
                    type="number"
                    min="0"
                    value="{x["present"]}"
                    oninput="calcRow({i})"
                >
            </td>

            <td>
                <input
                    name="leave_{i}"
                    type="number"
                    min="0"
                    value="{x["leave"]}"
                    oninput="calcRow({i})"
                >
            </td>

            <td>
                <input
                    id="a{i}"
                    readonly
                    value="{x["total"] - x["present"] - x["leave"]}"
                >
            </td>

        </tr>
        """
        for i, x in enumerate(rows)
    )

    return page(
        MOD_MODULES[module],
        f"""
        <div class="card">

            <h1>
                🛡️ {esc(MOD_MODULES[module])}
            </h1>

            <p>
                <b>Teacher:</b>
                {esc(duty.get("teacher_name", ""))}
                &nbsp;
                <b>Date:</b>
                {esc(adate)}
            </p>

            <p class="small">
                {esc(label)} attendance.
                Present और Leave editable हैं;
                Absent automatic है.
            </p>

        </div>

        <div class="card" style="overflow:auto">

            <form method="post">

                <input
                    type="hidden"
                    name="att_date"
                    value="{esc(adate)}"
                >

                <table>

                    <thead>

                        <tr>
                            <th>{esc(label)}</th>
                            <th>Total Student</th>
                            <th>Present</th>
                            <th>Leave</th>
                            <th>Absent</th>
                        </tr>

                    </thead>

                    <tbody>
                        {tr}
                    </tbody>

                    <tfoot>

                        <tr>
                            <th>Total</th>
                            <th id="grandTotal">{T}</th>
                            <th id="grandPresent">{P}</th>
                            <th id="grandLeave">{L}</th>
                            <th id="grandAbsent">{T-P-L}</th>
                        </tr>

                    </tfoot>

                </table>

                <br>

                <button class="ok">
                    💾 Save
                </button>

                <a
                    class="btn gray"
                    href="{url_for('mod_download', module=module)}"
                >
                    ⬇ Download
                </a>

            </form>

        </div>

        <script>
        function calcRow(i) {{

            let t = Math.max(
                0,
                +document.querySelector(
                    '[name="total_' + i + '"]'
                ).value || 0
            );

            let p = Math.min(
                t,
                Math.max(
                    0,
                    +document.querySelector(
                        '[name="present_' + i + '"]'
                    ).value || 0
                )
            );

            let l = Math.min(
                t - p,
                Math.max(
                    0,
                    +document.querySelector(
                        '[name="leave_' + i + '"]'
                    ).value || 0
                )
            );

            document.querySelector(
                '[name="present_' + i + '"]'
            ).value = p;

            document.querySelector(
                '[name="leave_' + i + '"]'
            ).value = l;

            document.getElementById(
                'a' + i
            ).value = t - p - l;

            let T = 0;
            let P = 0;
            let L = 0;

            document.querySelectorAll(
                '[name^="total_"]'
            ).forEach(
                x => T += +x.value || 0
            );

            document.querySelectorAll(
                '[name^="present_"]'
            ).forEach(
                x => P += +x.value || 0
            );

            document.querySelectorAll(
                '[name^="leave_"]'
            ).forEach(
                x => L += +x.value || 0
            );

            document.getElementById(
                'grandTotal'
            ).innerText = T;

            document.getElementById(
                'grandPresent'
            ).innerText = P;

            document.getElementById(
                'grandLeave'
            ).innerText = L;

            document.getElementById(
                'grandAbsent'
            ).innerText = Math.max(
                0,
                T - P - L
            );

        }}
        </script>
        """
    )


@app.route(
    "/mod/download/<module>"
)
@mod_required
def mod_download(module):

    if module not in MOD_MODULES:

        return redirect(
            url_for("mod_dashboard")
        )

    duty = mod_current_user()

    records = [
        r
        for r in mod_attendance_records()
        if r.get("module") == module
        and r.get("mod_user_id")
        == duty.get("user_id")
    ]

    out = io.StringIO()

    w = csv.writer(out)

    if module == "mess":

        w.writerow([
            "Date",
            "Teacher",
            "Meal",
            "Total",
            "Present",
            "Leave",
            "Submitted At"
        ])

        for r in records:

            for x in r.get(
                "meals",
                []
            ):

                w.writerow([
                    r.get("date"),
                    r.get("teacher_name"),
                    x.get("meal"),
                    x.get("total"),
                    x.get("present"),
                    x.get("leave"),
                    r.get("submitted_at")
                ])

    else:

        w.writerow([
            "Date",
            "Teacher",
            "Module",
            "House/Class",
            "Total",
            "Present",
            "Leave",
            "Submitted At"
        ])

        for r in records:

            for x in r.get(
                "rows",
                []
            ):

                w.writerow([
                    r.get("date"),
                    r.get("teacher_name"),
                    r.get("module_name"),
                    x.get("name"),
                    x.get("total"),
                    x.get("present"),
                    x.get("leave"),
                    r.get("submitted_at")
                ])

    return Response(
        "\ufeff" + out.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            f"attachment; "
            f"filename=mod_{module}_attendance.csv"
        }
    )


@app.route(
    "/principal/mod-attendance"
)
@principal_required
def principal_mod_report():

    module = request.args.get(
        "module",
        ""
    ).strip()

    date_filter = request.args.get(
        "date",
        ""
    ).strip()

    records = mod_attendance_records()

    if module:

        records = [
            r for r in records
            if r.get("module") == module
        ]

    if date_filter:

        records = [
            r for r in records
            if r.get("date") == date_filter
        ]

    records.sort(
        key=lambda r:
            r.get("submitted_at", ""),
        reverse=True
    )

    rows = []

    total = 0
    present = 0
    leave = 0

    for r in records:

        if r.get("module") == "mess":

            for x in r.get(
                "meals",
                []
            ):

                rows.append(
                    f"""
                    <tr>
                        <td>{esc(r.get("date", ""))}</td>
                        <td>{esc(r.get("teacher_name", ""))}</td>
                        <td>{esc(x.get("meal", ""))}</td>
                        <td>Mess</td>
                        <td>{x.get("total", 0)}</td>
                        <td>{x.get("present", 0)}</td>
                        <td>{x.get("leave", 0)}</td>
                        <td>{esc(r.get("submitted_at", ""))}</td>
                    </tr>
                    """
                )

                total += int(
                    x.get("total", 0)
                    or 0
                )

                present += int(
                    x.get("present", 0)
                    or 0
                )

                leave += int(
                    x.get("leave", 0)
                    or 0
                )

        else:

            for x in r.get(
                "rows",
                []
            ):

                rows.append(
                    f"""
                    <tr>
                        <td>{esc(r.get("date", ""))}</td>
                        <td>{esc(r.get("teacher_name", ""))}</td>
                        <td>{esc(r.get("module_name", ""))}</td>
                        <td>{esc(x.get("name", ""))}</td>
                        <td>{x.get("total", 0)}</td>
                        <td>{x.get("present", 0)}</td>
                        <td>{x.get("leave", 0)}</td>
                        <td>{esc(r.get("submitted_at", ""))}</td>
                    </tr>
                    """
                )

                total += int(
                    x.get("total", 0)
                    or 0
                )

                present += int(
                    x.get("present", 0)
                    or 0
                )

                leave += int(
                    x.get("leave", 0)
                    or 0
                )

    if not rows:

        rows = [
            """
            <tr>
                <td colspan="8" class="center">
                    No MOD attendance saved yet.
                </td>
            </tr>
            """
        ]

    opts = "".join(
        f"""
        <option
            value="{esc(k)}"
            {"selected" if k == module else ""}
        >
            {esc(v)}
        </option>
        """
        for k, v in MOD_MODULES.items()
    )

    return page(
        "MOD Attendance Report",
        f"""
        <div class="card">

            <h1>
                🛡️ Principal — MOD Attendance Report
            </h1>

            <form
                method="get"
                class="toolbar"
            >

                <select name="module">

                    <option value="">
                        All MOD Modules
                    </option>

                    {opts}

                </select>

                <input
                    type="date"
                    name="date"
                    value="{esc(date_filter)}"
                >

                <button>
                    Filter
                </button>

                <a
                    class="btn gray"
                    href="{url_for('principal_mod_report')}"
                >
                    Reset
                </a>

            </form>

        </div>

        <div class="grid">

            <div class="stat">
                <span>Total</span>
                <strong>{total}</strong>
            </div>

            <div class="stat">
                <span>Present</span>
                <strong>{present}</strong>
            </div>

            <div class="stat">
                <span>Leave</span>
                <strong>{leave}</strong>
            </div>

            <div class="stat">
                <span>Absent</span>
                <strong>
                    {max(0, total-present-leave)}
                </strong>
            </div>

        </div>

        <div
            class="card"
            style="overflow:auto"
        >

            <table>

                <thead>

                    <tr>
                        <th>Date</th>
                        <th>Teacher</th>
                        <th>Module/Meal</th>
                        <th>House/Class</th>
                        <th>Total</th>
                        <th>Present</th>
                        <th>Leave</th>
                        <th>Submitted At</th>
                    </tr>

                </thead>

                <tbody>
                    {''.join(rows)}
                </tbody>

            </table>

        </div>
        """
    )


# ============================================================
# START APPLICATION
# ============================================================

@app.route("/health")
def health():
    return {"status": "ok", "service": "JNV Attendance"}, 200


if __name__ == "__main__":

    print("=" * 70)

    print(
        "JAWAHAR NAVODAYA VIDYALAYA"
    )

    print(
        "COMPLETE ATTENDANCE MANAGEMENT SYSTEM"
    )

    print("=" * 70)

    print(
        "DATA:",
        DATA_DIR
    )

    print(
        "BACKUPS:",
        BACKUP_DIR
    )

    print()

    print(
        "Principal : principal / principal123"
    )

    print(
        "Teacher 1 : teacher1 / teacher123"
    )

    print(
        "Teacher 2 : teacher2 / teacher123"
    )

    print()

    sender, _ = gmail_settings()

    if sender:

        print(
            "MOD Email Sender:",
            sender
        )

    else:

        print(
            "WARNING: MOD Gmail is NOT configured."
        )

        print(
            "Set JNV_GMAIL_ADDRESS and "
            "JNV_GMAIL_APP_PASSWORD."
        )

    print()

    print(
        "Demo Students:",
        len(STUDENTS)
    )

    print()

    print(
        "Open:"
    )

    print(
        "http://127.0.0.1:8080"
    )

    print("=" * 70)

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        debug=os.environ.get(
            "JNV_DEBUG",
            "0"
        ) == "1"
    )

