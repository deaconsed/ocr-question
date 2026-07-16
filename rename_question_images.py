#!/usr/bin/env python3
"""
rename_question_images.py

Fix re-copied question frames whose filenames are bare numbers
(1.png, 2.png, ...) so they match the naming the app expects:
question_<n>.<ext>  (e.g. question_1.png).

The app serves/looks up images by 'question_<number>.<ext>'
(see folder_importer.py -> dest_name = f"question_{q_num}{ext}"),
so folders that only contain "1.png", "2.png" render as broken images.

Run it on the server, from anywhere:
    python rename_question_images.py

It will:
  1. list the exam_<id> folders under extracted_frames/
  2. let you choose one, several, or ALL of them
  3. walk every subject sub-folder
  4. rename bare-number images -> question_<n>.<ext>

Safe by design:
  - files already named 'question_*' are left untouched
  - files whose stem is not a plain integer are skipped (reported)
  - it will NOT overwrite an existing question_<n> file (collision -> skipped)
  - a dry-run preview is shown first; nothing changes until you type 'yes'
"""

import os
import sys

# Root folder that holds exam_<id>/<subject>/... images.
# Defaults to <this script's dir>/extracted_frames, matching app.py's INPUT_DIR.
INPUT_DIR = os.environ.get(
    "OCR_INPUT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "extracted_frames"),
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def list_exam_folders(root):
    if not os.path.isdir(root):
        print(f"[ERROR] Folder not found: {root}")
        print("        Set OCR_INPUT_DIR or run this from the project root.")
        sys.exit(1)
    exams = sorted(
        d for d in os.listdir(root)
        if d.startswith("exam_") and os.path.isdir(os.path.join(root, d))
    )
    return exams


def choose_exams(exams):
    print("\nAvailable exam folders:")
    for i, name in enumerate(exams, 1):
        print(f"  {i}. {name}")
    print("\nEnter the numbers to process (e.g. 1,3), or type 'all'.")
    raw = input("Choice: ").strip().lower()

    if raw in ("all", "*"):
        return exams

    chosen = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or not (1 <= int(part) <= len(exams)):
            print(f"[WARN] Ignoring invalid choice: {part!r}")
            continue
        chosen.append(exams[int(part) - 1])
    # de-dup, keep order
    seen = set()
    return [x for x in chosen if not (x in seen or seen.add(x))]


def plan_renames(root, exam_folders):
    """Return (renames, skipped) where renames is a list of
    (src_path, dst_path, label) tuples to perform."""
    renames = []
    skipped = []

    for exam in exam_folders:
        exam_path = os.path.join(root, exam)
        for subject in sorted(os.listdir(exam_path)):
            subject_path = os.path.join(exam_path, subject)
            if not os.path.isdir(subject_path):
                continue

            for fname in sorted(os.listdir(subject_path)):
                stem, ext = os.path.splitext(fname)
                if ext.lower() not in IMAGE_EXTENSIONS:
                    continue

                # Already correctly named -> leave it alone.
                if stem.startswith("question_"):
                    continue
                # Solutions and other named files -> leave alone.
                if not stem.isdigit():
                    skipped.append((f"{exam}/{subject}/{fname}", "not a plain number"))
                    continue

                q_num = int(stem)
                dst_name = f"question_{q_num}{ext.lower()}"
                src_path = os.path.join(subject_path, fname)
                dst_path = os.path.join(subject_path, dst_name)

                if os.path.exists(dst_path):
                    skipped.append(
                        (f"{exam}/{subject}/{fname}", f"{dst_name} already exists")
                    )
                    continue

                label = f"{exam}/{subject}/{fname}  ->  {dst_name}"
                renames.append((src_path, dst_path, label))

    return renames, skipped


def main():
    print("=" * 60)
    print(" Rename question images -> question_<n>.<ext>")
    print("=" * 60)
    print(f"Image root: {INPUT_DIR}")

    exams = list_exam_folders(INPUT_DIR)
    if not exams:
        print("No exam_* folders found. Nothing to do.")
        return

    exam_folders = choose_exams(exams)
    if not exam_folders:
        print("No valid selection. Exiting.")
        return

    renames, skipped = plan_renames(INPUT_DIR, exam_folders)

    print(f"\nSelected: {', '.join(exam_folders)}")
    print(f"Planned renames: {len(renames)}")
    print(f"Skipped: {len(skipped)}")

    if skipped:
        print("\n--- Skipped (unchanged) ---")
        for path, reason in skipped:
            print(f"  [skip] {path}  ({reason})")

    if not renames:
        print("\nNothing to rename.")
        return

    print("\n--- Preview (dry run) ---")
    for _, _, label in renames:
        print(f"  {label}")

    confirm = input(f"\nRename {len(renames)} file(s)? Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted. No files changed.")
        return

    done, errors = 0, 0
    for src, dst, label in renames:
        try:
            os.rename(src, dst)
            done += 1
        except OSError as e:
            errors += 1
            print(f"  [ERROR] {label}  ({e})")

    print(f"\nDone. Renamed {done} file(s), {errors} error(s).")


if __name__ == "__main__":
    main()
