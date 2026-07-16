#!/usr/bin/env python3
"""
rename_question_images.py

Normalise the on-disk layout of extracted_frames/ so it matches exactly
what the Flask app requests, for one or more exam sessions you choose.

Two fixes, in this order, per selected exam_<id> folder:

  1. SUBJECT FOLDER NAMES -> canonical form
       The app builds the path with:  subject.lower().replace(" ", "_")
       (see app.py serve_image / serve_image_exam), so a folder must be
       lower-case with underscores instead of spaces, e.g.:
           BIOLOGY          -> biology
           USE OF ENGLISH   -> use_of_english
       If a folder is not in that form, the app 404s on the exam-scoped
       URL and silently falls back to a shared, session-agnostic folder,
       which makes different sessions show the SAME images.

  2. IMAGE FILE NAMES -> question_<n>.<ext>
       Bare-number frames (1.png, 2.png, ...) are renamed to
       question_1.png, question_2.png, ... which is what the DB's
       image_name column and the app expect.

Run it on the server, from anywhere:
    python3 rename_question_images.py

Safe by design:
  - shows a full dry-run preview; nothing changes until you type 'yes'
  - folders/files already in the correct form are left untouched
  - if a canonical target folder already exists (e.g. both BIOLOGY and
    biology are present), it MERGES the contents in and reports any
    file that would collide instead of overwriting it
  - never overwrites an existing question_<n> file (collision -> skipped)
"""

import os
import sys

# Root that holds exam_<id>/<subject>/... images (matches app.py INPUT_DIR).
INPUT_DIR = os.environ.get(
    "OCR_INPUT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "extracted_frames"),
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def canonical(subject_folder_name):
    """The exact transform the app uses to turn a subject into a folder name."""
    return subject_folder_name.lower().replace(" ", "_")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def list_exam_folders(root):
    if not os.path.isdir(root):
        print(f"[ERROR] Folder not found: {root}")
        print("        Set OCR_INPUT_DIR or run this from the project root.")
        sys.exit(1)
    return sorted(
        d for d in os.listdir(root)
        if d.startswith("exam_") and os.path.isdir(os.path.join(root, d))
    )


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
    seen = set()
    return [x for x in chosen if not (x in seen or seen.add(x))]


# ---------------------------------------------------------------------------
# Step 1: subject folder normalisation
# ---------------------------------------------------------------------------
def plan_folder_renames(root, exam_folders):
    """Return (renames, merges, skipped).
    renames = [(src_dir, dst_dir, label)]        simple rename
    merges  = [(src_dir, dst_dir, label)]        target exists -> merge contents
    skipped = [(path, reason)]
    """
    renames, merges, skipped = [], [], []

    for exam in exam_folders:
        exam_path = os.path.join(root, exam)
        for sub in sorted(os.listdir(exam_path)):
            src_dir = os.path.join(exam_path, sub)
            if not os.path.isdir(src_dir):
                continue

            target = canonical(sub)
            if sub == target:
                continue  # already correct

            dst_dir = os.path.join(exam_path, target)
            label = f"{exam}/{sub}  ->  {exam}/{target}"

            if os.path.exists(dst_dir):
                # A folder with the canonical name already exists -> merge.
                merges.append((src_dir, dst_dir, label))
            else:
                renames.append((src_dir, dst_dir, label))

    return renames, merges, skipped


def apply_folder_renames(renames, merges):
    done = errors = merged_files = collisions = 0

    for src, dst, label in renames:
        try:
            os.rename(src, dst)
            done += 1
        except OSError as e:
            errors += 1
            print(f"  [ERROR] {label}  ({e})")

    for src, dst, label in merges:
        print(f"  [merge] {label}")
        for fname in os.listdir(src):
            s = os.path.join(src, fname)
            d = os.path.join(dst, fname)
            if os.path.exists(d):
                collisions += 1
                print(f"      [collision] {fname} already in target -> left in {os.path.basename(src)}/")
                continue
            try:
                os.rename(s, d)
                merged_files += 1
            except OSError as e:
                errors += 1
                print(f"      [ERROR] moving {fname} ({e})")
        # Remove the source folder only if it is now empty.
        try:
            if not os.listdir(src):
                os.rmdir(src)
        except OSError:
            pass

    return done, merged_files, collisions, errors


# ---------------------------------------------------------------------------
# Step 2: image file normalisation (bare number -> question_<n>)
# ---------------------------------------------------------------------------
def plan_image_renames(root, exam_folders):
    """Runs AFTER folder renames, so it reads the canonical folder names."""
    renames, skipped = [], []

    for exam in exam_folders:
        exam_path = os.path.join(root, exam)
        for sub in sorted(os.listdir(exam_path)):
            subject_path = os.path.join(exam_path, sub)
            if not os.path.isdir(subject_path):
                continue

            for fname in sorted(os.listdir(subject_path)):
                stem, ext = os.path.splitext(fname)
                if ext.lower() not in IMAGE_EXTENSIONS:
                    continue
                if stem.startswith("question_"):
                    continue
                if not stem.isdigit():
                    skipped.append((f"{exam}/{sub}/{fname}", "not a plain number"))
                    continue

                q_num = int(stem)
                dst_name = f"question_{q_num}{ext.lower()}"
                src_path = os.path.join(subject_path, fname)
                dst_path = os.path.join(subject_path, dst_name)

                if os.path.exists(dst_path):
                    skipped.append((f"{exam}/{sub}/{fname}", f"{dst_name} already exists"))
                    continue

                renames.append((src_path, dst_path, f"{exam}/{sub}/{fname}  ->  {dst_name}"))

    return renames, skipped


def apply_image_renames(renames):
    done = errors = 0
    for src, dst, label in renames:
        try:
            os.rename(src, dst)
            done += 1
        except OSError as e:
            errors += 1
            print(f"  [ERROR] {label}  ({e})")
    return done, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 64)
    print(" Normalise extracted_frames: subject folders + image names")
    print("=" * 64)
    print(f"Image root: {INPUT_DIR}")

    exams = list_exam_folders(INPUT_DIR)
    if not exams:
        print("No exam_* folders found. Nothing to do.")
        return

    exam_folders = choose_exams(exams)
    if not exam_folders:
        print("No valid selection. Exiting.")
        return

    print(f"\nSelected: {', '.join(exam_folders)}")

    # ---- Plan step 1 (folders) ----
    folder_renames, folder_merges, _ = plan_folder_renames(INPUT_DIR, exam_folders)

    print("\n--- STEP 1: subject folder names (dry run) ---")
    if not folder_renames and not folder_merges:
        print("  All subject folders already in canonical form.")
    for _, _, label in folder_renames:
        print(f"  rename: {label}")
    for _, _, label in folder_merges:
        print(f"  MERGE : {label}   (target already exists)")

    # Plan step 2 as a *preview only* using current names; after folder
    # renames the paths change, so we re-plan step 2 for real afterwards.
    prev_img_renames, prev_img_skipped = plan_image_renames(INPUT_DIR, exam_folders)
    print("\n--- STEP 2: image file names (preview) ---")
    if not prev_img_renames:
        print("  No bare-number images found to rename (or already question_<n>).")
    for _, _, label in prev_img_renames[:200]:
        print(f"  rename: {label}")
    if len(prev_img_renames) > 200:
        print(f"  ... and {len(prev_img_renames) - 200} more")
    for path, reason in prev_img_skipped:
        print(f"  [skip] {path}  ({reason})")

    total = len(folder_renames) + len(folder_merges) + len(prev_img_renames)
    if total == 0:
        print("\nNothing to do. Everything already looks correct.")
        return

    confirm = input(
        f"\nApply changes to {', '.join(exam_folders)}? Type 'yes' to proceed: "
    ).strip().lower()
    if confirm != "yes":
        print("Aborted. No files changed.")
        return

    # ---- Apply step 1 ----
    print("\nApplying folder renames...")
    fdone, mfiles, coll, ferr = apply_folder_renames(folder_renames, folder_merges)

    # ---- Re-plan + apply step 2 (paths are canonical now) ----
    img_renames, _ = plan_image_renames(INPUT_DIR, exam_folders)
    print("Applying image renames...")
    idone, ierr = apply_image_renames(img_renames)

    print("\n" + "-" * 40)
    print(f"Folders renamed : {fdone}")
    print(f"Folders merged  : {len(folder_merges)}  ({mfiles} files moved, {coll} collisions)")
    print(f"Images renamed  : {idone}")
    print(f"Errors          : {ferr + ierr}")
    print("Done.")


if __name__ == "__main__":
    main()
