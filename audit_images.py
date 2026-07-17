#!/usr/bin/env python3
"""
audit_images.py — READ-ONLY report on where every question image actually lives.

Changes nothing. It answers one question: is the legacy image fallback in
app.py still load-bearing, or is it only ever masking bugs?

app.py serves images from
    extracted_frames/exam_<exam_id>/<subject.lower().replace(" ", "_")>/<file>
and, when that 404s, falls back to a shared, session-agnostic folder
    extracted_frames/<subject.lower().replace(" ", "_")>/<file>
and then to "scan every exam_* folder and return the first match" — which
serves ANOTHER SESSION'S image. This script finds every row relying on either.

Each image referenced by the DB is classified as:

  OK          found at the correct exam-scoped path — nothing to do
  LEGACY_ONLY only in the shared legacy folder (the legacy route is load-bearing)
  OTHER_EXAM  only under a DIFFERENT exam_* folder — currently served WRONG
  MISSING     not on disk anywhere

Usage:
    python3 audit_images.py            # summary + problem rows
    python3 audit_images.py --all      # also list every OK row
"""

import os
import sys
from collections import defaultdict

from db import get_db_connection
# Single source of truth for the transform (same one migrate.py/app.py use).
from migrate import _canonical_subject_folder as canonical

INPUT_DIR = os.environ.get(
    "OCR_INPUT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "extracted_frames"),
)


def _exam_folders(root):
    if not os.path.isdir(root):
        return []
    return sorted(
        d for d in os.listdir(root)
        if d.startswith("exam_") and os.path.isdir(os.path.join(root, d))
    )


def classify(exam_id, subject_name, filename, root, exam_dirs):
    """Return (bucket, detail) for one referenced image file."""
    sub = canonical(subject_name)

    exam_path = os.path.join(root, f"exam_{exam_id}", sub, filename)
    if os.path.isfile(exam_path):
        return "OK", ""

    legacy_path = os.path.join(root, sub, filename)
    if os.path.isfile(legacy_path):
        return "LEGACY_ONLY", os.path.relpath(legacy_path, root)

    # Exactly what app.py's cross-exam scan would land on.
    for d in exam_dirs:
        if d == f"exam_{exam_id}":
            continue
        p = os.path.join(root, d, sub, filename)
        if os.path.isfile(p):
            return "OTHER_EXAM", os.path.relpath(p, root)

    return "MISSING", os.path.relpath(exam_path, root)


def check_folder_naming(root, exam_dirs):
    """Exam folders still holding non-canonical subject folder names."""
    offenders = defaultdict(list)
    for d in exam_dirs:
        exam_path = os.path.join(root, d)
        for sub in sorted(os.listdir(exam_path)):
            if not os.path.isdir(os.path.join(exam_path, sub)):
                continue
            if sub != canonical(sub):
                offenders[d].append(sub)
    return offenders


def main():
    show_all = "--all" in sys.argv

    print("=" * 70)
    print(" Image audit (read-only — nothing will be changed)")
    print("=" * 70)
    print(f"Image root: {INPUT_DIR}")

    if not os.path.isdir(INPUT_DIR):
        sys.exit(f"ERROR: image root not found: {INPUT_DIR}")

    conn = get_db_connection()
    if not conn:
        sys.exit("ERROR: could not connect to the database. Check your .env settings.")

    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT q.id, q.exam_id, q.question_number,
               q.image_name, q.question_image, q.solution_image,
               s.name AS subject_name
        FROM questions q
        JOIN subjects s ON q.subject_id = s.id
        ORDER BY q.exam_id, s.name, q.question_number
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    exam_dirs = _exam_folders(INPUT_DIR)
    print(f"Exam folders on disk: {len(exam_dirs)}   |   question rows in DB: {len(rows)}\n")

    buckets = defaultdict(list)
    per_exam = defaultdict(lambda: defaultdict(int))

    for r in rows:
        # A row can reference up to three distinct files; check each.
        for col in ("image_name", "question_image", "solution_image"):
            fname = r.get(col)
            if not fname:
                continue
            bucket, detail = classify(
                r["exam_id"], r["subject_name"], fname, INPUT_DIR, exam_dirs
            )
            label = (f"exam_{r['exam_id']} / {r['subject_name']} / Q{r['question_number']} "
                     f"[{col}={fname}]")
            buckets[bucket].append((label, detail))
            per_exam[r["exam_id"]][bucket] += 1

    total = sum(len(v) for v in buckets.values())

    # ---- summary ----
    print("--- SUMMARY (per referenced file) ---")
    for b in ("OK", "LEGACY_ONLY", "OTHER_EXAM", "MISSING"):
        print(f"  {b:<12} {len(buckets[b]):>6}")
    print(f"  {'TOTAL':<12} {total:>6}")

    # ---- per exam ----
    print("\n--- PER EXAM ---")
    print(f"  {'exam':>8}  {'OK':>6} {'LEGACY':>7} {'OTHER':>6} {'MISS':>6}")
    print("  " + "-" * 40)
    for eid in sorted(per_exam):
        c = per_exam[eid]
        print(f"  {('exam_' + str(eid)):>8}  {c['OK']:>6} {c['LEGACY_ONLY']:>7} "
              f"{c['OTHER_EXAM']:>6} {c['MISSING']:>6}")

    # ---- problem detail ----
    for b, note in (
        ("OTHER_EXAM", "SERVED FROM THE WRONG SESSION right now"),
        ("LEGACY_ONLY", "only in the shared legacy folder — legacy route is load-bearing"),
        ("MISSING", "not on disk anywhere — image will be broken"),
    ):
        if buckets[b]:
            print(f"\n--- {b}  ({len(buckets[b])}) — {note} ---")
            for label, detail in buckets[b][:200]:
                print(f"  {label}\n      -> {detail}")
            if len(buckets[b]) > 200:
                print(f"  ... and {len(buckets[b]) - 200} more")

    if show_all and buckets["OK"]:
        print(f"\n--- OK ({len(buckets['OK'])}) ---")
        for label, _ in buckets["OK"]:
            print(f"  {label}")

    # ---- folder naming ----
    offenders = check_folder_naming(INPUT_DIR, exam_dirs)
    print("\n--- SUBJECT FOLDER NAMING ---")
    if not offenders:
        print("  All subject folders are already in canonical form.")
    else:
        print("  These exams still have non-canonical folders — run rename_question_images.py:")
        for d in sorted(offenders):
            print(f"    {d}: {', '.join(offenders[d])}")

    # ---- verdict ----
    print("\n" + "=" * 70)
    if buckets["OTHER_EXAM"]:
        print("  ACTION: OTHER_EXAM rows exist — those images are being served from the")
        print("          wrong session today. Fix folder naming / re-copy the images.")
    if not buckets["LEGACY_ONLY"]:
        print("  SAFE: nothing depends on the shared legacy folders. The legacy route")
        print("        can be dropped and extracted_frames/<subject>/ archived.")
    else:
        print(f"  KEEP: {len(buckets['LEGACY_ONLY'])} file(s) exist ONLY in the shared legacy")
        print("        folders. Do not remove the legacy route until those are relocated.")
    print("=" * 70)


if __name__ == "__main__":
    main()
