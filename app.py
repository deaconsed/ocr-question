import os
import json
import base64
from flask import Flask, render_template, jsonify, request, send_from_directory, Response, session, redirect, url_for
import threading
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
from pipeline import pipeline
from db import get_db_connection

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-12345")
INPUT_DIR = "extracted_frames"

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

@app.route("/api/lock_question", methods=["POST"])
@login_required
def lock_question():
    data = request.json
    subject = data.get("subject")
    question_number = data.get("question_number")
    exam_id = data.get("exam_id")
    user_id = session.get("user_id")
    
    if not subject or not question_number:
        return jsonify({"error": "Missing required data"}), 400
        
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
        
    cursor = conn.cursor()
    
    if exam_id:
        cursor.execute("""
            UPDATE questions q
            JOIN subjects s ON q.subject_id = s.id
            SET q.locked_by = %s, q.locked_at = CURRENT_TIMESTAMP
            WHERE s.name = %s AND q.question_number = %s AND q.exam_id = %s
        """, (user_id, subject, question_number, exam_id))
    else:
        cursor.execute("""
            UPDATE questions q
            JOIN subjects s ON q.subject_id = s.id
            SET q.locked_by = %s, q.locked_at = CURRENT_TIMESTAMP
            WHERE s.name = %s AND q.question_number = %s
        """, (user_id, subject, question_number))
    
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/unlock_question", methods=["POST"])
@login_required
def unlock_question():
    data = request.json
    subject = data.get("subject")
    question_number = data.get("question_number")
    exam_id = data.get("exam_id")
    user_id = session.get("user_id")
    
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database connection failed"}), 500
        
    cursor = conn.cursor()
    
    if exam_id:
        cursor.execute("""
            UPDATE questions q
            JOIN subjects s ON q.subject_id = s.id
            SET q.locked_by = NULL, q.locked_at = NULL
            WHERE s.name = %s AND q.question_number = %s AND q.locked_by = %s AND q.exam_id = %s
        """, (subject, question_number, user_id, exam_id))
    else:
        cursor.execute("""
            UPDATE questions q
            JOIN subjects s ON q.subject_id = s.id
            SET q.locked_by = NULL, q.locked_at = NULL
            WHERE s.name = %s AND q.question_number = %s AND q.locked_by = %s
        """, (subject, question_number, user_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"success": True})

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
    
    if not video_path or not os.path.exists(video_path):
        return jsonify({"error": "Video not found"}), 404
    if not year:
        return jsonify({"error": "Year is required"}), 400
        
    threading.Thread(target=pipeline.run_pipeline, args=(video_path, year), daemon=True).start()
    return jsonify({"success": True, "message": f"Started processing {video_path}"})

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

if __name__ == "__main__":
    app.run(debug=True, port=5000)
