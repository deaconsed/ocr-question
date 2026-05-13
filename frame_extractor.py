import cv2
import os
import re
import sys
import time
import numpy as np
import easyocr
import json
import warnings
from difflib import SequenceMatcher
from db import get_db_connection

# Fix for OpenMP duplicate library error.
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

# Thread count: keep the single-threaded crash workaround on Linux only — on
# Windows/macOS the 1-thread cap makes EasyOCR ~10x slower on CPU and looks
# like a hang (first OCR call took >60s in practice).
if sys.platform.startswith("linux"):
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("MKL_SERVICE_FORCE_INTEL", "1")
else:
    _cpu = os.cpu_count() or 4
    os.environ.setdefault("OMP_NUM_THREADS", str(min(4, _cpu)))
    os.environ.setdefault("MKL_NUM_THREADS", str(min(4, _cpu)))

# Suppress harmless PyTorch warning regarding pin_memory on CPU-only systems
warnings.filterwarnings("ignore", category=UserWarning, message=".*pin_memory.*")

# ==========================================
# CONFIGURATION
# ==========================================
OUTPUT_DIR = "extracted_frames"
STATE_FILE = "extraction_state.json"
DEBUG_MODE = False

# How often to check for a new question (in seconds of video time)
CHECK_INTERVAL_SEC = 10

# After capturing a question, skip this many seconds before the next check.
# Students take at least ~25s per question; skipping avoids pointless OCR calls.
POST_CAPTURE_SKIP_SEC = 25

# Average pixel diff (0–255) between 64×48 workspace thumbnails required to
# trigger OCR. Keeps the timer tick / minor UI flicker from firing OCR.
FRAME_DIFF_THRESHOLD = 6

# Workspace crop (relative to full frame)
# 'top' is now detected dynamically per-frame using the green bar anchor.
# These are fallback/fixed limits for bottom, left, and right.
WORKSPACE_LIMITS = {
    'top_fallback': 0.06,  # Only used if green bar detection fails
    'bottom': 0.85,        # Exclude navigation grid area
    'left': 0.0,
    'right': 0.85          # Exclude webcam/info sidebar
}

# ==========================================
# SUBJECT DEFINITIONS
# ==========================================
SUBJECTS = {
    "AGRICULTURE": 40,
    "ARABIC": 40,
    "ART": 40,
    "BIOLOGY": 40,
    "CHEMISTRY": 40,
    "CHRISTIAN RELIGIOUS STUDIES": 40,
    "COMMERCE": 40,
    "COMPUTER STUDIES": 40,
    "ECONOMICS": 40,
    "FRENCH": 40,
    "GEOGRAPHY": 40,
    "GOVERNMENT": 40,
    "HAUSA": 40,
    "HISTORY": 40,
    "HOME ECONOMICS": 40,
    "IGBO": 40,
    "ISLAMIC STUDIES": 40,
    "LITERATURE IN ENGLISH": 40,
    "MATHEMATICS": 40,
    "MUSIC": 40,
    "PHYSICS": 40,
    "PHYSICAL AND HEALTH EDUCATION": 40,
    "PRINCIPLES OF ACCOUNTS": 40,
    "USE OF ENGLISH": 60,
    "YORUBA": 40,
}
TOTAL_EXPECTED = 180 # JAMB format: English (60) + 3 subjects (40 each)

# ==========================================
# CALCULATOR BUTTON DETECTION (HSV)
# Threshold is now a ratio (percentage of the search area)
CALC_GREEN_LOWER_HSV = np.array([35, 60, 80])
CALC_GREEN_UPPER_HSV = np.array([90, 255, 255])
CALC_MIN_GREEN_RATIO = 0.002  # At least 0.2% of search area must be green (loosened for low-bitrate 480p)

def has_calculator_button(frame, height, width):
    """
    Detect the quiz subject-tab bar (multiple green buttons in a horizontal row).
    
    The CBT exam interface has a row of green subject tabs at the top:
      [BIOLOGY] [USE OF ENGLISH] [ECONOMICS] [AGRICULTURE] [CALCULATOR]
    
    This function looks for MULTIPLE separate green regions arranged horizontally,
    which distinguishes the quiz screen from other pages that may have a single
    green element (like the LOGIN button on the welcome page).
    """
    # Search in the top 15% of the frame (the tab bar area)
    search_h = int(height * 0.15)
    search_w = int(width * 0.80)
    top_bar = frame[0:search_h, 0:search_w]
    
    hsv = cv2.cvtColor(top_bar, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, CALC_GREEN_LOWER_HSV, CALC_GREEN_UPPER_HSV)
    
    # Check 1: Minimum green ratio (must have some green)
    green_pixels = cv2.countNonZero(green_mask)
    total_search_pixels = search_h * search_w
    current_ratio = green_pixels / total_search_pixels
    
    if current_ratio < CALC_MIN_GREEN_RATIO:
        return False
    
    # Check 2: Must have MULTIPLE separate green blobs (contours)
    # This distinguishes the subject-tab row from a single LOGIN button
    contours, _ = cv2.findContours(green_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    # Filter out tiny noise contours (must be at least 0.1% of search area)
    min_contour_area = total_search_pixels * 0.001
    significant_contours = [c for c in contours if cv2.contourArea(c) > min_contour_area]
    
    if DEBUG_MODE and current_ratio > (CALC_MIN_GREEN_RATIO * 0.5):
        print(f"    [Detection Debug] Green Ratio: {current_ratio:.5f}, Green Blobs: {len(significant_contours)} (need >= 3)")
    
    # Need at least 3 separate green buttons to confirm it's the subject tab bar
    return len(significant_contours) >= 3


def find_green_bar_bottom(frame, height, width):
    """
    Dynamically detect the bottom edge of the green subject-buttons bar.
    Scans the top 30% of the frame row-by-row looking for green-dominant rows.
    Returns the Y pixel coordinate just below the green bar.
    Returns None if no green bar is found.
    """
    search_h = int(height * 0.30)  # Only scan top 30%
    search_w = int(width * 0.70)   # Left 70% where buttons live
    top_region = frame[0:search_h, 0:search_w]

    hsv = cv2.cvtColor(top_region, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, CALC_GREEN_LOWER_HSV, CALC_GREEN_UPPER_HSV)

    # Scan each row: find the last row that has significant green pixels
    bar_bottom = None
    min_row_ratio = 0.01  # At least 1% of the row must be green
    in_bar = False

    for y in range(search_h):
        row_green = cv2.countNonZero(green_mask[y:y+1, :])
        row_ratio = row_green / search_w
        if row_ratio >= min_row_ratio:
            in_bar = True
            bar_bottom = y
        elif in_bar:
            # We left the green bar region; stop scanning
            break

    if bar_bottom is not None:
        # Add a small margin below the bar (2% of frame height)
        bar_bottom = min(bar_bottom + int(height * 0.02), height - 1)

    return bar_bottom


def workspace_changed(current_ws, prev_ws):
    """
    Cheap thumbnail diff: returns True only when the workspace content has
    changed enough to warrant a full EasyOCR call.

    Resizes both frames to a 64×48 grayscale thumbnail (~0.5 ms) and compares
    mean absolute pixel difference against FRAME_DIFF_THRESHOLD.
    """
    if prev_ws is None:
        return True
    curr = cv2.resize(current_ws, (64, 48), interpolation=cv2.INTER_NEAREST)
    prev = cv2.resize(prev_ws,    (64, 48), interpolation=cv2.INTER_NEAREST)
    curr_g = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
    prev_g = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    return cv2.absdiff(curr_g, prev_g).mean() > FRAME_DIFF_THRESHOLD


# Common OCR letter→digit confusions. Applied ONLY in numeric contexts so we
# don't corrupt the subject-name pass.
_LETTER_TO_DIGIT = str.maketrans({
    'I': '1', 'l': '1', '|': '1', 'i': '1', '!': '1',
    'O': '0', 'D': '0', 'o': '0', 'Q': '0',
    'S': '5', 's': '5',
    'B': '8',
    'Z': '2', 'z': '2',
    'G': '6',
    'T': '7',
})


def _letters_to_digits(s):
    """Best-effort: convert OCR letter-shapes to digit-shapes. Returns the
    converted string only if it ends up being all digits; otherwise None."""
    converted = s.translate(_LETTER_TO_DIGIT)
    return converted if converted.isdigit() else None


def _extract_question_number(text):
    """
    Locate the question number in OCR text using progressively looser rules.
    Returns int in [1, 60] or None.

    Handles:
      - "Question 5", "Qucstion 5", "Quesfion 12"  (mangled spellings)
      - "Q5", "Q.5", "Q-5"
      - "Question I2"  (digit 1 OCR'd as letter I  -> recovered as 12)
      - "5 of 40", "5/40", "5/60"
      - "Question I"   (single-glyph 1)
    """
    if not text:
        return None

    def _clamp(n):
        return n if 1 <= n <= 60 else None

    # 1. "Q...word DIGITS" — standard case, Q + 3-12 letters of "Question" + digits.
    m = re.search(r'[QqOo0][A-Za-z]{3,12}\s*[:\-\.\#]?\s*(\d{1,3})\b', text)
    if m:
        n = _clamp(int(m.group(1)))
        if n is not None:
            return n

    # 2. "Q...word LETTERS-AS-DIGITS" — digits OCR'd as letters like "Question I2".
    #    Capture a short alphanumeric run after "Question" and try to map to digits.
    m = re.search(r'[QqOo0][A-Za-z]{3,12}\s*[:\-\.\#]?\s*([A-Za-z0-9\|\!]{1,3})\b', text)
    if m:
        raw = m.group(1)
        digits = _letters_to_digits(raw)
        if digits:
            n = _clamp(int(digits))
            if n is not None:
                return n

    # 3. "Q5" / "Q.5" / "Q-5" compact form
    m = re.search(r'\bQ\s*[\.\-\#:]?\s*(\d{1,3})\b', text, re.IGNORECASE)
    if m:
        n = _clamp(int(m.group(1)))
        if n is not None:
            return n

    # 4. "5 of 40" / "5/40" — JAMB-style "Question X of Y" indicator.
    m = re.search(r'\b(\d{1,3})\s*(?:of|/)\s*(?:40|60|180)\b', text, re.IGNORECASE)
    if m:
        n = _clamp(int(m.group(1)))
        if n is not None:
            return n

    # 5. Letter-as-digit version of "X of 40": "I2 of 40" → 12.
    m = re.search(r'\b([A-Za-z0-9\|\!]{1,3})\s*(?:of|/)\s*(?:40|60|180)\b', text, re.IGNORECASE)
    if m:
        digits = _letters_to_digits(m.group(1))
        if digits:
            n = _clamp(int(digits))
            if n is not None:
                return n

    # 6. "Question I" / "Question l" single-glyph fallback → 1.
    if re.search(r'[QqOo0][A-Za-z]{3,12}\s*[:\-\.]?\s*[Il\|\!]\b', text):
        return 1

    return None


def _fuzzy_subject_match(text, min_ratio=0.65):
    """
    Last-resort matcher for OCR text the keyword pass couldn't classify.
    Strips text to letters-only, then compares the leading window against
    every SUBJECTS name with difflib. EasyOCR routinely mangles B↔p, e↔c,
    I↔l, O↔0, etc. — exact-substring lookups can't handle that.
    Returns the SUBJECTS key with the highest similarity (≥ min_ratio) or None.
    """
    if not text:
        return None
    norm = re.sub(r'[^a-z]', '', text.lower())
    if len(norm) < 4:
        return None
    # Subject name typically appears in the first ~30 letters of the header.
    header = norm[:40]
    best_subj, best_ratio = None, 0.0
    for subj in SUBJECTS:
        target = re.sub(r'[^a-z]', '', subj.lower())
        if len(target) < 3:
            continue
        # Compare against a window slightly longer than the subject name so we
        # tolerate a stray prefix or suffix character.
        window = header[:len(target) + 2]
        if len(window) < max(3, len(target) - 2):
            continue
        ratio = SequenceMatcher(None, target, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_subj = subj
    return best_subj if best_ratio >= min_ratio else None


def extract_question_info(reader, workspace, emit_log=None):
    """
    Use EasyOCR to extract the subject name and question number
    from the workspace crop.
    Returns: (subject_name: str or None, question_number: int or None)
    """
    if workspace is None or workspace.size == 0:
        if DEBUG_MODE: print("    [OCR Debug] Error: workspace is empty.")
        return None, None

    ws_h, ws_w = workspace.shape[:2]
    checks_done = getattr(extract_question_info, 'checks_done', 0)
    # Always emit the first few OCR samples so we can diagnose mismatches without DEBUG_MODE.
    verbose = DEBUG_MODE or checks_done <= 5

    # Crop the subject + question text area (relative to workspace).
    # Take a wide+tall slice so any reasonable header layout fits.
    ocr_crop = workspace[0:int(ws_h * 0.35), 0:int(ws_w * 0.75)]
    if ocr_crop.size == 0:
        if verbose and emit_log: emit_log("    [OCR Debug] Error: ocr_crop is empty.")
        return None, None

    gray = cv2.cvtColor(ocr_crop, cv2.COLOR_BGR2GRAY)

    # Save the first few crops for visual debugging — color (raw) + grayscale (OCR input).
    if checks_done <= 5:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            cv2.imwrite(os.path.join(OUTPUT_DIR, f"_debug_ocr_color_{checks_done}.png"), ocr_crop)
            cv2.imwrite(os.path.join(OUTPUT_DIR, f"_debug_ocr_gray_{checks_done}.png"), gray)
        except Exception:
            pass

    t0 = time.time()
    results = reader.readtext(gray, detail=0, paragraph=False, mag_ratio=1.5)
    elapsed_ms = (time.time() - t0) * 1000.0
    text = " ".join(results).strip()
    # Stash on the function so the caller can read the raw OCR text after the call
    # (used when persisting unidentified frames for later manual review).
    extract_question_info.last_text = text
    if verbose and emit_log:
        emit_log(f"    [OCR #{checks_done}] {elapsed_ms:.0f}ms | found {len(results)} items | text: '{text}'")
    elif DEBUG_MODE:
        print(f"    [OCR Debug] {elapsed_ms:.0f}ms | text: '{text}'")

    # --- Extract Subject ---
    subject = None
    text_no_space = text.lower().replace(" ", "").replace("0", "o")
    
    # Map SUBJECTS keys -> possible OCR keywords (order matters for overlapping terms).
    # IMPORTANT: keys here MUST exactly match keys in the SUBJECTS dict, otherwise
    # downstream `captured[subject]` lookups raise KeyError and kill the loop silently.
    SUBJECT_KEYWORDS = {
        "LITERATURE IN ENGLISH": ["literature", "literat"],
        "USE OF ENGLISH": ["useofenglish", "english", "englis"],
        "MATHEMATICS": ["mathematics", "math", "maths"],
        "PHYSICS": ["physics", "physic", "phys"],
        "CHEMISTRY": ["chemistry", "chemist", "chem"],
        "BIOLOGY": ["biology", "biol", "bio"],
        "AGRICULTURE": ["agricultural", "agriculture", "agric"],
        "HOME ECONOMICS": ["homeecon", "homeeco", "homeec"],
        "ECONOMICS": ["economics", "econom", "econ"],
        "GOVERNMENT": ["government", "govern", "gov"],
        "COMMERCE": ["commerce", "commerc"],
        "PRINCIPLES OF ACCOUNTS": ["accounts", "account", "accounting", "principle"],
        "CHRISTIAN RELIGIOUS STUDIES": ["christian", "crs", "crk", "c.r.s", "c.r.k"],
        "ISLAMIC STUDIES": ["islamic", "irs", "irk", "i.r.s", "i.r.k", "islam"],
        "GEOGRAPHY": ["geography", "geograph", "geog"],
        "HISTORY": ["history", "hist"],
        "FRENCH": ["french", "frenc"],
        "ARABIC": ["arabic", "arab"],
        "HAUSA": ["hausa"],
        "IGBO": ["igbo"],
        "YORUBA": ["yoruba"],
        "MUSIC": ["music"],
        "ART": ["fineart", "fine art", "visualart", "art"],
        "COMPUTER STUDIES": ["computer", "comp"],
        "PHYSICAL AND HEALTH EDUCATION": ["physical", "healthedu", "phe", "p.h.e"],
    }

    for subj, keywords in SUBJECT_KEYWORDS.items():
        if any(kw in text_no_space for kw in keywords):
            # Guard: a keyword map key MUST exist in SUBJECTS, otherwise downstream
            # captured[subject] lookups will KeyError. Treat as a missed read.
            if subj not in SUBJECTS:
                if DEBUG_MODE:
                    print(f"    [OCR Debug] Keyword matched unknown subject '{subj}'. Check SUBJECT_KEYWORDS keys.")
                continue
            subject = subj
            break

    # Fuzzy fallback for OCR errors no keyword list can predict (e.g. "piOLOGY" → BIOLOGY).
    if subject is None:
        subject = _fuzzy_subject_match(text)
        if verbose and subject and emit_log:
            emit_log(f"    [OCR #{checks_done}] fuzzy-matched subject: {subject}")

    # --- Extract Question Number (handles mangled spellings + letter→digit OCR errors) ---
    question_num = _extract_question_number(text)

    return subject, question_num


def _record_unidentified(cursor, conn, exam_id, frame_number, image_rel_path,
                         timestamp_str, ocr_text, ocr_subject, ocr_question_number):
    """
    Persist an unidentified frame row for later manual review by an admin.
    Best-effort: any DB error is swallowed (we still have the .jpg on disk).
    """
    try:
        cursor.execute("""
            INSERT INTO unidentified_frames
              (exam_id, frame_number, image_path, video_timestamp,
               ocr_text, ocr_subject, ocr_question_number, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (exam_id, frame_number, image_rel_path, timestamp_str,
              (ocr_text or "")[:4000], ocr_subject, ocr_question_number))
        conn.commit()
    except Exception:
        # Likely the table doesn't exist yet — caller will warn the user on startup.
        try:
            conn.rollback()
        except Exception:
            pass


def parse_timestamp(ts):
    """Convert 'HH:MM:SS', 'MM:SS', or plain seconds string/number to float seconds."""
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    ts = str(ts).strip()
    if not ts:
        return 0.0
    parts = ts.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(ts)
    except (ValueError, IndexError):
        return 0.0


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


def process_video(video_path, emit_log, gpt_queue, year, check_stop=None, sessions=None):
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
    # quantize=False helps prevent crashes on some older Linux CPUs
    reader = easyocr.Reader(['en'], gpu=False, verbose=False, quantize=False)
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

    # Parse admin-specified session timestamps (manual mode)
    manual_sessions = []
    if sessions:
        for i, s in enumerate(sessions, 1):
            st = parse_timestamp(s.get('start'))
            et = parse_timestamp(s.get('end'))
            manual_sessions.append({
                'index': i,
                'start_frame': int(st * fps),
                'end_frame': int(et * fps) if et > 0 else total_frames,
                'start_ts': s.get('start', ''),
                'end_ts': s.get('end', ''),
            })
        emit_log(f"Manual mode: {len(manual_sessions)} session(s) specified.")

    emit_log(f"Processing Video: {width}x{height} @ {fps} FPS | Total Frames: {total_frames}")
    emit_log(f"Expected per session: {TOTAL_EXPECTED} questions (JAMB format)")
    emit_log(f"Checking every {CHECK_INTERVAL_SEC}s ({skip_frames} frames)")

    # Workspace crop coordinates (top is detected dynamically per frame)
    ws_top_fallback = int(height * WORKSPACE_LIMITS['top_fallback'])
    ws_bottom = int(height * WORKSPACE_LIMITS['bottom'])
    ws_left = int(width * WORKSPACE_LIMITS['left'])
    ws_right = int(width * WORKSPACE_LIMITS['right'])
    cached_ws_top = None  # Will be set dynamically on first quiz frame

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
    # PHASE 1: Skip to quiz start (auto mode only)
    # ========================================
    if manual_sessions:
        quiz_started = True  # timestamps mean we skip auto-detection

    if not quiz_started:
        emit_log("Scanning for quiz start (CALCULATOR button)...")

    while not quiz_started:
        if check_stop and check_stop():
            emit_log("Process aborted by user during quiz scanning.")
            cap.release()
            return

        # Check every 2 seconds of video while waiting for the quiz UI
        phase1_grab = max(1, int(fps * 2) - 1)
        for _ in range(phase1_grab):
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
            # Save a snapshot of the detected quiz frame for verification
            if DEBUG_MODE:
                debug_quiz_path = os.path.join(OUTPUT_DIR, "_debug_quiz_detected.png")
                cv2.imwrite(debug_quiz_path, frame)
                emit_log(f"  [Debug] Saved quiz detection frame to {debug_quiz_path}")

    # ========================================
    # PHASE 2: OCR every CHECK_INTERVAL_SEC seconds
    # ========================================
    emit_log("Starting Phase 2: OCR Scanning Loop...")
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
    last_key = None       # (subject, q_num) of the last detected question
    checks_done = 0
    prev_workspace = None # last workspace thumbnail used for diff comparison
    extra_skip = 0        # additional frames to skip after a successful capture
    calc_button_misses = 0  # consecutive frames where has_calculator_button returned False
    last_calc_log = 0       # last checks_done count we logged calc-miss diagnostics for

    # ── MANUAL MODE: iterate over admin-specified session windows ──
    for sess in manual_sessions:
        if check_stop and check_stop():
            emit_log("Process aborted by user.")
            break

        session_index = sess['index']
        current_exam_id = create_exam(cursor, conn, video_path, session_index, year)
        emit_log(f"=== Session {session_index}: {sess['start_ts']} → {sess['end_ts']} (Exam ID: {current_exam_id}) ===")

        for subj in SUBJECTS:
            captured[subj] = {}
        prev_workspace = None
        extra_skip = 0
        last_key = None

        cap.set(cv2.CAP_PROP_POS_FRAMES, sess['start_frame'])
        frame_number = sess['start_frame']
        cached_ws_top = None  # Re-derive per session — UI layout may differ
        calc_button_misses = 0
        consecutive_read_failures = 0

        while frame_number < sess['end_frame']:
            if check_stop and check_stop():
                emit_log("Process aborted by user during OCR scanning.")
                break

            try:
                current_skip = skip_frames + extra_skip
                extra_skip = 0
                frames_to_skip = min(current_skip - 1, max(0, sess['end_frame'] - frame_number - 1))
                for _ in range(frames_to_skip):
                    cap.grab()
                    frame_number += 1

                if frame_number >= sess['end_frame']:
                    break

                ret, frame = cap.read()
                if not ret or frame is None:
                    consecutive_read_failures += 1
                    if consecutive_read_failures >= 5:
                        emit_log(f"  Read failed 5x in a row at frame {frame_number}; stopping session early.")
                        break
                    # Try to recover by seeking forward a few seconds
                    frame_number += skip_frames
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                    continue
                consecutive_read_failures = 0
                frame_number += 1
                checks_done += 1

                if not has_calculator_button(frame, height, width):
                    calc_button_misses += 1
                    # Diagnostic: if we've gone a long time without seeing the quiz UI,
                    # tell the user (something is probably wrong with detection or window).
                    if calc_button_misses in (10, 50, 200) or (calc_button_misses > 0 and calc_button_misses % 500 == 0):
                        elapsed_vid_sec = frame_number / fps
                        emit_log(
                            f"  [S{session_index}] No quiz UI detected for {calc_button_misses} consecutive checks "
                            f"(video {elapsed_vid_sec/60:.1f}min). "
                            f"If this persists, calibrate green-button detection or session timestamps."
                        )
                        # Save a diagnostic snapshot of the first long-miss frame
                        if calc_button_misses == 10:
                            try:
                                miss_dir = os.path.join(OUTPUT_DIR, f"exam_{current_exam_id}", "_no_quiz_ui")
                                os.makedirs(miss_dir, exist_ok=True)
                                cv2.imwrite(os.path.join(miss_dir, f"frame_{frame_number}.jpg"), frame)
                            except Exception:
                                pass
                    # Periodic state save even on misses
                    if checks_done % 25 == 0:
                        save_state(frame_number, True)
                    continue

                if calc_button_misses > 0:
                    if calc_button_misses >= 10:
                        emit_log(f"  [S{session_index}] Quiz UI re-acquired after {calc_button_misses} misses.")
                    calc_button_misses = 0

                # Re-derive the green-bar top every ~200 checks in case UI shifts.
                if cached_ws_top is None or checks_done % 200 == 0:
                    bar_bottom = find_green_bar_bottom(frame, height, width)
                    new_top = bar_bottom if bar_bottom is not None else ws_top_fallback
                    if cached_ws_top is None:
                        emit_log(f"  [S{session_index}] Workspace top calibrated: y={new_top} (frame {frame_number}).")
                    cached_ws_top = new_top

                workspace = frame[cached_ws_top:ws_bottom, ws_left:ws_right]
                if workspace.size == 0:
                    continue
                ws_copy = workspace.copy()
                if not workspace_changed(ws_copy, prev_workspace):
                    prev_workspace = ws_copy
                    if checks_done % 25 == 0:
                        save_state(frame_number, True)
                    continue
                prev_workspace = ws_copy

                extract_question_info.checks_done = checks_done
                subject, q_num = extract_question_info(reader, workspace, emit_log=emit_log)

                if subject and q_num:
                    # Defensive: refuse subjects not registered in captured/SUBJECTS.
                    if subject not in captured:
                        emit_log(f"  Warning: OCR returned unmapped subject '{subject}', skipping.")
                        continue

                    key = (subject, q_num)
                    if key == last_key:
                        continue
                    last_key = key

                    if q_num in captured[subject]:
                        continue

                    subj_folder = subject.lower().replace(" ", "_")
                    exam_dir = os.path.join(OUTPUT_DIR, f"exam_{current_exam_id}", subj_folder)
                    os.makedirs(exam_dir, exist_ok=True)
                    image_basename = f"question_{q_num}.jpg"
                    try:
                        cv2.imwrite(os.path.join(exam_dir, image_basename), workspace)
                    except Exception as e:
                        emit_log(f"  Write Error ({image_basename}): {e}")
                        continue
                    captured[subject][q_num] = image_basename

                    elapsed_sec = frame_number / fps
                    timestamp_str = f"{int(elapsed_sec // 60):02d}:{int(elapsed_sec % 60):02d}"

                    try:
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
                        q_id = cursor.lastrowid
                        if not q_id:
                            cursor.execute("SELECT LAST_INSERT_ID()")
                            q_id = cursor.fetchone()[0]
                        if gpt_queue is not None and q_id:
                            gpt_queue.put(q_id)
                    except Exception as e:
                        emit_log(f"  DB Error: {e}")

                    total_captured = sum(len(v) for v in captured.values())
                    emit_log(f"  [S{session_index}] [{total_captured}/{TOTAL_EXPECTED}] {subject} Q{q_num} [{timestamp_str}] -> Extracted")

                    extra_skip = int(fps * POST_CAPTURE_SKIP_SEC)
                    prev_workspace = None
                    save_state(frame_number, True)
                else:
                    # Save unidentified frames immediately — even before any successful
                    # capture — so we can see what OCR is missing.
                    ocr_failures += 1
                    unk_dir = os.path.join(OUTPUT_DIR, f"exam_{current_exam_id}", "_unidentified")
                    os.makedirs(unk_dir, exist_ok=True)
                    image_name = f"frame_{frame_number}.jpg"
                    image_rel = f"exam_{current_exam_id}/_unidentified/{image_name}"
                    try:
                        cv2.imwrite(os.path.join(unk_dir, image_name), workspace)
                    except Exception:
                        pass
                    elapsed_sec = frame_number / fps
                    ts_str = f"{int(elapsed_sec // 60):02d}:{int(elapsed_sec % 60):02d}"
                    raw_text = getattr(extract_question_info, "last_text", "")
                    _record_unidentified(cursor, conn, current_exam_id, frame_number,
                                         image_rel, ts_str, raw_text, subject, q_num)
                    if ocr_failures <= 10 or ocr_failures % 50 == 0:
                        emit_log(f"  [S{session_index}] OCR partial (subj={subject}, q={q_num}) -> _unidentified")

                # Progress + state checkpoint every 10 checks (~100s of video).
                if checks_done % 10 == 0:
                    elapsed_vid_sec = frame_number / fps
                    total_captured = sum(len(v) for v in captured.values())
                    emit_log(
                        f"  [S{session_index}] ... checked {checks_done} | "
                        f"video {elapsed_vid_sec/60:.1f}min | captured {total_captured}/{TOTAL_EXPECTED}"
                    )
                    save_state(frame_number, True)
            except Exception as e:
                # Never let one bad iteration kill the whole session.
                emit_log(f"  Iteration error at frame {frame_number}: {type(e).__name__}: {e}")
                import traceback as _tb
                emit_log(_tb.format_exc())
                # Skip ahead to avoid getting stuck on the same frame
                extra_skip = max(extra_skip, int(fps * 5))

        # Session window finished
        cursor.execute("UPDATE exams SET status = 'complete' WHERE id = %s", (current_exam_id,))
        conn.commit()
        session_reports.append({
            "session_index": session_index,
            "exam_id": current_exam_id,
            "captured": {s: dict(qs) for s, qs in captured.items()}
        })
        total_in_sess = sum(len(v) for v in captured.values())
        emit_log(f"  Session {session_index} complete: {total_in_sess} questions captured.")

    # ── AUTO MODE: scan whole video (runs only when no manual sessions given) ──
    consecutive_read_failures = 0
    while not manual_sessions:
        if check_stop and check_stop():
            emit_log("Process aborted by user during OCR scanning.")
            break

        try:
            # Skip to next check point (plus any post-capture bonus skip)
            current_skip = skip_frames + extra_skip
            extra_skip = 0
            for _ in range(current_skip - 1):
                cap.grab()
                frame_number += 1

            if DEBUG_MODE: emit_log("  [Debug] Reading frame...")
            ret, frame = cap.read()
            if not ret or frame is None:
                consecutive_read_failures += 1
                if consecutive_read_failures >= 5:
                    emit_log("End of video stream (or repeated read failure).")
                    break
                frame_number += skip_frames
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                continue
            consecutive_read_failures = 0
            frame_number += 1
            checks_done += 1
        except Exception as e:
            emit_log(f"  Read error at frame {frame_number}: {type(e).__name__}: {e}")
            break

        # Validate: must be a quiz screen
        if not has_calculator_button(frame, height, width):
            calc_button_misses += 1
            if calc_button_misses in (50, 200, 1000) or (calc_button_misses > 0 and calc_button_misses % 2000 == 0):
                elapsed_vid_sec = frame_number / fps
                emit_log(
                    f"  No quiz UI detected for {calc_button_misses} consecutive checks "
                    f"(video {elapsed_vid_sec/60:.1f}min)."
                )
            if DEBUG_MODE and checks_done <= 5:
                # Log the first few failures so we can calibrate
                search_h = int(height * 0.20)
                search_w = int(width * 0.70)
                top_bar = frame[0:search_h, 0:search_w]
                hsv = cv2.cvtColor(top_bar, cv2.COLOR_BGR2HSV)
                green_mask = cv2.inRange(hsv, CALC_GREEN_LOWER_HSV, CALC_GREEN_UPPER_HSV)
                ratio = cv2.countNonZero(green_mask) / (search_h * search_w)
                emit_log(f"  [Debug] Calculator check FAILED (green ratio: {ratio:.5f}, need: {CALC_MIN_GREEN_RATIO})")
                # Save the first 3 failed frames so we can see what the video looks like
                if checks_done <= 3:
                    fail_path = os.path.join(OUTPUT_DIR, f"_debug_failed_frame_{checks_done}.png")
                    cv2.imwrite(fail_path, frame)
                    emit_log(f"  [Debug] Saved failed frame to {fail_path}")
            continue

        # Reset miss counter — we have a valid quiz frame again.
        if calc_button_misses > 0:
            if calc_button_misses >= 50:
                emit_log(f"  Quiz UI re-acquired after {calc_button_misses} misses.")
            calc_button_misses = 0

        # Dynamically detect workspace top; re-derive every ~200 checks in case UI shifts.
        if cached_ws_top is None or checks_done % 200 == 0:
            bar_bottom = find_green_bar_bottom(frame, height, width)
            new_top = bar_bottom if bar_bottom is not None else ws_top_fallback
            if cached_ws_top is None:
                emit_log(f"  Workspace top calibrated: y={new_top} (frame {frame_number}).")
            cached_ws_top = new_top

        # Crop workspace using dynamic top
        workspace = frame[cached_ws_top:ws_bottom, ws_left:ws_right]
        if workspace.size == 0:
            continue

        # Fast pre-filter: skip the expensive EasyOCR call when the workspace
        # looks identical to the last decoded frame (same question still on screen).
        ws_copy = workspace.copy()
        if not workspace_changed(ws_copy, prev_workspace):
            prev_workspace = ws_copy
            continue
        prev_workspace = ws_copy

        # OCR: get subject + question number
        if DEBUG_MODE: emit_log("  [Debug] Extracting question info...")
        extract_question_info.checks_done = checks_done
        subject, q_num = extract_question_info(reader, workspace, emit_log=emit_log)
        if DEBUG_MODE: emit_log(f"  [Debug] OCR Result: {subject}, {q_num}")

        if subject and q_num:
            # Defensive: refuse subjects not registered in captured/SUBJECTS.
            if subject not in captured:
                emit_log(f"  Warning: OCR returned unmapped subject '{subject}', skipping.")
                continue

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

                # Reset diff state for the new session
                prev_workspace = None
                extra_skip = 0

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
            try:
                cv2.imwrite(filename, workspace)
            except Exception as e:
                emit_log(f"  Write Error ({image_basename}): {e}")
                continue
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

            # Jump ahead after a capture — next question won't appear for at least POST_CAPTURE_SKIP_SEC
            extra_skip = int(fps * POST_CAPTURE_SKIP_SEC)
            prev_workspace = None  # force OCR on the first frame after the skip
        else:
            # Save unidentified frames immediately for later human review.
            ocr_failures += 1
            unk_dir = os.path.join(OUTPUT_DIR, f"exam_{current_exam_id}", "_unidentified")
            os.makedirs(unk_dir, exist_ok=True)
            image_name = f"frame_{frame_number}.jpg"
            image_rel = f"exam_{current_exam_id}/_unidentified/{image_name}"
            try:
                cv2.imwrite(os.path.join(unk_dir, image_name), workspace)
            except Exception:
                pass
            elapsed_sec = frame_number / fps
            ts_str = f"{int(elapsed_sec // 60):02d}:{int(elapsed_sec % 60):02d}"
            raw_text = getattr(extract_question_info, "last_text", "")
            _record_unidentified(cursor, conn, current_exam_id, frame_number,
                                 image_rel, ts_str, raw_text, subject, q_num)
            if ocr_failures <= 20 or ocr_failures % 50 == 0:
                emit_log(f"  OCR partial (subj={subject}, q={q_num}) -> _unidentified")

        # Progress checkpoint every 10 checks (~100s of video).
        if checks_done % 10 == 0:
            elapsed_vid_sec = frame_number / fps
            elapsed_vid_min = elapsed_vid_sec / 60
            total_captured = sum(len(v) for v in captured.values())
            save_state(frame_number, True)
            emit_log(f"  ... checked {checks_done} points | video time: {elapsed_vid_min:.1f}min | session {session_index} captured: {total_captured}")

    if not manual_sessions:
        # Mark final exam as complete (auto mode only)
        cursor.execute("UPDATE exams SET status = 'complete' WHERE id = %s", (current_exam_id,))
        conn.commit()
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
