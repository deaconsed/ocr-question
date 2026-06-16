"""
migrate.py — One-command migration of chosen exams (sessions) between installs.

Moves the database rows AND the question images for the exams you pick, packed
into a single .zip you copy to the other machine. Import is ADDITIVE: it merges
the exams into whatever is already on the target, remapping IDs so nothing is
overwritten. Safe to run repeatedly / continuously.

────────────────────────────────────────────────────────────────────────────
EXPORT  (run on the machine that HAS the data)

    python migrate.py export                 # lists exams, asks which to migrate
    python migrate.py export 2 7 8           # migrate exam ids 2, 7, 8
    python migrate.py export --all           # migrate every exam

  → produces  migration_bundle_<timestamp>.zip  (contains the data + images)

IMPORT  (run on the target machine, after `git pull` + `.env` is set up)

    python migrate.py import migration_bundle_20260616_101500.zip

  The exams are ADDED to the target (new IDs assigned). Subjects and users are
  matched by name/username and reused if they already exist. An exam that is
  already present (same video file + session) is skipped, so re-running an
  import never creates duplicates.
────────────────────────────────────────────────────────────────────────────
"""
import os
import re
import sys
import json
import zipfile
import tempfile
import shutil
from datetime import datetime, date
from decimal import Decimal

from db import get_db_connection

FRAMES_DIR = "extracted_frames"
DATA_NAME = "data.json"
MANIFEST_NAME = "manifest.txt"

# Tables carried in the bundle (order is informational only).
EXPORT_TABLES = ("subjects", "users", "exams", "questions", "unidentified_frames")


# ─────────────────────────── shared helpers ───────────────────────────

def _connect():
    conn = get_db_connection()
    if not conn:
        sys.exit("ERROR: could not connect to the database. Check your .env settings.")
    return conn


def _json_default(o):
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    if isinstance(o, datetime):
        return o.isoformat(sep=" ")
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def _remap_exam_path(path, old, new):
    """Rewrite an `exam_<old>/...` relative path to `exam_<new>/...`."""
    if not path:
        return path
    return re.sub(rf"exam_{old}(?=[/\\]|$)", f"exam_{new}", path)


# ─────────────────────────────── export ───────────────────────────────

def _list_exams(cursor):
    cursor.execute("""
        SELECT e.id, e.label, e.year, e.video_filename, e.session_index, e.status,
               (SELECT COUNT(*) FROM questions q WHERE q.exam_id = e.id) AS q_count
        FROM exams e ORDER BY e.id
    """)
    return cursor.fetchall()


def export(args):
    conn = _connect()
    cursor = conn.cursor(dictionary=True)

    exams = _list_exams(cursor)
    if not exams:
        sys.exit("No exams found in the database — nothing to migrate.")

    print("\nAvailable exams / sessions:")
    print(f"  {'ID':>4}  {'Year':>5}  {'Qs':>4}  Label")
    print("  " + "-" * 60)
    for e in exams:
        shown = e["label"] or f"{e['video_filename']} (session {e['session_index']})"
        print(f"  {e['id']:>4}  {str(e['year'] or ''):>5}  {e['q_count']:>4}  {shown}  [{e['status']}]")
    print()

    valid_ids = {e["id"] for e in exams}

    if "--all" in args:
        exam_ids = sorted(valid_ids)
    elif args:
        exam_ids = _parse_ids(args, valid_ids)
    else:
        raw = input("Enter exam IDs to migrate (space/comma separated), or 'all': ").strip()
        if raw.lower() in ("all", "--all", "*"):
            exam_ids = sorted(valid_ids)
        else:
            exam_ids = _parse_ids(raw.replace(",", " ").split(), valid_ids)

    if not exam_ids:
        sys.exit("No valid exam IDs selected. Aborting.")

    id_list = ", ".join(str(i) for i in exam_ids)
    print(f"\nMigrating exams: {id_list}")

    def fetch(table, where=""):
        cursor.execute(f"SELECT * FROM `{table}` {where}")
        return cursor.fetchall()

    data = {
        "subjects": fetch("subjects"),
        "users": fetch("users"),
        "exams": fetch("exams", f"WHERE id IN ({id_list})"),
        "questions": fetch("questions", f"WHERE exam_id IN ({id_list})"),
        "unidentified_frames": fetch("unidentified_frames", f"WHERE exam_id IN ({id_list})"),
    }
    cursor.close()
    conn.close()

    # Pack data + image folders into a single zip.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle = f"migration_bundle_{ts}.zip"
    img_counts, missing = {}, []

    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(DATA_NAME, json.dumps(data, default=_json_default, ensure_ascii=False))
        for eid in exam_ids:
            folder = os.path.join(FRAMES_DIR, f"exam_{eid}")
            if not os.path.isdir(folder):
                missing.append(eid)
                continue
            n = 0
            for root, _, files in os.walk(folder):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.join("images", os.path.relpath(full, FRAMES_DIR))
                    zf.write(full, arc)
                    n += 1
            img_counts[eid] = n
        manifest = [
            "OCR Extractor migration bundle",
            f"Generated: {datetime.now().isoformat()}",
            f"Exams: {id_list}",
            f"Counts: " + ", ".join(f"{t}={len(data[t])}" for t in EXPORT_TABLES),
            "",
            "Images per exam:",
        ] + [f"  exam_{eid}: {cnt} files" for eid, cnt in img_counts.items()]
        zf.writestr(MANIFEST_NAME, "\n".join(manifest))

    print("\n" + "=" * 60)
    print(f"  BUNDLE READY:  {bundle}")
    print(f"  Exams: {len(exam_ids)}  |  questions: {len(data['questions'])}"
          f"  |  images: {sum(img_counts.values())} files")
    if missing:
        print(f"  NOTE: no image folder on disk for exam(s) {missing} "
              f"(DB rows still included).")
    print("=" * 60)
    print("\nNext: copy this .zip to the other machine and run:")
    print(f"    python migrate.py import {bundle}")


def _parse_ids(tokens, valid_ids):
    out = []
    for tok in tokens:
        if tok.startswith("--"):
            continue
        try:
            i = int(tok)
        except ValueError:
            print(f"  Skipping non-numeric id: {tok!r}")
            continue
        if i not in valid_ids:
            print(f"  Skipping unknown exam id: {i}")
            continue
        if i not in out:
            out.append(i)
    return out


# ─────────────────────────────── import ───────────────────────────────

def _insert(cursor, table, row, overrides):
    """Insert `row` (a dict) minus its `id`, applying column `overrides`."""
    payload = {k: v for k, v in row.items() if k != "id"}
    payload.update(overrides)
    cols = list(payload.keys())
    collist = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    cursor.execute(f"INSERT INTO `{table}` ({collist}) VALUES ({placeholders})",
                   [payload[c] for c in cols])
    return cursor.lastrowid


def do_import(args):
    if not args:
        sys.exit("Usage: python migrate.py import <bundle.zip>")
    bundle = args[0]
    if not os.path.isfile(bundle):
        sys.exit(f"ERROR: bundle not found: {bundle}")

    # Make sure the schema exists first (creates tables + default admin if new).
    print("Ensuring database schema (init_db)...")
    try:
        import init_db
        init_db.init_db()
    except Exception as e:
        print(f"  WARNING: init_db step failed ({e}). Continuing — "
              "tables may already exist.")

    tmp = tempfile.mkdtemp(prefix="ocr_migrate_")
    try:
        with zipfile.ZipFile(bundle, "r") as zf:
            zf.extractall(tmp)
            try:
                print("\n--- bundle manifest ---")
                print(zf.read(MANIFEST_NAME).decode("utf-8"))
                print("------------------------\n")
            except KeyError:
                pass

        data_path = os.path.join(tmp, DATA_NAME)
        if not os.path.isfile(data_path):
            sys.exit(f"ERROR: bundle is missing {DATA_NAME}.")
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        conn = _connect()
        cursor = conn.cursor()
        try:
            exam_map, stats = _merge(cursor, data)
            conn.commit()
        except Exception:
            conn.rollback()
            cursor.close()
            conn.close()
            raise
        cursor.close()
        conn.close()

        # Drop images into extracted_frames/, renaming exam_<old> -> exam_<new>.
        img_src = os.path.join(tmp, "images")
        copied = 0
        if os.path.isdir(img_src):
            os.makedirs(FRAMES_DIR, exist_ok=True)
            for old_id, new_id in exam_map.items():
                src = os.path.join(img_src, f"exam_{old_id}")
                if not os.path.isdir(src):
                    continue
                dst = os.path.join(FRAMES_DIR, f"exam_{new_id}")
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                copied += 1

        print("\n" + "=" * 60)
        print("  IMPORT COMPLETE")
        print(f"  Exams added       : {stats['exams_added']}")
        print(f"  Exams skipped     : {stats['exams_skipped']} (already present)")
        print(f"  Questions added   : {stats['questions_added']}")
        print(f"  Unidentified added: {stats['uf_added']}")
        print(f"  Subjects reused/new: {stats['subjects_reused']}/{stats['subjects_new']}")
        print(f"  Users reused/new   : {stats['users_reused']}/{stats['users_new']}")
        print(f"  Image folders copied: {copied}")
        print("=" * 60)
        print("Start the app and verify the exams.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _merge(cursor, data):
    """Additive merge with ID remapping. Returns (exam_map, stats)."""
    stats = dict(subjects_reused=0, subjects_new=0, users_reused=0, users_new=0,
                 exams_added=0, exams_skipped=0, questions_added=0, uf_added=0)

    # Subjects — match by unique name.
    subject_map = {}
    for s in data.get("subjects", []):
        cursor.execute("SELECT id FROM subjects WHERE name = %s", (s["name"],))
        row = cursor.fetchone()
        if row:
            subject_map[s["id"]] = row[0]
            stats["subjects_reused"] += 1
        else:
            subject_map[s["id"]] = _insert(cursor, "subjects", s, {})
            stats["subjects_new"] += 1

    # Users — match by unique username (existing accounts keep their own password).
    user_map = {}
    for u in data.get("users", []):
        cursor.execute("SELECT id FROM users WHERE username = %s", (u["username"],))
        row = cursor.fetchone()
        if row:
            user_map[u["id"]] = row[0]
            stats["users_reused"] += 1
        else:
            user_map[u["id"]] = _insert(cursor, "users", u, {})
            stats["users_new"] += 1

    def map_user(v):
        return user_map.get(v) if v is not None else None

    # Exams — skip if (video_filename, session_index) already present.
    exam_map = {}
    for e in data.get("exams", []):
        cursor.execute(
            "SELECT id FROM exams WHERE video_filename = %s AND session_index = %s",
            (e["video_filename"], e["session_index"]))
        row = cursor.fetchone()
        if row:
            stats["exams_skipped"] += 1
            continue
        exam_map[e["id"]] = _insert(cursor, "exams", e, {})
        stats["exams_added"] += 1

    # Questions — only for newly added exams.
    for q in data.get("questions", []):
        if q["exam_id"] not in exam_map:
            continue
        _insert(cursor, "questions", q, {
            "exam_id": exam_map[q["exam_id"]],
            "subject_id": subject_map.get(q["subject_id"], q["subject_id"]),
            "verified_by": map_user(q.get("verified_by")),
            "completed_by": map_user(q.get("completed_by")),
            "locked_by": map_user(q.get("locked_by")),
        })
        stats["questions_added"] += 1

    # Unidentified frames — only for newly added exams; rewrite image_path.
    for uf in data.get("unidentified_frames", []):
        if uf["exam_id"] not in exam_map:
            continue
        new_exam = exam_map[uf["exam_id"]]
        _insert(cursor, "unidentified_frames", uf, {
            "exam_id": new_exam,
            "resolved_by": map_user(uf.get("resolved_by")),
            "image_path": _remap_exam_path(uf.get("image_path"), uf["exam_id"], new_exam),
        })
        stats["uf_added"] += 1

    return exam_map, stats


# ─────────────────────────────── entry ────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)
    mode, rest = sys.argv[1].lower(), sys.argv[2:]
    if mode == "export":
        export(rest)
    elif mode == "import":
        do_import(rest)
    else:
        print(__doc__)
        sys.exit(f"Unknown mode: {mode!r} (use 'export' or 'import').")


if __name__ == "__main__":
    main()
