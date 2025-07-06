import os
import psycopg2
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
import random
import traceback

app = Flask(__name__)

# CORS configuration for GitHub Pages frontend
CORS(app, resources={
    r"/api/*": {
        "origins": ["https://megamind1212.github.io/transcript_frontend", "http://localhost:8000"],  # Replace with your GitHub Pages URL
        "methods": ["GET", "POST", "OPTIONS", "PUT", "DELETE"],
        "allow_headers": ["Content-Type", "Authorization"],
        "supports_credentials": True
    }
})

# CockroachDB configuration
def get_db_connection():
    cert_path = "/tmp/cockroachdb_ca.crt"
    with open(cert_path, "w") as f:
        f.write(base64.b64decode(os.getenv("SSL_CA")).decode("utf-8"))
    
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=os.getenv("DB_PORT"),
        sslmode="verify-full",
        sslrootcert=cert_path
    )

# Environment variables for Gmail SMTP and Deepgram
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# Create OTPs table if not exists
def init_db():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS otps (
                key STRING PRIMARY KEY,
                otp STRING,
                created_at TIMESTAMP DEFAULT now()
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"Error initializing database: {str(e)}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Initialize database on app start
init_db()

# OPTIONS handler for preflight requests
@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return jsonify({}), 200

# Get Deepgram WebSocket URL
@app.route("/api/get-deepgram-ws", methods=["GET"])
def get_deepgram_ws():
    if not DEEPGRAM_API_KEY:
        return jsonify({"error": "Deepgram API key not configured"}), 500
    ws_url = f"wss://api.deepgram.com/v1/listen?model=nova-3-medical&diarize=true&smart_format=true&punctuate=true&token={DEEPGRAM_API_KEY}"
    return jsonify({"wsUrl": ws_url}), 200

# Request OTP
@app.route("/api/request-otp", methods=["POST"])
def request_otp():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))
        empid = int(data.get("empId"))

        if not orgid or not empid:
            return jsonify({"error": "orgid and empid are required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT empemail FROM employees WHERE orgid = %s AND empid = %s",
            (orgid, empid)
        )
        employee = cursor.fetchone()

        if not employee:
            return jsonify({"error": "No employee found with this orgid and empid"}), 404

        empemail = employee[0]
        if not empemail:
            return jsonify({"error": "Employee email not found"}), 400

        otp = str(random.randint(1000, 9999))
        otp_key = f"{orgid}-{empid}"

        # Store OTP in CockroachDB
        cursor.execute(
            "INSERT INTO otps (key, otp, created_at) VALUES (%s, %s, %s) ON CONFLICT (key) DO UPDATE SET otp = %s, created_at = %s",
            (otp_key, otp, datetime.now(), otp, datetime.now())
        )
        conn.commit()

        print(f"OTP for {empemail}: {otp}")

        if GMAIL_EMAIL and GMAIL_APP_PASSWORD:
            success = send_otp_email(empemail, otp)
            if not success:
                return jsonify({
                    "message": "Failed to send OTP via email. Check the server console for the OTP."
                }), 200
        else:
            return jsonify({
                "message": "Email service not configured. Check the server console for the OTP."
            }), 200

        return jsonify({"message": "OTP sent to your registered email address"}), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Send OTP via email
def send_otp_email(to_email, otp):
    try:
        msg = MIMEText(f"Your Notesmate OTP is: {otp}")
        msg["Subject"] = "Notesmate OTP Verification"
        msg["From"] = GMAIL_EMAIL
        msg["To"] = to_email

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_EMAIL, to_email, msg.as_string())
            return True
    except Exception as e:
        print(f"Failed to send email to {to_email}: {str(e)}")
        return False

# Validate OTP
@app.route("/api/validate-otp", methods=["POST"])
def validate_otp():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))
        empid = int(data.get("empId"))
        entered_otp = data.get("otp")

        if not orgid or not empid or not entered_otp:
            return jsonify({"error": "orgid, empid, and OTP are required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        otp_key = f"{orgid}-{empid}"
        cursor.execute(
            "SELECT otp, created_at FROM otps WHERE key = %s",
            (otp_key,)
        )
        result = cursor.fetchone()

        if not result:
            return jsonify({"error": "OTP not found or expired"}), 400

        stored_otp, created_at = result
        # Check if OTP is expired (e.g., 5 minutes)
        if datetime.now() - created_at > timedelta(minutes=5):
            cursor.execute("DELETE FROM otps WHERE key = %s", (otp_key,))
            conn.commit()
            return jsonify({"error": "OTP expired"}), 400

        if stored_otp != entered_otp:
            return jsonify({"error": "Invalid OTP"}), 400

        # Delete OTP after validation
        cursor.execute("DELETE FROM otps WHERE key = %s", (otp_key,))
        conn.commit()

        return jsonify({
            "message": "OTP validated successfully",
            "orgId": orgid,
            "empId": empid
        }), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Register new user
@app.route("/api/register", methods=["POST"])
def register():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))
        orgname = data.get("orgName")
        shortname = data.get("shortname")
        address = data.get("address")
        phone = data.get("orgPhone")
        email = data.get("orgEmail")
        empid = int(data.get("empId"))
        empname = data.get("empName")
        empshortname = data.get("empShortname")
        empphone = data.get("empPhone")
        empemail = data.get("empEmail")

        if not all([orgid, orgname, shortname, address, phone, email, empid, empname, empshortname, empphone, empemail]):
            return jsonify({"error": "All fields are required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM organizations WHERE orgid = %s", (orgid,))
        org_exists = cursor.fetchone()

        if not org_exists:
            cursor.execute(
                """INSERT INTO organizations 
                (orgid, orgname, shortname, address, phone, email) 
                VALUES (%s, %s, %s, %s, %s, %s)""",
                (orgid, orgname, shortname, address, phone, email)
            )

        cursor.execute("SELECT * FROM employees WHERE orgid = %s AND empid = %s", 
                      (orgid, empid))
        if cursor.fetchone():
            return jsonify({"error": "Employee with this empid already exists in this organization"}), 400

        cursor.execute(
            """INSERT INTO employees 
            (empid, orgid, empname, empshortname, empphone, empemail) 
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (empid, orgid, empname, empshortname, empphone, empemail)
        )

        conn.commit()
        return jsonify({"message": "Registration successful"}), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Register new client
@app.route("/api/register-client", methods=["POST"])
def register_client():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))
        clientname = data.get("clientName")
        clientshortname = data.get("clientShortname")
        clientphone = data.get("clientPhone")
        clientemail = data.get("clientEmail")

        if not all([orgid, clientname, clientshortname, clientphone, clientemail]):
            return jsonify({"error": "All fields are required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM organizations WHERE orgid = %s", (orgid,))
        org_exists = cursor.fetchone()
        if not org_exists:
            return jsonify({"error": "Organization not found"}), 404

        cursor.execute("SELECT MAX(clientid) FROM clients WHERE orgid = %s", (orgid,))
        result = cursor.fetchone()
        new_clientid = (result[0] or 0) + 1

        cursor.execute("SELECT * FROM clients WHERE orgid = %s AND clientid = %s", (orgid, new_clientid))
        if cursor.fetchone():
            return jsonify({"error": "Client with this clientid already exists in this organization"}), 400

        cursor.execute(
            """INSERT INTO clients 
            (clientid, orgid, clientname, clientshortname, clientphone, clientemail) 
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (new_clientid, orgid, clientname, clientshortname, clientphone, clientemail)
        )

        conn.commit()
        return jsonify({
            "message": "Client registered successfully",
            "clientId": new_clientid
        }), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Fetch clients
@app.route("/api/fetch-clients", methods=["POST"])
def fetch_clients():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))

        if not orgid:
            return jsonify({"error": "orgid is required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT clientid, clientname, clientshortname FROM clients WHERE orgid = %s",
            (orgid,)
        )
        clients = cursor.fetchall()

        client_list = [{
            "ClientID": row[0],
            "ClientName": row[1],
            "ClientShortname": row[2]
        } for row in clients]

        return jsonify({"clients": client_list}), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Save transcription
@app.route("/api/save-transcription", methods=["POST"])
def save_transcription():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))
        empid = int(data.get("empId"))
        clientid = int(data.get("clientId"))
        transcriptiontext = data.get("transcriptionText")
        audionotes = data.get("audioData")

        if not all([orgid, empid, clientid, transcriptiontext]):
            return jsonify({"error": "orgid, empid, clientid, and transcriptiontext are required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM clients WHERE orgid = %s AND clientid = %s",
            (orgid, clientid)
        )
        if not cursor.fetchone():
            return jsonify({"error": "Invalid clientid for this organization"}), 404

        audio_binary = base64.b64decode(audionotes) if audionotes else None

        cursor.execute(
            """INSERT INTO notes 
            (orgid, empid, clientid, meetingid, datetime, audionotes, textnotes) 
            VALUES (%s, %s, %s, nextval('notes_seq'), %s, %s, %s)""",
            (orgid, empid, clientid, datetime.now(), psycopg2.Binary(audio_binary) if audio_binary else None, transcriptiontext)
        )

        conn.commit()
        return jsonify({"message": "Transcription saved successfully"}), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Fetch notes
@app.route("/api/fetch-notes", methods=["POST"])
def fetch_notes():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))
        empid = int(data.get("empId"))
        clientid = int(data.get("clientId"))
        selecteddate = data.get("selectedDate")

        if not all([orgid, empid, clientid]):
            return jsonify({"error": "orgid, empid, and clientid are required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        query = """
            SELECT datetime, textnotes, audionotes
            FROM notes
            WHERE orgid = %s AND empid = %s AND clientid = %s
        """
        params = [orgid, empid, clientid]

        if selecteddate:
            query += " AND DATE(datetime) = %s"
            params.append(selecteddate)

        query += " ORDER BY datetime DESC"
        cursor.execute(query, params)
        notes = cursor.fetchall()

        note_list = [{
            "DateTime": row[0].isoformat(),
            "TextNotes": row[1],
            "AudioNotes": base64.b64encode(row[2]).decode("utf-8") if row[2] else None
        } for row in notes]

        return jsonify({"notes": note_list}), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Update note transcription
@app.route("/api/update-note", methods=["POST"])
def update_note():
    conn = None
    cursor = None
    try:
        data = request.get_json()
        orgid = int(data.get("orgId"))
        empid = int(data.get("empId"))
        clientid = int(data.get("clientId"))
        dateTime = data.get("dateTime")
        newText = data.get("newText")

        if not all([orgid, empid, clientid, dateTime, newText]):
            return jsonify({"error": "orgid, empid, clientid, dateTime, and newText are required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """UPDATE notes 
            SET textnotes = %s 
            WHERE orgid = %s AND empid = %s AND clientid = %s AND datetime = %s""",
            (newText, orgid, empid, clientid, dateTime)
        )
        conn.commit()

        return jsonify({"message": "Transcription updated successfully"}), 200

    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Default route
@app.route("/")
def index():
    return jsonify({"message": "NotesMate API is running"})

# Vercel serverless function compatibility
def handler(event, context):
    from serverless_wsgi import handle_request
    return handle_request(app, event, context)