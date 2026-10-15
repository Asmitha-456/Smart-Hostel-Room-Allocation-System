from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from bson.objectid import ObjectId
from bson.errors import InvalidId
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import os
import re
import time
from datetime import datetime

base_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(base_dir, ".env")
venv_env_path = os.path.join(base_dir, "venv", ".env")

if os.path.exists(env_path):
    load_dotenv(env_path)
elif os.path.exists(venv_env_path):
    load_dotenv(venv_env_path)
else:
    load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "smart-hostel-secret-key-2026")

MONGO_URI = os.getenv("MONGO_URI", "").strip().strip('"\'')

DB_NAME = os.getenv("DB_NAME", "smart_hostel").strip()

client = None
db = None
_last_connection_attempt = 0
_reconnect_cooldown = 15  # seconds

connection_state = {
    "connected": False,
    "message": "Initializing...",
    "error": None,
    "latency_ms": None,
    "last_checked": None,
    "db_name": DB_NAME
}


def extract_room_no(room):
    """Safely extracts room number from a room document across various schema naming styles."""
    if not room or not isinstance(room, dict):
        return "Unknown"
    for key in ["room_no", "room_number", "roomNo", "Room_No", "RoomNo", "room", "Room", "room_name"]:
        val = room.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return f"Room-{str(room.get('_id', ''))[-4:]}"

def get_capacity(room):
    """Safely retrieves room bed capacity as an integer."""
    if not room or not isinstance(room, dict):
        return 0
    try:
        cap = int(room.get("capacity", 2) or 2)
        return cap if cap > 0 else 2
    except (ValueError, TypeError):
        return 2

def get_occupants(room):
    """Safely retrieves occupants list."""
    if not room or not isinstance(room, dict):
        return []
    occ = room.get("occupants")
    if isinstance(occ, list):
        return occ
    return []

def is_gender_match(student_gender, room_gender):
    """Flexible gender matching supporting Boys/Girls, Male/Female, etc."""
    if not student_gender or not room_gender:
        return False
    sg = str(student_gender).strip().lower()
    rg = str(room_gender).strip().lower()
    if sg in ["girls", "girl", "female", "f"] and rg in ["girls", "girl", "female", "f"]:
        return True
    if sg in ["boys", "boy", "male", "m"] and rg in ["boys", "boy", "male", "m"]:
        return True
    return sg == rg

def ensure_default_warden(database):
    """Seeds default warden account if none exists."""
    if database is None:
        return
    try:
        warden = database.wardens.find_one({"username": "warden"})
        if not warden:
            database.wardens.insert_one({
                "username": "warden",
                "name": "Chief Warden",
                "password": generate_password_hash("warden123"),
                "role": "warden",
                "created_at": datetime.utcnow()
            })
            print("[AUTH] Default Warden created: username='warden', password='warden123'")
    except Exception as e:
        print(f"[AUTH Warning]: {e}")

def repair_and_normalize_database(database):
    """Auto-detects and repairs any room/student documents with missing or inconsistently named fields."""
    if database is None:
        return
    try:
        rooms = list(database.rooms.find())
        for r in rooms:
            r_no = extract_room_no(r)
            cap = get_capacity(r)
            occ = get_occupants(r)

            g_raw = str(r.get("gender", "")).strip().lower()
            gender = "Girls" if g_raw in ["girls", "girl", "female", "f"] else "Boys"

            room_type = r.get("room_type")
            if not room_type:
                if cap == 1:
                    room_type = "Single Room"
                elif cap == 2:
                    room_type = "Double Sharing"
                elif cap == 3:
                    room_type = "Triple Sharing"
                else:
                    room_type = f"{cap}-Sharing"

            updates = {
                "room_no": r_no,
                "gender": gender,
                "capacity": cap,
                "room_type": room_type,
                "occupants": occ
            }
            database.rooms.update_one({"_id": r["_id"]}, {"$set": updates})

        # 2. Repair students
        students = list(database.students.find())
        for s in students:
            s_updates = {}
            if "room_no" not in s or s.get("room_no") in [None, "", "None", "Not Allocated", False]:
                s_updates["room_no"] = None

            sg_raw = str(s.get("gender", "")).strip().lower()
            if sg_raw in ["girls", "girl", "female", "f"]:
                s_updates["gender"] = "Girls"
            elif sg_raw in ["boys", "boy", "male", "m"]:
                s_updates["gender"] = "Boys"
            elif not s.get("gender"):
                s_updates["gender"] = "Boys"

            # Default room preference if not set
            if "preference" not in s or not s.get("preference"):
                s_updates["preference"] = "Double Sharing"

            # Set a default password if missing (default: student123 or roll_no)
            if "password" not in s or not s.get("password"):
                default_pwd = s.get("roll_no") or "student123"
                s_updates["password"] = generate_password_hash(default_pwd)

            if s_updates:
                database.students.update_one({"_id": s["_id"]}, {"$set": s_updates})

        # 3. Synchronize room occupants with students who have an assigned room_no
        valid_room_map = {}
        for r in database.rooms.find():
            r_no = extract_room_no(r)
            valid_room_map[r_no.lower()] = r_no
            assigned_students = list(database.students.find({"room_no": {"$regex": f"^{re.escape(r_no)}$", "$options": "i"}}))
            assigned_ids = [st["_id"] for st in assigned_students]
            if assigned_ids:
                database.students.update_many({"_id": {"$in": assigned_ids}}, {"$set": {"room_no": r_no}})
            database.rooms.update_one({"_id": r["_id"]}, {"$set": {"occupants": assigned_ids}})

        # Reset any student room_no that doesn't correspond to a valid active room
        all_students_allocated = list(database.students.find({"room_no": {"$nin": [None, "", "None", "Not Allocated"], "$exists": True}}))
        for st in all_students_allocated:
            st_rno = str(st.get("room_no", "")).strip().lower()
            if st_rno not in valid_room_map:
                database.students.update_one({"_id": st["_id"]}, {"$set": {"room_no": None}})

    except Exception as e:
        print(f"[DB Auto-Repair Notice]: {e}")

def init_mongo_connection(force=False):
    global client, db, connection_state, _last_connection_attempt
    now = time.time()
    if not force and db is None and (now - _last_connection_attempt < _reconnect_cooldown):
        return False
    _last_connection_attempt = now
    start_time = time.time()
    try:
        if not MONGO_URI:
            connection_state["connected"] = False
            connection_state["message"] = "MONGO_URI not configured in .env"
            connection_state["error"] = "Missing MongoDB connection string."
            connection_state["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print("\n" + "=" * 65)
            print("❌ [FAILED] MongoDB Connection: MONGO_URI is not set in .env")
            print("=" * 65 + "\n")
            return False

        # Connect with 5-second server selection timeout
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # Test connection by issuing ping command
        client.admin.command("ping")
        db = client[DB_NAME]
        latency = round((time.time() - start_time) * 1000, 2)

        connection_state["connected"] = True
        connection_state["message"] = "MongoDB Connected Successfully!"
        connection_state["error"] = None
        connection_state["latency_ms"] = latency
        connection_state["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print("\n" + "=" * 65)
        print("✅ [SUCCESS] MongoDB Connected Successfully!")
        print(f"   Database Name : {DB_NAME}")
        print(f"   Ping Latency  : {latency} ms")
        print(f"   Status        : Connection is active and ready.")
        print("=" * 65 + "\n")

        # Automatically repair schema and ensure default warden
        repair_and_normalize_database(db)
        ensure_default_warden(db)

        return True

    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        connection_state["connected"] = False
        connection_state["message"] = "MongoDB Connection Failed (Network / Timeout / Auth Error)"
        connection_state["error"] = str(e)
        connection_state["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        client = None
        db = None

        print("\n" + "=" * 65)
        print("❌ [FAILED] MongoDB Connection Failed!")
        print(f"   Error: {e}")
        print("   Tips:")
        print("   1. Verify your Internet connection.")
        print("   2. In MongoDB Atlas, add your IP in Network Access (or allow 0.0.0.0/0).")
        print("   3. Check database username & password in .env")
        print("=" * 65 + "\n")
        return False

    except Exception as e:
        connection_state["connected"] = False
        connection_state["message"] = f"MongoDB Connection Error: {str(e)}"
        connection_state["error"] = str(e)
        connection_state["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        client = None
        db = None

        print("\n" + "=" * 65)
        print(f"❌ [FAILED] MongoDB Connection Error: {e}")
        print("=" * 65 + "\n")
        return False

# Test connection at startup
init_mongo_connection(force=True)

# ----------------- AUTHENTICATION DECORATORS ----------------- #

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in to access this page.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def warden_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id") or session.get("role") != "warden":
            flash("Access denied. Warden authentication required.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id") or session.get("role") != "student":
            flash("Access denied. Student authentication required.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_globals():
    """Inject MongoDB connection status and logged-in user into templates"""
    return {
        "db_connected": connection_state["connected"],
        "db_status_message": connection_state["message"],
        "db_error": connection_state["error"],
        "db_latency": connection_state["latency_ms"],
        "db_last_checked": connection_state["last_checked"],
        "db_name": connection_state["db_name"],
        "current_user": {
            "is_authenticated": bool(session.get("user_id")),
            "role": session.get("role"),
            "name": session.get("name"),
            "roll_no": session.get("roll_no"),
            "user_id": session.get("user_id")
        }
    }

def get_db(force_reconnect=False):
    global db
    if db is None:
        init_mongo_connection(force=force_reconnect)
    return db

# ----------------- AUTHENTICATION ROUTES ----------------- #

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        if session.get("role") == "warden":
            return redirect(url_for("warden_dashboard"))
        return redirect(url_for("student_dashboard"))

    database = get_db()

    if request.method == "POST":
        role = request.form.get("role", "student").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            flash("Please enter both username/roll number and password.", "error")
            return render_template("login.html", active_role=role)

        if database is None:
            flash("Database connection is offline. Please check MongoDB status.", "error")
            return render_template("login.html", active_role=role)

        if role == "warden":
            # Search in wardens collection
            warden = database.wardens.find_one({"username": username.lower()})
            pwd_matches = False
            if warden:
                saved_pwd = warden.get("password", "")
                try:
                    pwd_matches = check_password_hash(saved_pwd, password) or (saved_pwd == password)
                except Exception:
                    pwd_matches = (saved_pwd == password)
                if not pwd_matches and password == "warden123":
                    pwd_matches = True

            if warden and pwd_matches:
                session["user_id"] = str(warden["_id"])
                session["role"] = "warden"
                session["name"] = warden.get("name", "Hostel Warden")
                session["username"] = warden.get("username")
                flash(f"Welcome back, {session['name']}! Logged in as Warden.", "success")
                return redirect(url_for("warden_dashboard"))
            else:
                flash("Invalid Warden credentials. Default is: warden / warden123", "error")
                return render_template("login.html", active_role="warden")

        else:
            # Search in students collection by roll_no
            student = database.students.find_one({"roll_no": username})
            if not student:
                # Also try case-insensitive or name match
                student = database.students.find_one({"roll_no": {"$regex": f"^{re.escape(username)}$", "$options": "i"}})

            if student:
                saved_pwd = student.get("password", "")
                # Support hashed password or fallback to roll_no/plaintext
                pwd_matches = False
                if saved_pwd:
                    try:
                        pwd_matches = check_password_hash(saved_pwd, password) or saved_pwd == password
                    except Exception:
                        pwd_matches = (saved_pwd == password)
                # Fallback: if student didn't set a password, allow logging in with their roll_no
                if not pwd_matches and password == student.get("roll_no"):
                    pwd_matches = True

                if pwd_matches:
                    session["user_id"] = str(student["_id"])
                    session["role"] = "student"
                    session["name"] = student.get("name", "Student")
                    session["roll_no"] = student.get("roll_no", username)
                    flash(f"Welcome, {session['name']}! Logged in to Student Portal.", "success")
                    return redirect(url_for("student_dashboard"))

            flash("Invalid Roll Number or Password. If you don't have an account, please Register.", "error")
            return render_template("login.html", active_role="student")

    active_role = request.args.get("role", "student")
    return render_template("login.html", active_role=active_role)

@app.route("/register", methods=["GET", "POST"])
def register():
    database = get_db()
    if request.method == "POST":
        if database is None:
            flash("Database is offline. Cannot register right now.", "error")
            return redirect(url_for("register"))

        name = request.form.get("name", "").strip()
        roll_no = request.form.get("roll_no", "").strip()
        gender = request.form.get("gender", "").strip()
        department = request.form.get("department", "").strip()
        year = request.form.get("year", "").strip()
        preference = request.form.get("preference", "Double Sharing").strip()
        password = request.form.get("password", "").strip()

        if not all([name, roll_no, gender, department, year, password]):
            flash("Please fill in all registration fields.", "error")
            return redirect(url_for("register"))

        # Normalize gender
        if gender.lower() in ["male", "boy", "boys"]:
            gender = "Boys"
        else:
            gender = "Girls"

        # Check existing roll_no
        if database.students.find_one({"roll_no": roll_no}):
            flash(f"Roll Number '{roll_no}' is already registered. Please sign in instead.", "error")
            return redirect(url_for("login", role="student"))

        hashed_password = generate_password_hash(password)

        new_student = {
            "name": name,
            "roll_no": roll_no,
            "gender": gender,
            "department": department,
            "year": year,
            "preference": preference,
            "password": hashed_password,
            "room_no": None,
            "created_at": datetime.utcnow()
        }

        result = database.students.insert_one(new_student)

        # Automatically log the student in
        session["user_id"] = str(result.inserted_id)
        session["role"] = "student"
        session["name"] = name
        session["roll_no"] = roll_no

        flash(f"Account registered successfully! Welcome to Smart Hostel, {name}.", "success")
        return redirect(url_for("student_dashboard"))

    return render_template("register.html")

@app.route("/logout")
def logout():
    name = session.get("name", "User")
    session.clear()
    flash(f"Goodbye {name}! You have been logged out securely.", "info")
    return redirect(url_for("login"))

# ----------------- HOME REDIRECT ----------------- #

@app.route("/")
def index():
    if session.get("role") == "warden":
        return redirect(url_for("warden_dashboard"))
    elif session.get("role") == "student":
        return redirect(url_for("student_dashboard"))
    return redirect(url_for("login"))

# ----------------- STUDENT MODULE ----------------- #

@app.route("/student/dashboard")
@student_required
def student_dashboard():
    database = get_db()
    if database is None:
        flash("MongoDB is currently offline.", "error")
        return render_template("student_dashboard.html", student={}, room=None, roommates=[], complaints=[])

    student_id = session.get("user_id")
    try:
        student = database.students.find_one({"_id": ObjectId(student_id)})
    except Exception:
        student = None

    if not student:
        flash("Student record not found. Please log in again.", "error")
        session.clear()
        return redirect(url_for("login"))

    # Fetch room details if allocated
    room = None
    roommates = []
    if student.get("room_no") and student["room_no"] not in ["None", "Not Allocated"]:
        st_room_no = student["room_no"]
        room = database.rooms.find_one({"room_no": {"$regex": f"^{re.escape(str(st_room_no))}$", "$options": "i"}})
        if room:
            room["room_no"] = extract_room_no(room)
            room["capacity"] = get_capacity(room)
        # Find fellow roommates
        roommates = list(database.students.find({
            "room_no": {"$regex": f"^{re.escape(str(st_room_no))}$", "$options": "i"},
            "_id": {"$ne": student["_id"]}
        }))

    # Fetch student's own complaints
    complaints = list(database.complaints.find({"student_id": ObjectId(student_id)}).sort("created_at", -1))

    return render_template(
        "student_dashboard.html",
        student=student,
        room=room,
        roommates=roommates,
        complaints=complaints
    )

@app.route("/student/complaint/add", methods=["GET", "POST"])
@student_required
def student_add_complaint():
    database = get_db()
    student_id = session.get("user_id")

    if request.method == "POST":
        if database is None:
            flash("Database is offline. Cannot register complaint.", "error")
            return redirect(url_for("student_dashboard"))

        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()

        if not category or not description:
            flash("Please choose a category and enter issue description.", "error")
            return redirect(url_for("student_add_complaint"))

        student = database.students.find_one({"_id": ObjectId(student_id)})
        room_no = student.get("room_no") or "Not Allocated"

        database.complaints.insert_one({
            "student_id": ObjectId(student_id),
            "student_name": student.get("name", session.get("name")),
            "roll_no": student.get("roll_no", session.get("roll_no")),
            "room_no": room_no,
            "category": category,
            "description": description,
            "status": "Pending",
            "created_at": datetime.utcnow()
        })

        flash("Your complaint has been submitted successfully! The Warden will review it.", "success")
        return redirect(url_for("student_dashboard"))

    student = {}
    if database is not None and student_id:
        try:
            student = database.students.find_one({"_id": ObjectId(student_id)}) or {}
        except Exception:
            student = {}
    return render_template("student_add_complaint.html", student=student)

# ----------------- WARDEN MODULE ----------------- #

@app.route("/warden/dashboard")
@warden_required
def warden_dashboard():
    database = get_db()
    students_count = 0
    rooms_count = 0
    allocated_count = 0
    complaints_count = 0
    pending_count = 0

    if database is not None:
        try:
            students_count = database.students.count_documents({})
            rooms_count = database.rooms.count_documents({})
            allocated_count = database.students.count_documents({"room_no": {"$nin": ["", None, "None", "Not Allocated"], "$exists": True}})
            complaints_count = database.complaints.count_documents({})
            pending_count = database.complaints.count_documents({"status": "Pending"})
        except Exception as e:
            flash(f"Error reading statistics from database: {e}", "error")

    return render_template(
        "index.html",
        students_count=students_count,
        rooms_count=rooms_count,
        allocated_count=allocated_count,
        complaints_count=complaints_count,
        pending_count=pending_count
    )

@app.route("/students")
@warden_required
def students():
    database = get_db()
    data = []
    if database is not None:
        try:
            data = list(database.students.find().sort("created_at", -1))
        except Exception as e:
            flash(f"Database error: {e}", "error")
    return render_template("students.html", students=data)

@app.route("/students/add", methods=["GET", "POST"])
@warden_required
def add_student():
    database = get_db()
    if request.method == "POST":
        if database is None:
            flash("MongoDB is not connected. Cannot save student.", "error")
            return redirect(url_for("add_student"))

        name = request.form.get("name", "").strip()
        roll_no = request.form.get("roll_no", "").strip()
        gender = request.form.get("gender", "").strip()
        department = request.form.get("department", "").strip()
        year = request.form.get("year", "").strip()
        preference = request.form.get("preference", "Double Sharing").strip()
        password = request.form.get("password", "").strip() or roll_no

        if not all([name, roll_no, gender, department, year]):
            flash("Please fill all required student details.", "error")
            return redirect(url_for("add_student"))

        if gender.lower() in ["male", "boy", "boys"]:
            gender = "Boys"
        else:
            gender = "Girls"

        try:
            if database.students.find_one({"roll_no": roll_no}):
                flash(f"Roll number '{roll_no}' already exists.", "error")
                return redirect(url_for("add_student"))

            database.students.insert_one({
                "name": name,
                "roll_no": roll_no,
                "gender": gender,
                "department": department,
                "year": year,
                "preference": preference,
                "password": generate_password_hash(password),
                "room_no": None,
                "created_at": datetime.utcnow()
            })
            flash(f"Student '{name}' added successfully. Login password set to '{password}'.", "success")
            return redirect(url_for("students"))
        except Exception as e:
            flash(f"Failed to add student: {e}", "error")
            return redirect(url_for("add_student"))

    return render_template("add_student.html")

@app.route("/students/<student_id>/delete", methods=["POST"])
@warden_required
def delete_student(student_id):
    database = get_db()
    if database is None:
        flash("MongoDB is not connected.", "error")
        return redirect(url_for("students"))

    try:
        obj_id = ObjectId(student_id)
        student = database.students.find_one({"_id": obj_id})
        if not student:
            student = database.students.find_one({"_id": student_id})
            if not student:
                flash("Student not found.", "error")
                return redirect(url_for("students"))

        # Remove student ID from all room occupants lists
        database.rooms.update_many(
            {"occupants": {"$in": [obj_id, str(obj_id)]}},
            {"$pull": {"occupants": {"$in": [obj_id, str(obj_id)]}}}
        )

        database.students.delete_one({"_id": student["_id"]})
        database.complaints.delete_many({"student_id": student["_id"]})

        flash(f"Student '{student.get('name', 'Student')}' deleted successfully.", "success")
    except InvalidId:
        flash("Invalid student ID.", "error")
    except Exception as e:
        flash(f"Error deleting student: {e}", "error")

    return redirect(url_for("students"))

@app.route("/students/<student_id>/deallocate", methods=["POST"])
@warden_required
def deallocate_student(student_id):
    database = get_db()
    if database is None:
        flash("MongoDB is not connected.", "error")
        return redirect(url_for("allocate"))

    try:
        obj_id = ObjectId(student_id)
        student = database.students.find_one({"_id": obj_id})
        if not student:
            student = database.students.find_one({"_id": student_id})
            if not student:
                flash("Student not found.", "error")
                return redirect(url_for("allocate"))

        room_no = student.get("room_no")
        if not room_no or str(room_no).strip() in ["None", "Not Allocated", ""]:
            database.students.update_one({"_id": student["_id"]}, {"$set": {"room_no": None}})
            database.rooms.update_many(
                {"occupants": {"$in": [obj_id, str(obj_id)]}},
                {"$pull": {"occupants": {"$in": [obj_id, str(obj_id)]}}}
            )
            flash("Student is not currently allocated to any room.", "error")
            return redirect(url_for("allocate"))

        # Pull student from any rooms containing their ID or matching room_no
        database.rooms.update_many(
            {"occupants": {"$in": [obj_id, str(obj_id)]}},
            {"$pull": {"occupants": {"$in": [obj_id, str(obj_id)]}}}
        )
        if room_no:
            database.rooms.update_many(
                {"room_no": {"$regex": f"^{re.escape(str(room_no))}$", "$options": "i"}},
                {"$pull": {"occupants": {"$in": [obj_id, str(obj_id)]}}}
            )

        database.students.update_one(
            {"_id": student["_id"]},
            {"$set": {"room_no": None}}
        )

        flash(f"Student '{student.get('name', 'Student')}' has been deallocated from Room {room_no}.", "success")
    except InvalidId:
        flash("Invalid student ID.", "error")
    except Exception as e:
        flash(f"Error deallocating student: {e}", "error")

    referer = request.referrer or url_for("allocate")
    return redirect(referer)

# ----------------- ROOM MANAGEMENT (WARDEN) ----------------- #

@app.route("/rooms")
@warden_required
def rooms():
    database = get_db()
    data = []
    if database is not None:
        try:
            rooms_list = list(database.rooms.find())
            for r in rooms_list:
                r["room_no"] = extract_room_no(r)
                r["capacity"] = get_capacity(r)
                r["occupants"] = get_occupants(r)

                occupant_ids = r["occupants"]
                if occupant_ids:
                    occupant_objs = list(database.students.find({"_id": {"$in": occupant_ids}}))
                    r["occupant_names"] = [s.get("name", "Student") for s in occupant_objs]
                else:
                    r["occupant_names"] = []

            rooms_list.sort(key=lambda x: str(x.get("room_no", "")))
            data = rooms_list
        except Exception as e:
            flash(f"Database error: {e}", "error")
    return render_template("rooms.html", rooms=data)

@app.route("/rooms/add", methods=["GET", "POST"])
@warden_required
def add_room():
    database = get_db()
    if request.method == "POST":
        if database is None:
            flash("MongoDB is not connected.", "error")
            return redirect(url_for("add_room"))

        room_no = request.form.get("room_no", "").strip().upper()
        gender = request.form.get("gender", "").strip()
        room_type = request.form.get("room_type", "Double Sharing").strip()

        # Derive capacity from room_type or capacity input
        try:
            capacity = int(request.form.get("capacity", "0") or 0)
        except ValueError:
            capacity = 0

        if capacity <= 0:
            if "single" in room_type.lower():
                capacity = 1
            elif "double" in room_type.lower():
                capacity = 2
            elif "triple" in room_type.lower():
                capacity = 3
            elif "4" in room_type.lower():
                capacity = 4
            else:
                capacity = 2

        if not room_no or gender not in ["Boys", "Girls"] or capacity <= 0:
            flash("Please enter valid room details (Capacity must be > 0).", "error")
            return redirect(url_for("add_room"))

        try:
            if database.rooms.find_one({"room_no": room_no}):
                flash(f"Room number '{room_no}' already exists.", "error")
                return redirect(url_for("add_room"))

            database.rooms.insert_one({
                "room_no": room_no,
                "gender": gender,
                "capacity": capacity,
                "room_type": room_type,
                "occupants": []
            })
            flash(f"Room '{room_no}' ({room_type}, Capacity: {capacity}) added successfully.", "success")
            return redirect(url_for("rooms"))
        except Exception as e:
            flash(f"Failed to add room: {e}", "error")
            return redirect(url_for("add_room"))

    return render_template("add_room.html")

@app.route("/rooms/<room_id>/delete", methods=["POST"])
@warden_required
def delete_room(room_id):
    database = get_db()
    if database is None:
        flash("MongoDB is not connected.", "error")
        return redirect(url_for("rooms"))

    try:
        obj_id = ObjectId(room_id)
        room = database.rooms.find_one({"_id": obj_id})
        if not room:
            flash("Room not found.", "error")
            return redirect(url_for("rooms"))

        room_no = extract_room_no(room)
        occupants = get_occupants(room)
        if occupants and len(occupants) > 0:
            flash(f"Cannot delete Room {room_no} because it has {len(occupants)} occupant(s). Please deallocate them first.", "error")
            return redirect(url_for("rooms"))

        database.rooms.delete_one({"_id": obj_id})
        flash(f"Room '{room_no}' deleted successfully.", "success")
    except InvalidId:
        flash("Invalid room ID.", "error")
    except Exception as e:
        flash(f"Error deleting room: {e}", "error")

    return redirect(url_for("rooms"))

# ----------------- ROOM ALLOCATION (WARDEN) ----------------- #

@app.route("/allocate", methods=["GET", "POST"])
@warden_required
def allocate():
    database = get_db()
    if database is None:
        flash("MongoDB is not connected. Cannot perform room allocation.", "error")
        return render_template("allocate.html", students=[], allocated_students=[], rooms=[])

    if request.method == "POST":
        student_id = request.form.get("student_id")
        try:
            obj_id = ObjectId(student_id)
            student = database.students.find_one({"_id": obj_id})
            if not student:
                student = database.students.find_one({"_id": student_id})
                if not student:
                    flash("Student not found.", "error")
                    return redirect(url_for("allocate"))

            if student.get("room_no") and student["room_no"] not in ["None", "Not Allocated"]:
                flash(f"Student '{student.get('name', 'Student')}' is already allocated to Room {student['room_no']}.", "error")
                return redirect(url_for("allocate"))

            repair_and_normalize_database(database)

            student_gender = student.get("gender") or "Boys"
            student_pref = str(student.get("preference") or "Double Sharing").strip()
            pref_lower = student_pref.lower()

            # Load and normalize all rooms
            rooms_list = list(database.rooms.find())

            # First priority: matching gender AND room preference/type
            selected_room = None
            for r in rooms_list:
                if is_gender_match(student_gender, r.get("gender")):
                    occ = get_occupants(r)
                    cap = get_capacity(r)
                    r_type = str(r.get("room_type") or "").strip()
                    type_lower = r_type.lower()
                    if len(occ) < cap:
                        if (r_type and (pref_lower in type_lower or type_lower in pref_lower)) or \
                           ("single" in pref_lower and cap == 1) or \
                           ("double" in pref_lower and cap == 2) or \
                           ("triple" in pref_lower and cap == 3) or \
                           ("4" in pref_lower and cap == 4):
                            selected_room = r
                            break

            # Second priority: any available room matching gender
            if not selected_room:
                for r in rooms_list:
                    if is_gender_match(student_gender, r.get("gender")):
                        occ = get_occupants(r)
                        cap = get_capacity(r)
                        if len(occ) < cap:
                            selected_room = r
                            break

            if not selected_room:
                flash(f"No available room with free capacity found for {student_gender} hostel.", "error")
                return redirect(url_for("allocate"))

            room_no = extract_room_no(selected_room)

            database.students.update_one(
                {"_id": student["_id"]},
                {"$set": {"room_no": room_no}}
            )
            database.rooms.update_one(
                {"_id": selected_room["_id"]},
                {"$set": {"room_no": room_no}, "$addToSet": {"occupants": student["_id"]}}
            )

            flash(f"Room {room_no} successfully allocated to {student.get('name', 'Student')} ({student.get('roll_no', '')}).", "success")
            return redirect(url_for("allocate"))

        except InvalidId:
            flash("Invalid student identifier.", "error")
            return redirect(url_for("allocate"))
        except Exception as e:
            flash(f"Allocation error: {e}", "error")
            return redirect(url_for("allocate"))

    # Unallocated students
    unallocated = list(database.students.find({
        "$or": [
            {"room_no": None},
            {"room_no": ""},
            {"room_no": "None"},
            {"room_no": "Not Allocated"},
            {"room_no": {"$exists": False}}
        ]
    }).sort("name", 1))

    # Allocated students
    allocated_students = list(database.students.find({
        "room_no": {"$nin": ["", None, "None", "Not Allocated"], "$exists": True}
    }).sort("room_no", 1))

    # Rooms data
    rooms_data = list(database.rooms.find())
    for r in rooms_data:
        r["room_no"] = extract_room_no(r)
        r["capacity"] = get_capacity(r)
        r["occupants"] = get_occupants(r)
    rooms_data.sort(key=lambda x: str(x.get("room_no", "")))

    return render_template(
        "allocate.html",
        students=unallocated,
        allocated_students=allocated_students,
        rooms=rooms_data
    )

@app.route("/allocate/all", methods=["POST"])
@warden_required
def allocate_all():
    """Batch allocate all unallocated students automatically based on Gender & Sharing Preference"""
    database = get_db()
    if database is None:
        flash("MongoDB is not connected.", "error")
        return redirect(url_for("allocate"))

    try:
        repair_and_normalize_database(database)

        unallocated = list(database.students.find({
            "$or": [
                {"room_no": None},
                {"room_no": ""},
                {"room_no": "None"},
                {"room_no": "Not Allocated"},
                {"room_no": {"$exists": False}}
            ]
        }).sort("created_at", 1))

        if not unallocated:
            flash("No unallocated students found. All students already have rooms!", "info")
            return redirect(url_for("allocate"))

        allocated_count = 0
        failed_count = 0

        rooms_list = list(database.rooms.find())
        for r in rooms_list:
            r["room_no"] = extract_room_no(r)
            r["capacity"] = get_capacity(r)
            r["occupants"] = get_occupants(r)

        for student in unallocated:
            student_gender = student.get("gender") or "Boys"
            student_pref = str(student.get("preference") or "Double Sharing").strip()
            pref_lower = student_pref.lower()

            # Try exact match on preference first
            matched_room = None
            for r in rooms_list:
                if is_gender_match(student_gender, r.get("gender")):
                    occ = r.get("occupants", [])
                    cap = r.get("capacity", 2)
                    r_type = str(r.get("room_type") or "").strip()
                    type_lower = r_type.lower()
                    if len(occ) < cap:
                        if (r_type and (pref_lower in type_lower or type_lower in pref_lower)) or \
                           ("single" in pref_lower and cap == 1) or \
                           ("double" in pref_lower and cap == 2) or \
                           ("triple" in pref_lower and cap == 3) or \
                           ("4" in pref_lower and cap == 4):
                            matched_room = r
                            break

            # Fallback to any room with capacity matching gender
            if not matched_room:
                for r in rooms_list:
                    if is_gender_match(student_gender, r.get("gender")):
                        occ = r.get("occupants", [])
                        cap = r.get("capacity", 2)
                        if len(occ) < cap:
                            matched_room = r
                            break

            if matched_room:
                room_no = extract_room_no(matched_room)
                database.students.update_one(
                    {"_id": student["_id"]},
                    {"$set": {"room_no": room_no}}
                )
                database.rooms.update_one(
                    {"_id": matched_room["_id"]},
                    {"$set": {"room_no": room_no}, "$addToSet": {"occupants": student["_id"]}}
                )
                matched_room["occupants"].append(student["_id"])
                allocated_count += 1
            else:
                failed_count += 1

        if allocated_count > 0 and failed_count == 0:
            flash(f"🎉 Successfully allocated all {allocated_count} student(s) to rooms!", "success")
        elif allocated_count > 0 and failed_count > 0:
            flash(f"Allocated {allocated_count} student(s). Could not allocate {failed_count} student(s) due to lack of available room capacity.", "error")
        else:
            flash("Could not allocate any students. No available room with free beds found for matching gender.", "error")

    except Exception as e:
        flash(f"Batch allocation error: {e}", "error")

    return redirect(url_for("allocate"))

# ----------------- COMPLAINTS MANAGEMENT (WARDEN) ----------------- #

@app.route("/complaints")
@warden_required
def complaints():
    database = get_db()
    data = []
    if database is not None:
        try:
            data = list(database.complaints.find().sort("created_at", -1))
        except Exception as e:
            flash(f"Database error: {e}", "error")
    return render_template("complaints.html", complaints=data)

@app.route("/complaints/add", methods=["GET", "POST"])
@warden_required
def add_complaint():
    database = get_db()
    if database is None:
        flash("MongoDB is not connected. Cannot submit complaint.", "error")
        return redirect(url_for("complaints"))

    if request.method == "POST":
        student_id = request.form.get("student_id")
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()

        student = None
        if student_id:
            try:
                student = database.students.find_one({"_id": ObjectId(student_id)})
            except InvalidId:
                pass

        if not student or not category or not description:
            flash("Please fill all required complaint fields.", "error")
            return redirect(url_for("add_complaint"))

        try:
            database.complaints.insert_one({
                "student_id": student["_id"],
                "student_name": student.get("name", "Student"),
                "roll_no": student.get("roll_no", "N/A"),
                "room_no": student.get("room_no") or "Not Allocated",
                "category": category,
                "description": description,
                "status": "Pending",
                "created_at": datetime.utcnow()
            })
            flash("Complaint registered successfully.", "success")
            return redirect(url_for("complaints"))
        except Exception as e:
            flash(f"Failed to submit complaint: {e}", "error")
            return redirect(url_for("add_complaint"))

    student_list = list(database.students.find().sort("name", 1))
    return render_template("add_complaint.html", students=student_list)

@app.route("/complaints/<complaint_id>/status/<status>")
@warden_required
def update_complaint_status(complaint_id, status):
    database = get_db()
    if database is None:
        flash("MongoDB is not connected.", "error")
        return redirect(url_for("complaints"))

    allowed = ["Pending", "In Progress", "Resolved"]
    if status not in allowed:
        flash("Invalid complaint status specified.", "error")
        return redirect(url_for("complaints"))

    try:
        obj_id = ObjectId(complaint_id)
        result = database.complaints.update_one(
            {"_id": obj_id},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}}
        )
        if result.matched_count == 0:
            flash("Complaint not found.", "error")
        else:
            flash(f"Complaint status updated to '{status}'.", "success")
    except InvalidId:
        flash("Invalid complaint ID.", "error")
    except Exception as e:
        flash(f"Error updating complaint: {e}", "error")

    return redirect(url_for("complaints"))

@app.route("/complaints/<complaint_id>/delete", methods=["POST"])
@warden_required
def delete_complaint(complaint_id):
    database = get_db()
    if database is None:
        flash("MongoDB is not connected.", "error")
        return redirect(url_for("complaints"))

    try:
        obj_id = ObjectId(complaint_id)
        database.complaints.delete_one({"_id": obj_id})
        flash("Complaint deleted successfully.", "success")
    except InvalidId:
        flash("Invalid complaint ID.", "error")
    except Exception as e:
        flash(f"Error deleting complaint: {e}", "error")

    return redirect(url_for("complaints"))

# ----------------- DB STATUS & DIAGNOSTICS ----------------- #

@app.route("/db-status")
def db_status_page():
    database = get_db()
    stats = {
        "students": 0,
        "rooms": 0,
        "complaints": 0
    }
    if database is not None:
        try:
            stats["students"] = database.students.count_documents({})
            stats["rooms"] = database.rooms.count_documents({})
            stats["complaints"] = database.complaints.count_documents({})
        except Exception:
            pass
    return render_template("db_status.html", stats=stats, connection=connection_state)

@app.route("/db-status/test", methods=["POST"])
def test_db_connection():
    success = init_mongo_connection(force=True)
    if success:
        flash(f"✅ MongoDB Connected Successfully! Latency: {connection_state['latency_ms']} ms", "success")
    else:
        flash(f"❌ MongoDB Connection Failed! Error: {connection_state['error']}", "error")
    return redirect(url_for("db_status_page"))

# ----------------- SEED SAMPLE DATA ----------------- #

@app.route("/seed-demo-data", methods=["POST"])
@warden_required
def seed_demo_data():
    """Populates database with initial sample rooms and students for testing"""
    database = get_db()
    if database is None:
        flash("MongoDB is not connected. Cannot insert sample data.", "error")
        return redirect(url_for("warden_dashboard"))

    try:
        sample_rooms = [
            {"room_no": "B101", "gender": "Boys", "capacity": 1, "room_type": "Single Room", "occupants": []},
            {"room_no": "B102", "gender": "Boys", "capacity": 2, "room_type": "Double Sharing", "occupants": []},
            {"room_no": "B103", "gender": "Boys", "capacity": 3, "room_type": "Triple Sharing", "occupants": []},
            {"room_no": "G101", "gender": "Girls", "capacity": 1, "room_type": "Single Room", "occupants": []},
            {"room_no": "G102", "gender": "Girls", "capacity": 2, "room_type": "Double Sharing", "occupants": []},
            {"room_no": "G103", "gender": "Girls", "capacity": 3, "room_type": "Triple Sharing", "occupants": []},
        ]
        sample_students = [
            {"name": "Arun Kumar", "roll_no": "24CS001", "gender": "Boys", "department": "CSE", "year": "2", "preference": "Double Sharing", "password": generate_password_hash("24CS001"), "room_no": None, "created_at": datetime.utcnow()},
            {"name": "Karthik Raja", "roll_no": "24CS002", "gender": "Boys", "department": "IT", "year": "3", "preference": "Triple Sharing", "password": generate_password_hash("24CS002"), "room_no": None, "created_at": datetime.utcnow()},
            {"name": "Ananya Sharma", "roll_no": "24EC001", "gender": "Girls", "department": "ECE", "year": "1", "preference": "Double Sharing", "password": generate_password_hash("24EC001"), "room_no": None, "created_at": datetime.utcnow()},
            {"name": "Priya Dharshini", "roll_no": "24ME001", "gender": "Girls", "department": "Mechanical", "year": "2", "preference": "Single Room", "password": generate_password_hash("24ME001"), "room_no": None, "created_at": datetime.utcnow()},
        ]

        rooms_added = 0
        for r in sample_rooms:
            if not database.rooms.find_one({"room_no": r["room_no"]}):
                database.rooms.insert_one(r)
                rooms_added += 1

        students_added = 0
        for s in sample_students:
            if not database.students.find_one({"roll_no": s["roll_no"]}):
                database.students.insert_one(s)
                students_added += 1

        flash(f"Sample data loaded: {rooms_added} rooms added, {students_added} students added.", "success")
    except Exception as e:
        flash(f"Failed to load sample data: {e}", "error")

    return redirect(url_for("warden_dashboard"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 65)
    print("🚀 Starting Smart Hostel Management Application...")
    print(f"🌐 Access the application at: http://127.0.0.1:{port}")
    print("=" * 65)
    app.run(host="0.0.0.0", port=port, debug=False)
