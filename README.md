# Smart Hostel Room Allocation and Complaints Management System

A full-stack, role-based hostel management web application built with **Python Flask** and **MongoDB Atlas**, featuring dedicated **Student** and **Warden** portals.

---

## 👥 Role-Based Portals & Key Features

### 1. 👨‍🎓 Student Portal
- **Student Registration**:
  - Full Name, Roll Number / ID, Gender (Boys / Girls), Department, and Year.
  - **Room Sharing Preference**: Choose between **Single Room (1 Bed)**, **Double Sharing (2 Beds)**, **Triple Sharing (3 Beds)**, or **4-Sharing (4 Beds)**.
  - Secure password setup.
- **Student Sign In**:
  - Log in using Roll Number and Password.
- **Personalized Dashboard**:
  - Live **Room Allocation Status**:
    * When allocated: Displays assigned Room Number, Room Type, and names of fellow roommates!
    * When pending: Displays "⏳ Pending Allocation by Warden" with their selected preference.
- **Complaint Submission & Tracking**:
  - Submit complaints directly (Plumbing, Electrical, Water, Cleaning, WiFi, Furniture, etc.).
  - Student details and room number are automatically linked.
  - Live status tracking: **Pending** ⏳, **In Progress** 🔄, and **Resolved** ✅.

---

### 2. 🛡️ Warden (Admin) Portal
- **Warden Authentication**:
  - Default Username: `warden`
  - Default Password: `warden123`
- **Dashboard Overview**:
  - High-level analytics: Total Enrolled Students, Total Rooms, Allocated Beds, and Pending Complaints.
- **Smart Room Allocation**:
  - **Gender Matching Rule**: Boys &rarr; Boys Hostel Block; Girls &rarr; Girls Hostel Block.
  - **Preference-Aware Allocation**: Prioritizes rooms matching the student's chosen Room Sharing Type (Single, Double, Triple).
  - **⚡ Auto-Allocate All Pending**: One-click batch allocation for all waiting students.
  - **Vacate / Deallocate**: Release a student from a room with 1 click.
- **Room Management**:
  - Create rooms with block (Boys/Girls), Room Sharing Type, and bed capacities.
  - Real-time tracking of occupied vs available beds, and list of student occupants.
  - Safe deletion of empty rooms.
- **Complaints Resolution**:
  - Review all student complaints across all rooms.
  - Update complaint status to **Pending**, **In Progress**, or **Resolved**.
  - Delete completed complaint records.

---

### 3. 🔌 MongoDB Atlas Connection Health & Diagnostics
- **Terminal Startup Logs**: Clear `[SUCCESS] MongoDB Connected Successfully!` or `[FAILED]` in the console.
- **Live Navbar Indicator**: `🟢 MongoDB: Connected (smart_hostel)` or `🔴 MongoDB: Not Connected`.
- **Database Status Page (`/db-status`)**: Ping latency in milliseconds, live collection counts, and on-the-fly **"Test / Reconnect"** button.
- **Standalone Tester**: Run `python test_connection.py` in the terminal anytime.

---

## 🚀 Setup & Execution Guide

### 1. Open Terminal in Project Folder
```bash
cd Smart_Hostel_Room_Allocation_Flask_MongoDB
```

### 2. Activate Virtual Environment
On Windows Command Prompt:
```cmd
venv\Scripts\activate
```
On Windows PowerShell:
```powershell
.\venv\Scripts\Activate.ps1
```

### 3. Verify .env Configuration
Ensure the `.env` file exists in the root folder with:
```env
MONGO_URI="mongodb+srv://24104015_db_user:Asmitha_456@cluster0.tywoyrw.mongodb.net/?appName=Cluster0"
DB_NAME=smart_hostel
SECRET_KEY=smart-hostel-secret-key-2026
```

> **Note on MongoDB Atlas Network Access:**
> If you experience connection timeout errors, log into [MongoDB Atlas](https://cloud.mongodb.com/) &rarr; **Network Access** &rarr; **Add IP Address** &rarr; Select **Allow Access from Anywhere (`0.0.0.0/0`)** and confirm.

### 4. Run the Application
```bash
python app.py
```

Open your browser at:
👉 **[http://127.0.0.1:5000](http://127.0.0.1:5000)**

---

## 🔑 Login Credentials

| Role | Username / Identifier | Password | Access Rights |
| :--- | :--- | :--- | :--- |
| **🛡️ Warden** | `warden` | `warden123` | Full admin control, room creation, smart allocation, complaints management |
| **👨‍🎓 Student** | Your Roll Number (e.g. `24104015`) | Password created during registration | View room allocation status, view roommates, submit & track complaints |

*(Tip: Existing students already seeded in the database can log in using their Roll Number as both username and password, or by registering a new student account.)*
