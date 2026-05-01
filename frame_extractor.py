import cv2
import os
import re
import numpy as np
import easyocr
import json
from db import get_db_connection

# ==========================================
# CONFIGURATION
# ==========================================
OUTPUT_DIR = "extracted_frames"
STATE_FILE = "extraction_state.json"
DEBUG_MODE = False

# How often to check for a new question (in seconds of video time)
CHECK_INTERVAL_SEC = 10  # OCR every 10 seconds of video

# Workspace crop (relative to full frame)
WORKSPACE_LIMITS = {
    'top': 0.12,    # Exclude browser URL bar
    'bottom': 0.75, # Exclude navigator grid
    'left': 0.0,
    'right': 0.82   # Exclude webcam/info sidebar
}

# ==========================================
# SUBJECT DEFINITIONS
# ==========================================
SUBJECTS = {
    "USE OF ENGLISH": 60,
    "MATHEMATICS": 40,
    "PHYSICS": 40,
    "CHEMISTRY": 40,
    "BIOLOGY": 40,
    "AGRICULTURAL SCIENCE": 40,
    "ECONOMICS": 40,
    "LITERATURE IN ENGLISH": 40,
    "GOVERNMENT": 40,
    "COMMERCE": 40,
    "PRINCIPLES OF ACCOUNTS": 40,
    "CHRISTIAN RELIGIOUS KNOWLEDGE": 40,
    "ISLAMIC RELIGIOUS KNOWLEDGE": 40,
    "GEOGRAPHY": 40,
    "HISTORY": 40,
    "FRENCH": 40,
    "ARABIC": 40,
    "HAUSA": 40,
    "IGBO": 40,
    "YORUBA": 40,
    "MUSIC": 40,
    "FINE ARTS": 40,
    "COMPUTER STUDIES": 40,
    "PHYSICAL AND HEALTH EDUCATION": 40,
    "HOME ECONOMICS": 40,
}
TOTAL_EXPECTED = 180 # JAMB format: English (60) + 3 subjects (40 each)

# ==========================================
# CALCULATOR BUTTON DETECTION (HSV)
# ==========================================
CALC_GREEN_LOWER_HSV = np.array([35, 80, 100])
CALC_GREEN_UPPER_HSV = np.array([85, 255, 255])
CALC_MIN_GREEN_PIXELS = 800


def has_calculator_button(frame, height, width):
    """Check if the frame contains the CALCULATOR button in the top subject bar."""
    top_bar = frame[0:int(height * 0.15), 0:int(width * 0.65)]
    hsv = cv2.cvtColor(top_bar, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, CALC_GREEN_LOWER_HSV, CALC_GREEN_UPPER_HSV)
    return cv2.countNonZero(green_mask) > CALC_MIN_GREEN_PIXELS


def extract_question_info(reader, workspace):
    """
    Use EasyOCR to extract the subject name and question number
    from the workspace crop.
    Returns: (subject_name: str or None, question_number: int or None)
    """
    ws_h, ws_w = workspace.shape[:2]

    # Crop the subject + question text area
    ocr_crop = workspace[int(ws_h * 0.08):int(ws_h * 0.25), 0:int(ws_w * 0.5)]

    # Upscale 3x for better OCR accuracy
    scaled = cv2.resize(ocr_crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

    # Preprocess: grayscale + threshold
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)

    # Run OCR
    results = reader.readtext(thresh, detail=0, paragraph=False)
    text = " ".join(results).strip()

    # --- Extract Subject ---
    subject = None
    text_no_space = text.lower().replace(" ", "").replace("0", "o")
    
    # Map subjects to possible OCR keywords (order matters for overlapping terms)
    SUBJECT_KEYWORDS = {
        "LITERATURE IN ENGLISH": ["literature", "literat"],
        "USE OF ENGLISH": ["english", "englis"],
        "MATHEMATICS": ["mathematics", "math", "maths"],
        "PHYSICS": ["physics", "physic", "phys"],
        "CHEMISTRY": ["chemistry", "chemist", "chem"],
        "BIOLOGY": ["biology", "biol", "bio"],
        "AGRICULTURAL SCIENCE": ["agricultural", "agric"],
        "HOME ECONOMICS": ["homeecon", "homeeco"],
        "ECONOMICS": ["economics", "econom", "econ"],
        "GOVERNMENT": ["government", "govern", "gov"],
        "COMMERCE": ["commerce", "commerc"],
        "PRINCIPLES OF ACCOUNTS": ["accounts", "account", "accounting", "principle"],
        "CHRISTIAN RELIGIOUS KNOWLEDGE": ["christian", "crk", "c.r.k"],
        "ISLAMIC RELIGIOUS KNOWLEDGE": ["islamic", "irk", "i.r.k", "islam"],
        "GEOGRAPHY": ["geography", "geograph", "geog"],
        "HISTORY": ["history", "hist"],
        "FRENCH": ["french", "frenc"],
        "ARABIC": ["arabic", "arab"],
        "HAUSA": ["hausa"],
        "IGBO": ["igbo"],
        "YORUBA": ["yoruba"],
        "MUSIC": ["music"],
        "FINE ARTS": ["fineart", "art"],
        "COMPUTER STUDIES": ["computer", "comp"],
        "PHYSICAL AND HEALTH EDUCATION": ["physical", "healthedu", "phe", "p.h.e"],
    }

    for subj, keywords in SUBJECT_KEYWORDS.items():
        if any(kw in text_no_space for kw in keywords):
            subject = subj
            break

    # --- Extract Question Number ---
    question_num = None
    match = re.search(r'[QqOo]uestion\s*(\d+)', text, re.IGNORECASE)
    if match:
        question_num = int(match.group(1))

    return subject, question_num


def create_exam(cursor, conn, video_filename, session_index, year):
    """Create a new exam row in the database and return its ID."""
    label = f"{year} - {os.path.basename(video_filename)} - Session {session_index}"
    cursor.execute("""
        INSERT INTO exams (video_filename, session_index, year, label, status)
        VALUES (%s, %s, %s, %s, 'in_progress')
        ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id), year=VALUES(year), label=VALUES(label)
    """, (os.path.basename(video_filename), session_index, year, label))
    conn.commit()
    cursor.execute("SELECT LAST_INSERT_ID()")
    return cursor.fetchone()[0]


def process_video(video_path, emit_log, gpt_queue, year, check_stop=None):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Initialize DB Connection
    emit_log("Connecting to database...")
    conn = get_db_connection()
    if not conn:
        emit_log("Failed to connect to MySQL database. Exiting.")
        return
    cursor = conn.cursor()

    # Sync subjects with DB
    subject_ids = {}
    for subj in SUBJECTS.keys():
        cursor.execute("SELECT id FROM subjects WHERE name = %s", (subj,))
        result = cursor.fetchone()
        if result:
            subject_ids[subj] = result[0]
        else:
            cursor.execute("INSERT INTO subjects (name) VALUES (%s)", (subj,))
            conn.commit()
            cursor.execute("SELECT id FROM subjects WHERE name = %s", (subj,))
            subject_ids[subj] = cursor.fetchone()[0]

    # Initialize EasyOCR
    emit_log("Initializing EasyOCR reader...")
    reader = easyocr.Reader(['en'], gpu=False, verbose=False)
    emit_log("EasyOCR ready.")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        emit_log(f"Error: Could not open video file {video_path}")
        return

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    skip_frames = int(fps * CHECK_INTERVAL_SEC)

    emit_log(f"Processing Video: {width}x{height} @ {fps} FPS | Total Frames: {total_frames}")
    emit_log(f"Expected per session: {TOTAL_EXPECTED} questions (JAMB format)")
    emit_log(f"Checking every {CHECK_INTERVAL_SEC}s ({skip_frames} frames)")

    # Workspace crop coordinates
    ws_top = int(height * WORKSPACE_LIMITS['top'])
    ws_bottom = int(height * WORKSPACE_LIMITS['bottom'])
    ws_left = int(width * WORKSPACE_LIMITS['left'])
    ws_right = int(width * WORKSPACE_LIMITS['right'])

    # Load State
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception:
            pass

    vid_key = os.path.basename(video_path)
    vid_state = state.get(vid_key, {
        "frame": 0, 
        "quiz_started": False, 
        "session_index": 1,
        "exam_id": None
    })
    frame_number = vid_state["frame"]
    quiz_started = vid_state["quiz_started"]
    session_index = vid_state.get("session_index", 1)
    current_exam_id = vid_state.get("exam_id", None)

    if frame_number > 0:
        emit_log(f"Resuming from saved state: frame {frame_number}, session {session_index}...")
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)

    # Create or resume exam
    if current_exam_id:
        cursor.execute("SELECT id FROM exams WHERE id = %s", (current_exam_id,))
        if not cursor.fetchone():
            emit_log(f"Warning: Saved Exam ID {current_exam_id} not found in DB (wiped?). Creating new...")
            current_exam_id = None

    if current_exam_id is None:
        current_exam_id = create_exam(cursor, conn, video_path, session_index, year)
        emit_log(f"Created Exam Session {session_index} (ID: {current_exam_id})")
    else:
        emit_log(f"Resuming Exam Session {session_index} (ID: {current_exam_id})")

    def save_state(frame_num, started):
        state[vid_key] = {
            "frame": frame_num, 
            "quiz_started": started,
            "session_index": session_index,
            "exam_id": current_exam_id
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    # ========================================
    # PHASE 1: Skip to quiz start
    # ========================================
    if not quiz_started:
        emit_log("Scanning for quiz start (CALCULATOR button)...")

    while not quiz_started:
        if check_stop and check_stop():
            emit_log("Process aborted by user during quiz scanning.")
            cap.release()
            return

        # Skip 10 frames at a time while scanning for quiz start
        for _ in range(9):
            cap.grab()
            frame_number += 1

        ret, frame = cap.read()
        if not ret:
            emit_log("End of video — quiz screen never found!")
            return
        frame_number += 1

        if has_calculator_button(frame, height, width):
            quiz_started = True
            save_state(frame_number, True)
            emit_log(f">>> Quiz UI Detected at frame {frame_number}!")

    # ========================================
    # PHASE 2: OCR every CHECK_INTERVAL_SEC seconds
    # ========================================
    # Tracking: {subject: {question_num: filename}}
    captured = {}
    for subj in SUBJECTS:
        captured[subj] = {}
        
    # Pre-populate captured from Database (scoped to current exam)
    if cursor:
        cursor.execute("""
            SELECT s.name, q.question_number, q.image_name 
            FROM questions q 
            JOIN subjects s ON q.subject_id = s.id
            WHERE q.exam_id = %s
        """, (current_exam_id,))
        for s_name, q_num, img_name in cursor.fetchall():
            if s_name in captured:
                captured[s_name][q_num] = img_name
                
    # Track all sessions' reports for final summary
    session_reports = []
    
    ocr_failures = 0
    last_key = None  # (subject, q_num) of the last detected question
    checks_done = 0

    while True:
        if check_stop and check_stop():
            emit_log("Process aborted by user during OCR scanning.")
            break

        # Skip to next check point
        for _ in range(skip_frames - 1):
            cap.grab()
            frame_number += 1

        ret, frame = cap.read()
        if not ret:
            emit_log("End of video stream.")
            break
        frame_number += 1
        checks_done += 1

        # Validate: must be a quiz screen
        if not has_calculator_button(frame, height, width):
            continue

        # Crop workspace
        workspace = frame[ws_top:ws_bottom, ws_left:ws_right]

        # OCR: get subject + question number
        subject, q_num = extract_question_info(reader, workspace)

        if subject and q_num:
            key = (subject, q_num)

            # Skip if same as last check (no change)
            if key == last_key:
                continue

            last_key = key

            # ── SESSION BOUNDARY DETECTION ──
            # If we already have this exact (subject, question_number) in this session,
            # it means a new student has started their exam.
            if q_num in captured[subject]:
                # Save report for current session before switching
                session_reports.append({
                    "session_index": session_index,
                    "exam_id": current_exam_id,
                    "captured": {s: dict(qs) for s, qs in captured.items()}
                })
                
                # Mark current exam as complete
                cursor.execute("UPDATE exams SET status = 'complete' WHERE id = %s", (current_exam_id,))
                conn.commit()
                
                total_in_session = sum(len(v) for v in captured.values())
                emit_log(f"\n{'='*55}")
                emit_log(f"  SESSION BOUNDARY DETECTED at frame {frame_number}!")
                emit_log(f"  Session {session_index} captured {total_in_session} questions.")
                emit_log(f"  Detected duplicate: {subject} Q{q_num}")
                emit_log(f"  Starting new session...")
                emit_log(f"{'='*55}\n")
                
                # Start new session
                session_index += 1
                current_exam_id = create_exam(cursor, conn, video_path, session_index, year)
                emit_log(f"Created Exam Session {session_index} (ID: {current_exam_id})")
                
                # Reset captured
                captured = {}
                for subj in SUBJECTS:
                    captured[subj] = {}
                
                # Save state
                save_state(frame_number, True)

            # Skip if already captured (in current session)
            if q_num in captured[subject]:
                continue

            # Save!
            subj_folder = subject.lower().replace(" ", "_")
            # Use exam-scoped subfolder: extracted_frames/exam_<id>/subject/
            exam_dir = os.path.join(OUTPUT_DIR, f"exam_{current_exam_id}", subj_folder)
            os.makedirs(exam_dir, exist_ok=True)

            image_basename = f"question_{q_num}.jpg"
            filename = os.path.join(exam_dir, image_basename)
            cv2.imwrite(filename, workspace)
            captured[subject][q_num] = filename

            # DB Insert
            elapsed_sec = frame_number / fps
            mins = int(elapsed_sec // 60)
            secs = int(elapsed_sec % 60)
            timestamp_str = f"{mins:02d}:{secs:02d}"
            
            try:
                # Ensure we have a valid subject ID
                s_id = subject_ids.get(subject)
                if s_id is None:
                    emit_log(f"  DB Error: Subject '{subject}' not found in mapping.")
                    continue

                cursor.execute("""
                    INSERT INTO questions 
                    (exam_id, subject_id, question_number, image_name, video_timestamp) 
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
                """, (current_exam_id, s_id, q_num, image_basename, timestamp_str))
                conn.commit()
                
                # Fetch the ID (from lastrowid)
                q_id = cursor.lastrowid
                if not q_id: # Fallback if lastrowid is 0
                    cursor.execute("SELECT LAST_INSERT_ID()")
                    q_id = cursor.fetchone()[0]
                
                # Push to GPT queue
                if gpt_queue is not None and q_id:
                    gpt_queue.put(q_id)
                    
            except Exception as e:
                emit_log(f"  DB Error: {e}")

            total_captured = sum(len(v) for v in captured.values())
            emit_log(f"  [S{session_index}] [{total_captured}/{TOTAL_EXPECTED}] {subject} Q{q_num} [{timestamp_str}] -> Extracted")
        else:
            # Only log if it looks like a new frame (not the same as last)
            if last_key is not None:
                ocr_failures += 1
                unk_dir = os.path.join(OUTPUT_DIR, f"exam_{current_exam_id}", "_unidentified")
                os.makedirs(unk_dir, exist_ok=True)
                filename = os.path.join(unk_dir, f"frame_{frame_number}.jpg")
                cv2.imwrite(filename, workspace)
                emit_log(f"  OCR partial (subj={subject}, q={q_num}) -> _unidentified")

        # Progress indicator every 50 checks
        if checks_done % 50 == 0:
            elapsed_vid_sec = frame_number / fps
            elapsed_vid_min = elapsed_vid_sec / 60
            total_captured = sum(len(v) for v in captured.values())
            save_state(frame_number, True)
            emit_log(f"  ... checked {checks_done} points | video time: {elapsed_vid_min:.1f}min | session {session_index} captured: {total_captured}")

    # Mark final exam as complete
    cursor.execute("UPDATE exams SET status = 'complete' WHERE id = %s", (current_exam_id,))
    conn.commit()
    
    # Add final session to reports
    session_reports.append({
        "session_index": session_index,
        "exam_id": current_exam_id,
        "captured": {s: dict(qs) for s, qs in captured.items()}
    })

    save_state(frame_number, True)
    cap.release()
    try:
        cv2.destroyAllWindows()
    except:
        pass

    # ==========================================
    # FINAL REPORT
    # ==========================================
    emit_log(f"\n{'='*55}")
    emit_log(f"  CAPTURE REPORT — {len(session_reports)} session(s) detected")
    emit_log(f"{'='*55}")
    
    grand_total = 0
    
    for report in session_reports:
        sess_captured = report["captured"]
        sess_total = sum(len(v) for v in sess_captured.values())
        grand_total += sess_total
        
        emit_log(f"\n  ── Session {report['session_index']} (Exam ID: {report['exam_id']}) ──")
        
        for subject, expected_count in SUBJECTS.items():
            captured_qs = sorted(sess_captured.get(subject, {}).keys())
            if len(captured_qs) == 0:
                continue
                
            missing = [q for q in range(1, expected_count + 1) if q not in captured_qs]
            status = "COMPLETE" if not missing else f"MISSING {len(missing)}"
            emit_log(f"    {subject}: {len(captured_qs)}/{expected_count} [{status}]")
        
        emit_log(f"    Session Total: {sess_total}/{TOTAL_EXPECTED}")

    emit_log(f"\n  GRAND TOTAL: {grand_total} questions across {len(session_reports)} session(s)")
    if ocr_failures > 0:
        emit_log(f"  OCR Failures: {ocr_failures} (saved to _unidentified/)")
    emit_log(f"{'='*55}")
    
    if cursor: cursor.close()
    if conn: conn.close()


if __name__ == "__main__":
    process_video("vid.mkv", print, None, None)
