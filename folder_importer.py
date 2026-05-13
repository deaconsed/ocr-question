import os
import shutil
from db import get_db_connection

OUTPUT_DIR = "extracted_frames"

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

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def import_screenshot_folder(folder_path, year, emit_log, gpt_queue, check_stop=None):
    """
    Import a pre-organized screenshot folder into the database and queue GPT extraction.

    Expected folder structure:
        <folder_path>/
            <SUBJECT NAME>/
                1.png
                2.png
                ...

    The folder name is used as the exam's video_filename identifier.
    """
    folder_path = os.path.abspath(folder_path)
    folder_name = os.path.basename(folder_path)

    if not os.path.isdir(folder_path):
        emit_log(f"Error: Folder not found: {folder_path}")
        return

    emit_log(f"=== Starting Folder Import: {folder_name} ===")
    emit_log(f"  Year: {year}")

    conn = get_db_connection()
    if not conn:
        emit_log("Error: Could not connect to database.")
        return
    cursor = conn.cursor()

    # Sync subjects with DB
    subject_ids = {}
    for subj in SUBJECTS:
        cursor.execute("SELECT id FROM subjects WHERE name = %s", (subj,))
        row = cursor.fetchone()
        if row:
            subject_ids[subj] = row[0]
        else:
            cursor.execute("INSERT INTO subjects (name) VALUES (%s)", (subj,))
            conn.commit()
            cursor.execute("SELECT id FROM subjects WHERE name = %s", (subj,))
            subject_ids[subj] = cursor.fetchone()[0]

    # Create exam record (folder_name as video_filename, session_index=1)
    label = f"{year} - {folder_name}"
    cursor.execute("""
        INSERT INTO exams (video_filename, session_index, year, label, status)
        VALUES (%s, %s, %s, %s, 'in_progress')
        ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id), year=VALUES(year), label=VALUES(label)
    """, (folder_name, 1, year, label))
    conn.commit()
    cursor.execute("SELECT LAST_INSERT_ID()")
    exam_id = cursor.fetchone()[0]
    emit_log(f"  Exam record created/resumed (ID: {exam_id})")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total_imported = 0
    total_skipped = 0

    # Discover subject subfolders
    try:
        entries = os.listdir(folder_path)
    except PermissionError as e:
        emit_log(f"Error reading folder: {e}")
        cursor.close()
        conn.close()
        return

    subject_dirs = sorted([e for e in entries if os.path.isdir(os.path.join(folder_path, e))])

    if not subject_dirs:
        emit_log("Error: No subject subfolders found inside the folder.")
        cursor.close()
        conn.close()
        return

    for subject_dir in subject_dirs:
        if check_stop and check_stop():
            emit_log("Import aborted by user.")
            break

        subject_name = subject_dir.strip().upper()
        subject_path = os.path.join(folder_path, subject_dir)

        if subject_name not in SUBJECTS:
            emit_log(f"  [Skip] Unrecognized subject folder: '{subject_dir}'")
            continue

        subject_id = subject_ids[subject_name]
        subject_folder = subject_name.lower().replace(" ", "_")
        dest_dir = os.path.join(OUTPUT_DIR, f"exam_{exam_id}", subject_folder)
        os.makedirs(dest_dir, exist_ok=True)

        # Collect and sort image files by question number
        image_files = []
        for fname in os.listdir(subject_path):
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in IMAGE_EXTENSIONS:
                continue
            try:
                q_num = int(stem)
                image_files.append((q_num, fname))
            except ValueError:
                emit_log(f"  [Skip] Cannot parse question number from: {fname}")

        image_files.sort(key=lambda x: x[0])

        expected = SUBJECTS[subject_name]
        emit_log(f"\n  [{subject_name}] Found {len(image_files)}/{expected} images")

        subject_imported = 0

        for q_num, fname in image_files:
            if check_stop and check_stop():
                emit_log("Import aborted by user.")
                break

            src_path = os.path.join(subject_path, fname)
            ext = os.path.splitext(fname)[1].lower()
            dest_name = f"question_{q_num}{ext}"
            dest_path = os.path.join(dest_dir, dest_name)

            # Copy image
            shutil.copy2(src_path, dest_path)

            # Insert DB record (skip if already exists)
            try:
                cursor.execute("""
                    INSERT INTO questions (exam_id, subject_id, question_number, image_name)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
                """, (exam_id, subject_id, q_num, dest_name))
                conn.commit()

                q_id = cursor.lastrowid
                if not q_id:
                    cursor.execute("SELECT LAST_INSERT_ID()")
                    q_id = cursor.fetchone()[0]

                # Only queue for GPT if this is a new insert (lastrowid > 0 means insert happened)
                if gpt_queue and q_id:
                    # Check if question_text is already extracted
                    cursor.execute("SELECT question_text FROM questions WHERE id = %s", (q_id,))
                    row = cursor.fetchone()
                    if row and row[0] is None:
                        gpt_queue.put(q_id)

                subject_imported += 1
                total_imported += 1
                emit_log(f"    Q{q_num} -> Queued for extraction")

            except Exception as e:
                emit_log(f"    [DB Error] Q{q_num}: {e}")
                total_skipped += 1

        emit_log(f"  [{subject_name}] {subject_imported} questions imported")

    # Mark exam complete
    cursor.execute("UPDATE exams SET status = 'complete' WHERE id = %s", (exam_id,))
    conn.commit()
    cursor.close()
    conn.close()

    emit_log(f"\n{'='*55}")
    emit_log(f"  FOLDER IMPORT COMPLETE")
    emit_log(f"  Exam ID : {exam_id}")
    emit_log(f"  Imported: {total_imported} questions queued for GPT extraction")
    if total_skipped:
        emit_log(f"  Skipped : {total_skipped} errors")
    emit_log(f"{'='*55}")
