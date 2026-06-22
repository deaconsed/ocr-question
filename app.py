import os
import json
import base64
import shutil
import subprocess
from datetime import datetime, date
from decimal import Decimal
from flask import Flask, render_template, jsonify, request, send_from_directory, Response, session, redirect, url_for
import threading
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
from pipeline import pipeline
from db import get_db_connection

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-12345")
INPUT_DIR = "extracted_frames"


def _ensure_unidentified_table():
    """Idempotent: create unidentified_frames if missing, so users on existing
    databases don't have to re-run init_db.py."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS unidentified_frames (
                id INT AUTO_INCREMENT PRIMARY KEY,
                exam_id INT NOT NULL,
                frame_number INT NOT NULL,
                image_path VARCHAR(500) NOT NULL,
                video_timestamp VARCHAR(50),
                ocr_text TEXT,
                ocr_subject VARCHAR(255) NULL,
                ocr_question_number INT NULL,
                status ENUM('pending','resolved','discarded') NOT NULL DEFAULT 'pending',
                resolved_by INT NULL,
                resolved_at TIMESTAMP NULL,
                resolved_subject VARCHAR(255) NULL,
                resolved_question_number INT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (exam_id) REFERENCES exams(id),
                FOREIGN KEY (resolved_by) REFERENCES users(id),
                INDEX idx_exam_status (exam_id, status)
            )
        """)
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"[startup] Could not ensure unidentified_frames table: {e}")
    finally:
        conn.close()


def _ensure_assignment_tables():
    """Idempotent: create the verifier assignment tables if missing, so users on
    existing databases get the feature without re-running init_db.py."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS verifier_subjects")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verifier_assignments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                exam_id INT NOT NULL,
                subject_id INT NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_user_exam_subject (user_id, exam_id, subject_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE,
                FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verifier_exams (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                exam_id INT NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_user_exam (user_id, exam_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
            )
        """)
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"[startup] Could not ensure verifier assignment tables: {e}")
    finally:
        conn.close()


_ensure_unidentified_table()
_ensure_assignment_tables()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return "Unauthorized. Admin access required.", 403
        return f(*args, **kwargs)
    return decorated_function

# --- Auth Routes ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        conn = get_db_connection()
        if not conn:
            return "Database connection failed", 500
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('index'))
        else:
            return render_template("login.html", error="Invalid username or password")
            
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route("/")
@login_required
def index():
    return render_template("index.html", user=session)

@app.route("/api/my_assignments", methods=["GET"])
@login_required
def my_assignments():
    """Assignments for the logged-in user: session-scoped subjects (each a subject
    within one specific exam session) and whole sessions. Used to render the
    quick-access panel on the verifier dashboard."""
    user_id = session.get("user_id")
    conn = get_db_connection()
    if not conn:
        return jsonify({"assignments": [], "exams": []})
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT va.exam_id, va.subject_id, s.name AS subject_name,
                   e.year AS exam_year, e.label AS exam_label, e.session_index
            FROM verifier_assignments va
            JOIN subjects s ON va.subject_id = s.id
            JOIN exams e ON va.exam_id = e.id
            WHERE va.user_id = %s
            ORDER BY e.year DESC, e.session_index ASC, s.name ASC
        """, (user_id,))
        assignments = cursor.fetchall()

        cursor.execute("""
            SELECT e.id, e.year, e.label, e.session_index
            FROM verifier_exams ve
            JOIN exams e ON ve.exam_id = e.id
            WHERE ve.user_id = %s
            ORDER BY e.year DESC, e.session_index ASC
        """, (user_id,))
        exams = cursor.fetchall()
        return jsonify({"assignments": assignments, "exams": exams})
    finally:
        cursor.close()
        conn.close()

# --- Exam Routes ---
@app.route("/api/exams", methods=["GET"])
@login_required
def get_exams():
    conn = get_db_connection()
    if not conn:
        return jsonify([])
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT e.*, 
               COUNT(q.id) as total_questions,
               COUNT(CASE WHEN q.verified = TRUE THEN 1 END) as verified_count
        FROM exams e
        LEFT JOIN questions q ON e.id = q.exam_id
        GROUP BY e.id
        ORDER BY e.year DESC, e.created_at DESC
    """)
    exams = cursor.fetchall()
    
    # Get subjects per exam
    for exam in exams:
        cursor.execute("""
            SELECT DISTINCT s.name 
            FROM questions q 
            JOIN subjects s ON q.subject_id = s.id 
            WHERE q.exam_id = %s
        """, (exam['id'],))
        exam['subjects'] = [row['name'] for row in cursor.fetchall()]
        # Convert datetime for JSON serialization
        if exam.get('created_at'):
            exam['created_at'] = exam['created_at'].isoformat()
    
    cursor.close()
    conn.close()
    return jsonify(exams)

@app.route("/api/subjects", methods=["GET"])
@login_required
def get_subjects():
    exam_id = request.args.get("exam_id")
    
    conn = get_db_connection()
    if not conn:
        return jsonify([])
    cursor = conn.cursor(dictionary=True)
    
    if exam_id:
        cursor.execute("""
            SELECT DISTINCT s.name 
            FROM subjects s 
            JOIN questions q ON s.id = q.subject_id 
            WHERE q.exam_id = %s
        """, (exam_id,))
    else:
        # Fallback: return all subjects with questions
        cursor.execute("SELECT DISTINCT s.name FROM subjects s JOIN questions q ON s.id = q.subject_id")
    
    subjects = [row['name'] for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
    return jsonify(subjects)

@app.route("/api/questions/<subject>", methods=["GET"])
@login_required
def get_questions(subject):
    exam_id = request.args.get("exam_id")
    if not exam_id:
        return jsonify({"error": "exam_id is required"}), 400
    
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
        
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT q.*, 
               u1.username as verified_by_username,
               u2.username as locked_by_username
        FROM questions q
        JOIN subjects s ON q.subject_id = s.id
        LEFT JOIN users u1 ON q.verified_by = u1.id
        LEFT JOIN users u2 ON q.locked_by = u2.id
        WHERE s.name = %s AND q.exam_id = %s
        ORDER BY q.question_number ASC
    """, (subject, exam_id))
    
    questions = cursor.fetchall()
    
    user_role = session.get('role')
    
    expected_count = 60 if subject == "USE OF ENGLISH" else 40
    existing_map = {q['question_number']: q for q in questions}
    
    full_list = []
    
    for i in range(1, expected_count + 1):
        if i in existing_map:
            q = existing_map[i]
            
            # Visibility Restriction for Teachers
            if user_role == 'teacher':
                q['image_name'] = None # Hide raw frame image
            
            if q.get('options'):
                try:
                    q['options'] = json.loads(q['options'])
                except:
                    q['options'] = {}
            else:
                q['options'] = {}
            full_list.append(q)
        else:
            # Find previous known timestamp
            prev_ts = "Start"
            for j in range(i - 1, 0, -1):
                if j in existing_map and existing_map[j].get('video_timestamp'):
                    prev_ts = existing_map[j]['video_timestamp']
                    break
            
            # Find next known timestamp
            next_ts = "End"
            for j in range(i + 1, expected_count + 1):
                if j in existing_map and existing_map[j].get('video_timestamp'):
                    next_ts = existing_map[j]['video_timestamp']
                    break
                    
            full_list.append({
                "question_number": i,
                "is_missing": True,
                "prev_timestamp": prev_ts,
                "next_timestamp": next_ts,
                "options": {}
            })
            
    cursor.close()
    conn.close()
    
    return jsonify({"questions": full_list})

@app.route("/images/<int:exam_id>/<subject>/<path:filename>")
def serve_image_exam(exam_id, subject, filename):
    """Serve images from exam-scoped directories."""
    subject_folder = subject.lower().replace(" ", "_")
    subject_dir = os.path.join(INPUT_DIR, f"exam_{exam_id}", subject_folder)
    return send_from_directory(subject_dir, filename)

@app.route("/images/<subject>/<path:filename>")
def serve_image(subject, filename):
    """Legacy: serve images from non-exam-scoped directories."""
    subject_folder = subject.lower().replace(" ", "_")
    subject_dir = os.path.join(INPUT_DIR, subject_folder)
    if os.path.exists(os.path.join(subject_dir, filename)):
        return send_from_directory(subject_dir, filename)
    # Try all exam folders as fallback
    for d in os.listdir(INPUT_DIR):
        if d.startswith("exam_"):
            path = os.path.join(INPUT_DIR, d, subject_folder, filename)
            if os.path.exists(path):
                return send_from_directory(os.path.join(INPUT_DIR, d, subject_folder), filename)
    return "Image not found", 404

@app.route("/api/crop/<subject>", methods=["POST"])
@login_required
def save_crop(subject):
    data = request.json
    question_number = data.get("question_number")
    image_data_b64 = data.get("image_data")
    exam_id = data.get("exam_id")
    
    if not question_number or not image_data_b64:
        return jsonify({"error": "Missing data"}), 400
        
    if "," in image_data_b64:
        image_data_b64 = image_data_b64.split(",")[1]
        
    image_bytes = base64.b64decode(image_data_b64)
    filename = f"cropped_question_{question_number}.jpg"
    
    subject_folder = subject.lower().replace(" ", "_")
    
    # Use exam-scoped path if exam_id provided
    if exam_id:
        filepath = os.path.join(INPUT_DIR, f"exam_{exam_id}", subject_folder, filename)
    else:
        filepath = os.path.join(INPUT_DIR, subject_folder, filename)
    
    # Save image to disk
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(image_bytes)
        
    # Update Database
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        if exam_id:
            cursor.execute("""
                UPDATE questions q
                JOIN subjects s ON q.subject_id = s.id
                SET q.question_image = %s, q.has_image = TRUE
                WHERE s.name = %s AND q.question_number = %s AND q.exam_id = %s
            """, (filename, subject, question_number, exam_id))
        else:
            cursor.execute("""
                UPDATE questions q
                JOIN subjects s ON q.subject_id = s.id
                SET q.question_image = %s, q.has_image = TRUE
                WHERE s.name = %s AND q.question_number = %s
            """, (filename, subject, question_number))
        conn.commit()
        cursor.close()
        conn.close()
            
    return jsonify({"success": True, "filename": filename})

@app.route("/api/update_text", methods=["POST"])
@login_required
def update_text():
    data = request.json
    subject = data.get("subject")
    question_number = data.get("question_number")
    question_text = data.get("question_text")
    options = data.get("options")
    exam_id = data.get("exam_id")
    
    if not subject or not question_number:
        return jsonify({"error": "Missing required data"}), 400
        
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
        
    options_json = json.dumps(options) if options else "{}"
        
    user_id = session.get('user_id')
    
    cursor = conn.cursor()
    if exam_id:
        cursor.execute("""
            UPDATE questions q
            JOIN subjects s ON q.subject_id = s.id
            SET q.question_text = %s, q.options = %s, 
                q.verified = TRUE, q.verified_by = %s, 
                q.locked_by = NULL, q.locked_at = NULL
            WHERE s.name = %s AND q.question_number = %s AND q.exam_id = %s
        """, (question_text, options_json, user_id, subject, question_number, exam_id))
    else:
        cursor.execute("""
            UPDATE questions q
            JOIN subjects s ON q.subject_id = s.id
            SET q.question_text = %s, q.options = %s, 
                q.verified = TRUE, q.verified_by = %s, 
                q.locked_by = NULL, q.locked_at = NULL
            WHERE s.name = %s AND q.question_number = %s
        """, (question_text, options_json, user_id, subject, question_number))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({"success": True})

@app.route("/api/verify_question", methods=["POST"])
@login_required
def verify_question():
    data = request.json
    subject = data.get("subject")
    question_number = data.get("question_number")
    exam_id = data.get("exam_id")
    
    if session.get('role') != 'verifier' and session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
        
    user_id = session.get('user_id')
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE questions q
        JOIN subjects s ON q.subject_id = s.id
        SET q.verified = TRUE, q.verified_by = %s,
            q.locked_by = NULL, q.locked_at = NULL
        WHERE s.name = %s AND q.question_number = %s AND q.exam_id = %s
    """, (user_id, subject, question_number, exam_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/upload_missing_question", methods=["POST"])
@login_required
def upload_missing_question():
    data = request.json
    subject = data.get("subject")
    question_number = data.get("question_number")
    image_data_b64 = data.get("image_data")
    exam_id = data.get("exam_id")
    
    if not subject or not question_number or not image_data_b64 or not exam_id:
        return jsonify({"error": "Missing data (subject, question_number, image_data, exam_id required)"}), 400
        
    if "," in image_data_b64:
        image_data_b64 = image_data_b64.split(",")[1]
        
    image_bytes = base64.b64decode(image_data_b64)
    filename = f"question_{question_number}_manual.jpg"
    
    subject_folder = subject.lower().replace(" ", "_")
    filepath = os.path.join(INPUT_DIR, f"exam_{exam_id}", subject_folder, filename)
    
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(image_bytes)
        
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
        
    cursor = conn.cursor(dictionary=True)
    # Get subject id
    cursor.execute("SELECT id FROM subjects WHERE name = %s", (subject,))
    subj_row = cursor.fetchone()
    if not subj_row:
        return jsonify({"error": "Subject not found"}), 404
        
    subject_id = subj_row['id']
    
    # Insert new question
    cursor.execute("""
        INSERT INTO questions (exam_id, subject_id, question_number, image_name, has_image, question_image)
        VALUES (%s, %s, %s, %s, TRUE, %s)
    """, (exam_id, subject_id, question_number, filename, filename))
    
    new_q_id = cursor.lastrowid
    conn.commit()
    cursor.close()
    conn.close()
    
    # Spawn background thread to run GPT extractor
    from gpt_extractor import process_single_question
    def run_extractor():
        try:
            print(f"Running manual extraction for Q_ID {new_q_id}")
            process_single_question(new_q_id, print)
        except Exception as e:
            print(f"Error in manual extraction: {e}")
            
    threading.Thread(target=run_extractor, daemon=True).start()
    
    return jsonify({"success": True, "message": "Uploaded and processing started"})

@app.route("/api/submit_solution", methods=["POST"])
@login_required
def submit_solution():
    data = request.json
    subject = data.get("subject")
    question_number = data.get("question_number")
    teacher_answer = data.get("teacher_answer")
    solution_image_b64 = data.get("solution_image")
    exam_id = data.get("exam_id")
    
    if session.get('role') != 'teacher' and session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    if not subject or not question_number or not teacher_answer:
        return jsonify({"error": "Missing required fields"}), 400
        
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
        
    user_id = session.get('user_id')
    solution_filename = None

    if solution_image_b64:
        if "," in solution_image_b64:
            solution_image_b64 = solution_image_b64.split(",")[1]
        
        image_bytes = base64.b64decode(solution_image_b64)
        solution_filename = f"solution_{question_number}_{user_id}.jpg"
        
        subject_folder = subject.lower().replace(" ", "_")
        filepath = os.path.join(INPUT_DIR, f"exam_{exam_id}", subject_folder, solution_filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(image_bytes)

    cursor = conn.cursor()
    cursor.execute("""
        UPDATE questions q
        JOIN subjects s ON q.subject_id = s.id
        SET q.teacher_answer = %s, q.solution_image = %s, 
            q.completed = TRUE, q.completed_by = %s,
            q.locked_by = NULL, q.locked_at = NULL
        WHERE s.name = %s AND q.question_number = %s AND q.exam_id = %s
    """, (teacher_answer, solution_filename, user_id, subject, question_number, exam_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return jsonify({"success": True})

@app.route("/api/fix_with_ai", methods=["POST"])
@login_required
def fix_with_ai():
    data = request.json
    question_text = data.get("question_text", "")
    options = data.get("options", {})
    
    if not question_text and not options:
        return jsonify({"error": "No text to fix"}), 400
        
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"error": "OpenAI API key missing"}), 500
        
    from gpt_extractor import fix_text_with_gpt
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    
    fixed_data = fix_text_with_gpt(client, question_text, options)
    if fixed_data:
        return jsonify({"success": True, "fixed_data": fixed_data})
    else:
        return jsonify({"error": "AI failed to process the text"}), 500

# Question locking was removed: every question is always accessible to any
# verifier/admin. The /api/lock_question and /api/unlock_question endpoints and
# the lock checks in the dashboard were deleted so an admin browsing a question
# no longer blocks verifiers from working on it.

# --- Admin Pipeline Routes ---

@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html")

@app.route("/api/videos")
def get_videos():
    videos = []
    # Check root directory
    for f in os.listdir("."):
        if f.endswith(('.mp4', '.mkv', '.avi')):
            videos.append(f)
            
    # Check a videos folder if it exists
    if os.path.exists("videos"):
        for f in os.listdir("videos"):
            if f.endswith(('.mp4', '.mkv', '.avi')):
                videos.append(os.path.join("videos", f))
                
    return jsonify({"videos": videos, "is_running": pipeline.is_running, "current_video": pipeline.current_video})

@app.route("/api/process_video", methods=["POST"])
def process_video():
    if pipeline.is_running:
        return jsonify({"error": "A video is already being processed"}), 400
        
    data = request.json
    video_path = data.get("video_path")
    year = data.get("year")
    sessions = data.get("sessions") or None  # list of {start, end} or None for auto

    if not video_path or not os.path.exists(video_path):
        return jsonify({"error": "Video not found"}), 404
    if not year:
        return jsonify({"error": "Year is required"}), 400

    threading.Thread(target=pipeline.run_pipeline, args=(video_path, year, sessions), daemon=True).start()
    return jsonify({"success": True, "message": f"Started processing {video_path}"})

@app.route("/api/browse_file")
@admin_required
def browse_file():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)

    file_path = filedialog.askopenfilename(
        title="Select Video File",
        filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov"), ("All files", "*.*")]
    )
    root.destroy()

    return jsonify({"file_path": file_path})

@app.route("/api/browse_folder")
@admin_required
def browse_folder():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)

    folder_path = filedialog.askdirectory(title="Select Screenshot Folder")
    root.destroy()

    return jsonify({"folder_path": folder_path})

@app.route("/api/import_folder", methods=["POST"])
@admin_required
def import_folder():
    if pipeline.is_running:
        return jsonify({"error": "Pipeline is already running"}), 400

    data = request.json
    folder_path = data.get("folder_path")
    year = data.get("year")

    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Folder not found"}), 404
    if not year:
        return jsonify({"error": "Year is required"}), 400

    threading.Thread(target=pipeline.run_folder_import, args=(folder_path, year), daemon=True).start()
    return jsonify({"success": True, "message": f"Started importing {os.path.basename(folder_path)}"})

@app.route("/api/stop_video", methods=["POST"])
def stop_video():
    if not pipeline.is_running:
        return jsonify({"error": "No video is currently being processed"}), 400
        
    pipeline.stop_pipeline()
    return jsonify({"success": True, "message": "Stop signal sent"})

@app.route("/api/stream_progress")
def stream_progress():
    def event_stream():
        q = pipeline.subscribe_logs()
        try:
            while True:
                msg = q.get()
                yield f"data: {msg}\n\n"
        except GeneratorExit:
            pipeline.unsubscribe_logs(q)
            
    return Response(event_stream(), mimetype="text/event-stream")

# --- User Management Routes ---

@app.route("/api/users", methods=["GET"])
@admin_required
def get_users():
    conn = get_db_connection()
    if not conn:
        return jsonify([])
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role, created_at FROM users")
    users = cursor.fetchall()
    for u in users:
        if u.get('created_at'):
            u['created_at'] = u['created_at'].isoformat()
    cursor.close()
    conn.close()
    return jsonify(users)

@app.route("/api/users", methods=["POST"])
@admin_required
def create_user():
    data = request.json
    username = data.get("username")
    password = data.get("password")
    role = data.get("role", "verifier")
    
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password required"}), 400
        
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
        
    cursor = conn.cursor()
    try:
        pw_hash = generate_password_hash(password)
        cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)", 
                       (username, pw_hash, role))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        cursor.close()
        conn.close()

@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@admin_required
def delete_user(user_id):
    if user_id == session.get('user_id'):
        return jsonify({"success": False, "error": "You cannot delete yourself"}), 400
        
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
        
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"success": True})

# --- Verifier Assignment Routes ---

@app.route("/api/admin/exam_subjects", methods=["GET"])
@admin_required
def admin_exam_subjects():
    """Subjects (id + name) present in a given exam session, for the assignment UI."""
    exam_id = request.args.get("exam_id", type=int)
    if not exam_id:
        return jsonify({"error": "exam_id is required"}), 400
    conn = get_db_connection()
    if not conn:
        return jsonify([])
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT DISTINCT s.id, s.name
        FROM subjects s
        JOIN questions q ON q.subject_id = s.id
        WHERE q.exam_id = %s
        ORDER BY s.name
    """, (exam_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(rows)


@app.route("/api/admin/users/<int:user_id>/assignments", methods=["GET"])
@admin_required
def admin_get_assignments(user_id):
    """Return the session-scoped subject assignments and whole-session assignments
    currently held by a user (with display labels)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT va.exam_id, va.subject_id, s.name AS subject_name,
                   e.year AS exam_year, e.label AS exam_label, e.session_index
            FROM verifier_assignments va
            JOIN subjects s ON va.subject_id = s.id
            JOIN exams e ON va.exam_id = e.id
            WHERE va.user_id = %s
            ORDER BY e.year DESC, e.session_index ASC, s.name ASC
        """, (user_id,))
        assignments = cursor.fetchall()
        cursor.execute("SELECT exam_id FROM verifier_exams WHERE user_id = %s", (user_id,))
        exam_ids = [r["exam_id"] for r in cursor.fetchall()]
        return jsonify({"assignments": assignments, "exam_ids": exam_ids})
    finally:
        cursor.close()
        conn.close()


@app.route("/api/admin/users/<int:user_id>/assignments", methods=["POST"])
@admin_required
def admin_set_assignments(user_id):
    """Replace a user's assignments.
    Body: {assignments: [{exam_id:int, subject_id:int}], exam_ids: [int]}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        pairs = {(int(a["exam_id"]), int(a["subject_id"]))
                 for a in (data.get("assignments") or [])}
        exam_ids = {int(x) for x in (data.get("exam_ids") or [])}
    except (TypeError, ValueError, KeyError):
        return jsonify({"success": False, "error": "Invalid assignment payload"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            return jsonify({"success": False, "error": "User not found"}), 404

        cursor.execute("DELETE FROM verifier_assignments WHERE user_id = %s", (user_id,))
        for exam_id, subject_id in pairs:
            cursor.execute(
                "INSERT INTO verifier_assignments (user_id, exam_id, subject_id) VALUES (%s, %s, %s)",
                (user_id, exam_id, subject_id))

        cursor.execute("DELETE FROM verifier_exams WHERE user_id = %s", (user_id,))
        for eid in exam_ids:
            cursor.execute(
                "INSERT INTO verifier_exams (user_id, exam_id) VALUES (%s, %s)",
                (user_id, eid))

        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        cursor.close()
        conn.close()

# --- Admin Session / Database Routes ---

@app.route("/api/admin/exams/<int:exam_id>", methods=["DELETE"])
@admin_required
def admin_delete_exam(exam_id):
    """Delete an exam session: removes its questions, the exam row, and the
    extracted_frames/exam_<id>/ folder on disk. Irreversible."""
    if pipeline.is_running:
        return jsonify({"success": False,
                        "error": "Cannot delete sessions while a pipeline is running."}), 409

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500

    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, label FROM exams WHERE id = %s", (exam_id,))
    exam = cursor.fetchone()
    if not exam:
        cursor.close()
        conn.close()
        return jsonify({"success": False, "error": "Exam not found"}), 404

    try:
        # Questions FK to exams (no CASCADE) — delete children first.
        cursor.execute("DELETE FROM questions WHERE exam_id = %s", (exam_id,))
        deleted_questions = cursor.rowcount
        cursor.execute("DELETE FROM exams WHERE id = %s", (exam_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        cursor.close()
        conn.close()
        return jsonify({"success": False, "error": f"DB error: {e}"}), 500
    finally:
        cursor.close()
        conn.close()

    # Remove the on-disk frame folder. Best-effort — DB delete already succeeded.
    folder = os.path.join(INPUT_DIR, f"exam_{exam_id}")
    folder_removed = False
    if os.path.isdir(folder):
        try:
            shutil.rmtree(folder)
            folder_removed = True
        except Exception as e:
            return jsonify({
                "success": True,
                "deleted_questions": deleted_questions,
                "label": exam["label"],
                "folder_removed": False,
                "warning": f"DB rows deleted but could not remove {folder}: {e}",
            })

    return jsonify({
        "success": True,
        "deleted_questions": deleted_questions,
        "label": exam["label"],
        "folder_removed": folder_removed,
    })


def _sql_quote(v):
    """Render a Python value as a MySQL SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return "'" + v.isoformat(sep=" ") + "'"
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex() if v else "''"
    s = str(v)
    s = s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r").replace("\x00", "\\0")
    return "'" + s + "'"


def _python_sql_dump(db_name):
    """Fallback SQL dump in pure Python (used when mysqldump isn't on PATH)."""
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database connection failed")
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]

        lines = [
            f"-- OCR Extractor Database Backup (python fallback)",
            f"-- Database: {db_name}",
            f"-- Generated: {datetime.now().isoformat()}",
            "",
            "SET FOREIGN_KEY_CHECKS = 0;",
            "SET NAMES utf8mb4;",
            "",
        ]
        for table in tables:
            cursor.execute(f"SHOW CREATE TABLE `{table}`")
            create_stmt = cursor.fetchone()[1]
            lines.append(f"-- ---- Table: {table} ----")
            lines.append(f"DROP TABLE IF EXISTS `{table}`;")
            lines.append(create_stmt + ";")
            lines.append("")

            cursor.execute(f"SELECT * FROM `{table}`")
            rows = cursor.fetchall()
            if rows:
                cols = [d[0] for d in cursor.description]
                col_list = ", ".join(f"`{c}`" for c in cols)
                for row in rows:
                    values = ", ".join(_sql_quote(v) for v in row)
                    lines.append(f"INSERT INTO `{table}` ({col_list}) VALUES ({values});")
                lines.append("")

        lines.append("SET FOREIGN_KEY_CHECKS = 1;")
        return "\n".join(lines)
    finally:
        cursor.close()
        conn.close()


@app.route("/api/admin/db_backup", methods=["GET"])
@admin_required
def admin_db_backup():
    """Generate a full SQL dump and stream it as a downloadable file."""
    db_host = os.getenv("DB_HOST", "localhost")
    db_user = os.getenv("DB_USER", "root")
    db_pass = os.getenv("DB_PASSWORD", "")
    db_name = os.getenv("DB_NAME", "ocr_extractor")

    sql_text = None
    used_mysqldump = False

    # Try mysqldump first — cleanest output, schema-accurate.
    try:
        cmd = ["mysqldump", "-h", db_host, "-u", db_user]
        if db_pass:
            cmd.append(f"--password={db_pass}")
        cmd += ["--single-transaction", "--routines", "--triggers", db_name]
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        if result.returncode == 0:
            sql_text = result.stdout
            used_mysqldump = True
        # else: fall through to Python fallback
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if sql_text is None:
        try:
            sql_text = _python_sql_dump(db_name)
        except Exception as e:
            return jsonify({"error": f"Backup failed: {e}"}), 500

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{db_name}_backup_{timestamp}.sql"

    response = Response(sql_text, mimetype="application/sql")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["X-Backup-Source"] = "mysqldump" if used_mysqldump else "python"
    return response


@app.route("/api/admin/export_docx", methods=["GET"])
@admin_required
def admin_export_docx():
    """Export one subject within one session to a .docx download. Formulas are
    rendered as inline images and question diagrams are embedded."""
    exam_id = request.args.get("exam_id", type=int)
    subject = request.args.get("subject")
    if not exam_id or not subject:
        return jsonify({"error": "exam_id and subject are required"}), 400

    try:
        from docx_exporter import build_subject_docx
        buf, filename = build_subject_docx(exam_id, subject)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Export failed: {e}"}), 500

    response = Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# --- Manual Review: Unidentified Frames ---

@app.route("/api/admin/all_subjects", methods=["GET"])
@admin_required
def admin_all_subjects():
    """Return the canonical subject list (with expected question counts)."""
    from frame_extractor import SUBJECTS as _SUBJECTS
    out = [{"name": name, "expected": count} for name, count in _SUBJECTS.items()]
    out.sort(key=lambda s: s["name"])
    return jsonify(out)


@app.route("/api/admin/unidentified", methods=["GET"])
@admin_required
def admin_list_unidentified():
    """List unidentified frames. Filter by ?exam_id=N and ?status=pending|resolved|discarded."""
    exam_id = request.args.get("exam_id", type=int)
    status = request.args.get("status", default="pending")
    if status not in ("pending", "resolved", "discarded", "all"):
        return jsonify({"error": "invalid status"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
    cursor = conn.cursor(dictionary=True)
    try:
        clauses, params = [], []
        if exam_id:
            clauses.append("u.exam_id = %s"); params.append(exam_id)
        if status != "all":
            clauses.append("u.status = %s"); params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor.execute(f"""
            SELECT u.id, u.exam_id, u.frame_number, u.image_path, u.video_timestamp,
                   u.ocr_text, u.ocr_subject, u.ocr_question_number, u.status,
                   u.resolved_subject, u.resolved_question_number,
                   u.resolved_at, u.created_at,
                   e.label AS exam_label, e.year AS exam_year,
                   ru.username AS resolved_by_username,
                   q.id AS question_id,
                   (q.question_text IS NOT NULL AND q.question_text <> '') AS ai_extracted
            FROM unidentified_frames u
            JOIN exams e ON u.exam_id = e.id
            LEFT JOIN users ru ON u.resolved_by = ru.id
            LEFT JOIN subjects rs ON rs.name = u.resolved_subject
            LEFT JOIN questions q
                   ON q.exam_id = u.exam_id
                  AND q.subject_id = rs.id
                  AND q.question_number = u.resolved_question_number
            {where}
            ORDER BY u.exam_id ASC, u.frame_number ASC
            LIMIT 500
        """, tuple(params))
        rows = cursor.fetchall()
        for r in rows:
            for k in ("resolved_at", "created_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        return jsonify(rows)
    finally:
        cursor.close()
        conn.close()


@app.route("/api/admin/unidentified/<int:uid>/resolve", methods=["POST"])
@admin_required
def admin_resolve_unidentified(uid):
    """Promote an unidentified frame into a real question row.
    Body: {subject: str, question_number: int}"""
    from frame_extractor import SUBJECTS as _SUBJECTS
    data = request.get_json(force=True, silent=True) or {}
    subject = (data.get("subject") or "").strip().upper()
    try:
        q_num = int(data.get("question_number"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "question_number must be an integer"}), 400

    if subject not in _SUBJECTS:
        return jsonify({"success": False, "error": f"Unknown subject '{subject}'"}), 400
    expected_max = _SUBJECTS[subject]
    if not (1 <= q_num <= expected_max):
        return jsonify({"success": False,
                        "error": f"question_number must be 1..{expected_max} for {subject}"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500

    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, exam_id, image_path, video_timestamp, status
            FROM unidentified_frames WHERE id = %s
        """, (uid,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Unidentified frame not found"}), 404
        if row["status"] != "pending":
            return jsonify({"success": False, "error": f"Already {row['status']}"}), 409

        exam_id = row["exam_id"]

        # Ensure subject row exists, get its id.
        cursor.execute("SELECT id FROM subjects WHERE name = %s", (subject,))
        s_row = cursor.fetchone()
        if s_row:
            subject_id = s_row["id"]
        else:
            cursor.execute("INSERT INTO subjects (name) VALUES (%s)", (subject,))
            conn.commit()
            subject_id = cursor.lastrowid

        # Refuse if a question already exists at this slot (avoid silent overwrite).
        cursor.execute("""
            SELECT id FROM questions
            WHERE exam_id = %s AND subject_id = %s AND question_number = %s
        """, (exam_id, subject_id, q_num))
        if cursor.fetchone():
            return jsonify({
                "success": False,
                "error": f"A question already exists at {subject} Q{q_num} for this exam."
            }), 409

        # Move/copy the image into the canonical exam/subject folder.
        src = os.path.join(INPUT_DIR, row["image_path"])
        subj_folder = subject.lower().replace(" ", "_")
        dst_dir = os.path.join(INPUT_DIR, f"exam_{exam_id}", subj_folder)
        os.makedirs(dst_dir, exist_ok=True)
        dst_name = f"question_{q_num}.jpg"
        dst = os.path.join(dst_dir, dst_name)
        moved = False
        if os.path.isfile(src):
            try:
                shutil.move(src, dst)
                moved = True
            except Exception:
                try:
                    shutil.copy2(src, dst); moved = True
                except Exception as e:
                    return jsonify({"success": False,
                                    "error": f"Image copy failed: {e}"}), 500
        else:
            # The source image may have been cleaned up — proceed anyway.
            pass

        # Insert the question row.
        cursor.execute("""
            INSERT INTO questions
              (exam_id, subject_id, question_number, image_name, video_timestamp)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
        """, (exam_id, subject_id, q_num, dst_name, row.get("video_timestamp") or ""))
        conn.commit()
        q_id = cursor.lastrowid or None
        if not q_id:
            cursor.execute("SELECT id FROM questions WHERE exam_id=%s AND subject_id=%s AND question_number=%s",
                           (exam_id, subject_id, q_num))
            r2 = cursor.fetchone()
            q_id = r2["id"] if r2 else None

        # Mark the unidentified row resolved.
        cursor.execute("""
            UPDATE unidentified_frames
            SET status = 'resolved', resolved_by = %s, resolved_at = NOW(),
                resolved_subject = %s, resolved_question_number = %s
            WHERE id = %s
        """, (session.get("user_id"), subject, q_num, uid))
        conn.commit()

        # Trigger AI extraction now. extract_one_async spawns a worker thread
        # that runs independent of the pipeline-bound queue (which is idle here).
        ai_triggered = False
        if q_id:
            try:
                pipeline.extract_one_async(q_id, force=False)
                ai_triggered = True
            except Exception as e:
                print(f"[resolve] Could not trigger AI extraction: {e}")

        return jsonify({
            "success": True,
            "question_id": q_id,
            "image_moved": moved,
            "subject": subject,
            "question_number": q_num,
            "ai_triggered": ai_triggered,
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": f"DB error: {e}"}), 500
    finally:
        cursor.close()
        conn.close()


@app.route("/api/admin/unidentified/<int:uid>/run_ai", methods=["POST"])
@admin_required
def admin_run_ai_for_unidentified(uid):
    """Trigger (or re-trigger with force=true) AI extraction for the question
    that was created from a resolved unidentified frame."""
    data = request.get_json(force=True, silent=True) or {}
    force = bool(data.get("force", False))

    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500

    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT u.status, u.resolved_subject, u.resolved_question_number, u.exam_id,
                   q.id AS question_id, q.question_text
            FROM unidentified_frames u
            LEFT JOIN subjects s ON s.name = u.resolved_subject
            LEFT JOIN questions q
                   ON q.exam_id = u.exam_id
                  AND q.subject_id = s.id
                  AND q.question_number = u.resolved_question_number
            WHERE u.id = %s
        """, (uid,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Not found"}), 404
        if row["status"] != "resolved":
            return jsonify({"success": False, "error": "Frame must be resolved first."}), 409
        if not row["question_id"]:
            return jsonify({"success": False, "error": "Linked question row not found."}), 500
        if row["question_text"] and not force:
            return jsonify({
                "success": False,
                "error": "Already extracted. Pass force=true to re-run.",
                "question_id": row["question_id"],
            }), 409
    finally:
        cursor.close()
        conn.close()

    try:
        pipeline.extract_one_async(row["question_id"], force=force)
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not start AI worker: {e}"}), 500

    return jsonify({"success": True, "question_id": row["question_id"], "force": force})


@app.route("/api/admin/unidentified/<int:uid>/discard", methods=["POST"])
@admin_required
def admin_discard_unidentified(uid):
    """Mark an unidentified frame as 'not a question' and remove its image."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"success": False, "error": "Database connection failed"}), 500
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT image_path, status FROM unidentified_frames WHERE id = %s", (uid,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Not found"}), 404
        if row["status"] != "pending":
            return jsonify({"success": False, "error": f"Already {row['status']}"}), 409

        cursor.execute("""
            UPDATE unidentified_frames
            SET status = 'discarded', resolved_by = %s, resolved_at = NOW()
            WHERE id = %s
        """, (session.get("user_id"), uid))
        conn.commit()

        # Best-effort: remove the orphaned image.
        src = os.path.join(INPUT_DIR, row["image_path"])
        if os.path.isfile(src):
            try:
                os.remove(src)
            except Exception:
                pass
        return jsonify({"success": True})
    finally:
        cursor.close()
        conn.close()


@app.route("/unidentified_image/<int:uid>")
@admin_required
def serve_unidentified_image(uid):
    """Serve the raw image for a given unidentified-frame row."""
    conn = get_db_connection()
    if not conn:
        return "DB error", 500
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT image_path FROM unidentified_frames WHERE id = %s", (uid,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        return "Not found", 404
    full = os.path.join(INPUT_DIR, row["image_path"])
    if not os.path.isfile(full):
        return "Image missing on disk", 404
    return send_from_directory(os.path.dirname(full), os.path.basename(full))


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True, port=5000)
