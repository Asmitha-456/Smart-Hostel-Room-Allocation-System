"""
Smart Hostel Room Allocation - MongoDB Connection Tester & Health Diagnostics
Run this file in your terminal: python test_connection.py
"""

import os
import sys
import time
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

# Check root .env and venv/.env
base_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(base_dir, ".env")
venv_env = os.path.join(base_dir, "venv", ".env")

if os.path.exists(env_path):
    load_dotenv(env_path)
    print(f"[*] Loaded configuration from: {env_path}")
elif os.path.exists(venv_env):
    load_dotenv(venv_env)
    print(f"[*] Loaded configuration from: {venv_env}")
else:
    load_dotenv()
    print("[!] Warning: .env file not found, using default environment variables.")

MONGO_URI = os.getenv("MONGO_URI", "").strip().strip('"\'')

DB_NAME = os.getenv("DB_NAME", "smart_hostel").strip()

print("\n" + "=" * 65)
print("     MONGODB CONNECTION TEST / SMART HOSTEL")
print("=" * 65)

# Mask password in URI for clean printing
masked_uri = MONGO_URI
if "@" in MONGO_URI and "://" in MONGO_URI:
    prefix = MONGO_URI.split("://")[0]
    creds_host = MONGO_URI.split("://")[1]
    if "@" in creds_host:
        host_part = creds_host.split("@")[1]
        masked_uri = f"{prefix}://*****:*****@{host_part}"

print(f"Target Cluster  : {masked_uri}")
print(f"Database Name   : {DB_NAME}")
print("Connecting...")

start_time = time.time()

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Ping the server to check connectivity
    client.admin.command("ping")
    db = client[DB_NAME]
    latency = round((time.time() - start_time) * 1000, 2)

    students_count = db.students.count_documents({})
    rooms_count = db.rooms.count_documents({})
    complaints_count = db.complaints.count_documents({})

    print("\n" + "=" * 65)
    print("✅ STATUS: SUCCESSFUL! MongoDB Connected Successfully.")
    print("=" * 65)
    print(f"Latency          : {latency} ms")
    print(f"Students Count   : {students_count}")
    print(f"Rooms Count      : {rooms_count}")
    print(f"Complaints Count : {complaints_count}")
    print("=" * 65)

    # Detailed Rooms Diagnostic
    print("\n[Hostel Rooms in Database]:")
    rooms = list(db.rooms.find())
    if rooms:
        for r in rooms:
            r_no = r.get("room_no") or r.get("room_number") or r.get("room") or "MISSING_ROOM_NO"
            gender = r.get("gender", "N/A")
            capacity = r.get("capacity", "N/A")
            occupants = r.get("occupants", [])
            print(f"  • Room {r_no} ({gender}) - Capacity: {capacity}, Occupants: {len(occupants)}")
    else:
        print("  (No rooms currently created in database)")

    # Detailed Students Diagnostic
    print("\n[Students in Database]:")
    students = list(db.students.find())
    if students:
        for s in students:
            name = s.get("name", "Unknown")
            roll = s.get("roll_no", "N/A")
            gender = s.get("gender", "N/A")
            room = s.get("room_no") or "Not Allocated"
            print(f"  • {name} ({roll}, {gender}) -> Room: {room}")
    else:
        print("  (No students currently enrolled in database)")

    print("\n" + "=" * 65)
    print("Your MongoDB Atlas database is completely healthy and ready!")
    print("To run the Flask application, execute: python app.py\n")

except (ConnectionFailure, ServerSelectionTimeoutError) as e:
    print("\n" + "=" * 65)
    print("❌ STATUS: FAILED! MongoDB Connection Failed / Not Connected.")
    print("=" * 65)
    print(f"Error Message: {e}\n")
    print("HOW TO FIX:")
    print("1. MongoDB Atlas Network Access:")
    print("   - Open https://cloud.mongodb.com")
    print("   - Go to 'Network Access' -> Click 'Add IP Address'")
    print("   - Select 'Allow Access From Anywhere' (0.0.0.0/0) and confirm.")
    print("2. Check credentials in .env file (username & password).")
    print("3. Ensure your internet connection is active.")
    print("=" * 65 + "\n")
    sys.exit(1)

except Exception as e:
    print("\n" + "=" * 65)
    print(f"❌ STATUS: FAILED! Unexpected Error: {e}")
    print("=" * 65 + "\n")
    sys.exit(1)
