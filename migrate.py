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
    python migrate.py export --new           # only exams not migrated before
    python migrate.py export --all           # migrate every exam

  → produces  migration_bundle_<timestamp>.zip  (contains the data + images)

  After a successful export each chosen exam is stamped `migrated_at`, so next
  time `--new` (or the listing) shows you exactly what is still outstanding.

OTHER

    python migrate.py list                   # show exams + their migrated status
    python migrate.py unmark 2 7 | --all     # clear the migrated flag to re-send

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
EXPORT_TABLES = ("subjects", "users", "exams", "questions", "unidentified_frames",
                 "verifier_assignments", "verifier_exams")


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


def _canonical_subject_folder(name):
    """The exact transform app.py uses to build an image path from a subject."""
    return name.lower().replace(" ", "_")


def _normalize_subject_folders(exam_dir):
    """Rename subject folders inside `exam_dir` to the canonical form.

    app.py serves images from `subject.lower().replace(" ", "_")`, so a folder
    named BIOLOGY or "USE OF ENGLISH" 404s on the exam-scoped route and makes
    the UI fall back to shared, session-agnostic images. Bundles exported from
    an install with such folders would carry the problem across, so normalise
    on the way in. Returns the number of folders fixed.
    """
    if not os.path.isdir(exam_dir):
        return 0

    fixed = 0
    for sub in sorted(os.listdir(exam_dir)):
        src = os.path.join(exam_dir, sub)
        if not os.path.isdir(src):
            continue

        target = _canonical_subject_folder(sub)
        if sub == target:
            continue

        dst = os.path.join(exam_dir, target)
        try:
            # On a case-insensitive filesystem `dst` "exists" because it is the
            # same directory; a plain rename still corrects the stored name.
            if os.path.exists(dst) and not os.path.samefile(src, dst):
                for fn in os.listdir(src):
                    s, d = os.path.join(src, fn), os.path.join(dst, fn)
                    if os.path.exists(d):
                        print(f"    [collision] {target}/{fn} exists — left in {sub}/")
                        continue
                    os.rename(s, d)
                if not os.listdir(src):
                    os.rmdir(src)
            else:
                os.rename(src, dst)
            fixed += 1
        except OSError as e:
            print(f"    [WARN] could not normalise {sub} -> {target} ({e})")

    return fixed


# ─────────────────────────────── export ───────────────────────────────

def _ensure_migrated_column(cursor, conn):
    """Add exams.migrated_at if an older schema is missing it."""
    try:
        cursor.execute("SELECT migrated_at FROM exams LIMIT 1")
        cursor.fetchall()
    except Exception:
        try:
            cursor.execute("ALTER TABLE exams ADD COLUMN migrated_at TIMESTAMP NULL")
            conn.commit()
        except Exception:
            pass


def _list_exams(cursor):
    cursor.execute("""
        SELECT e.id, e.label, e.year, e.video_filename, e.session_index, e.status,
               e.migrated_at,
               (SELECT COUNT(*) FROM questions q WHERE q.exam_id = e.id) AS q_count
        FROM exams e ORDER BY e.id
    """)
    return cursor.fetchall()


def _print_exam_table(exams):
    print(f"  {'ID':>4}  {'Year':>5}  {'Qs':>4}  {'Migrated':>10}  Label")
    print("  " + "-" * 70)
    for e in exams:
        shown = e["label"] or f"{e['video_filename']} (session {e['session_index']})"
        mig = e["migrated_at"]
        mig_str = str(mig)[:10] if mig else "-"
        print(f"  {e['id']:>4}  {str(e['year'] or ''):>5}  {e['q_count']:>4}  "
              f"{mig_str:>10}  {shown}  [{e['status']}]")


def export(args):
    conn = _connect()
    _ensure_migrated_column(conn.cursor(), conn)
    cursor = conn.cursor(dictionary=True)

    exams = _list_exams(cursor)
    if not exams:
        sys.exit("No exams found in the database — nothing to migrate.")

    print("\nAvailable exams / sessions:")
    _print_exam_table(exams)
    print()

    valid_ids = {e["id"] for e in exams}
    new_ids = sorted(e["id"] for e in exams if not e["migrated_at"])

    if "--all" in args:
        exam_ids = sorted(valid_ids)
    elif "--new" in args:
        exam_ids = new_ids
    elif args:
        exam_ids = _parse_ids(args, valid_ids)
    else:
        raw = input("Enter exam IDs to migrate (space/comma separated), "
                    "'new' for un-migrated, or 'all': ").strip()
        low = raw.lower()
        if low in ("all", "--all", "*"):
            exam_ids = sorted(valid_ids)
        elif low in ("new", "--new"):
            exam_ids = new_ids
        else:
            exam_ids = _parse_ids(raw.replace(",", " ").split(), valid_ids)

    if not exam_ids:
        sys.exit("No valid exam IDs selected. Aborting.")

    id_list = ", ".join(str(i) for i in exam_ids)
    print(f"\nMigrating exams: {id_list}")

    def fetch(table, where=""):
        cursor.execute(f"SELECT * FROM `{table}` {where}")
        return cursor.fetchall()

    def fetch_optional(table, where=""):
        """Like fetch, but tolerates an older source schema without the table."""
        try:
            return fetch(table, where)
        except Exception:
            print(f"  NOTE: table '{table}' not present here — skipping it.")
            return []

    data = {
        "subjects": fetch("subjects"),
        "users": fetch("users"),
        "exams": fetch("exams", f"WHERE id IN ({id_list})"),
        "questions": fetch("questions", f"WHERE exam_id IN ({id_list})"),
        "unidentified_frames": fetch("unidentified_frames", f"WHERE exam_id IN ({id_list})"),
        # Verifier assignments are scoped to an exam, so they travel with it.
        "verifier_assignments": fetch_optional("verifier_assignments", f"WHERE exam_id IN ({id_list})"),
        "verifier_exams": fetch_optional("verifier_exams", f"WHERE exam_id IN ({id_list})"),
    }

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

    # Bundle written successfully — stamp the exams as migrated on this machine.
    cursor.execute(
        f"UPDATE exams SET migrated_at = NOW() WHERE id IN ({id_list})")
    conn.commit()
    cursor.close()
    conn.close()

    print("\n" + "=" * 60)
    print(f"  BUNDLE READY:  {bundle}")
    print(f"  Exams: {len(exam_ids)}  |  questions: {len(data['questions'])}"
          f"  |  images: {sum(img_counts.values())} files")
    print(f"  Marked migrated_at on {len(exam_ids)} exam(s).")
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
        copied = normalised = 0
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
                # The bundle preserves whatever folder names the source used;
                # make them match what app.py actually serves from.
                normalised += _normalize_subject_folders(dst)

        print("\n" + "=" * 60)
        print("  IMPORT COMPLETE")
        print(f"  Exams added       : {stats['exams_added']}")
        print(f"  Exams skipped     : {stats['exams_skipped']} (already present)")
        print(f"  Questions added   : {stats['questions_added']}")
        print(f"  Unidentified added: {stats['uf_added']}")
        print(f"  Assignments added : {stats['assignments_added']}")
        print(f"  Subjects reused/new: {stats['subjects_reused']}/{stats['subjects_new']}")
        print(f"  Users reused/new   : {stats['users_reused']}/{stats['users_new']}")
        print(f"  Image folders copied: {copied}")
        print(f"  Subject folders normalised: {normalised}")
        print("=" * 60)
        print("Start the app and verify the exams.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _merge(cursor, data):
    """Additive merge with ID remapping. Returns (exam_map, stats)."""
    stats = dict(subjects_reused=0, subjects_new=0, users_reused=0, users_new=0,
                 exams_added=0, exams_skipped=0, questions_added=0, uf_added=0,
                 assignments_added=0)

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
        # migrated_at is a per-machine flag; the freshly-imported copy hasn't
        # been migrated onward, so it starts NULL on the target.
        exam_map[e["id"]] = _insert(cursor, "exams", e, {"migrated_at": None})
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

    # Verifier assignments — only for newly added exams. Older bundles won't
    # carry these keys, hence the .get() defaults.
    for va in data.get("verifier_assignments", []):
        if va["exam_id"] not in exam_map:
            continue
        uid = user_map.get(va["user_id"])
        sid = subject_map.get(va["subject_id"])
        if uid is None or sid is None:
            continue  # user/subject didn't come across; skip rather than break the FK
        _insert(cursor, "verifier_assignments", va, {
            "user_id": uid,
            "exam_id": exam_map[va["exam_id"]],
            "subject_id": sid,
        })
        stats["assignments_added"] += 1

    for ve in data.get("verifier_exams", []):
        if ve["exam_id"] not in exam_map:
            continue
        uid = user_map.get(ve["user_id"])
        if uid is None:
            continue
        _insert(cursor, "verifier_exams", ve, {
            "user_id": uid,
            "exam_id": exam_map[ve["exam_id"]],
        })
        stats["assignments_added"] += 1

    return exam_map, stats


# ──────────────────────────── list / unmark ───────────────────────────

def cmd_list(args):
    conn = _connect()
    _ensure_migrated_column(conn.cursor(), conn)
    cursor = conn.cursor(dictionary=True)
    exams = _list_exams(cursor)
    cursor.close()
    conn.close()
    if not exams:
        print("No exams in the database.")
        return
    pending = [e for e in exams if not e["migrated_at"]]
    print("\nExams / sessions:")
    _print_exam_table(exams)
    print(f"\n  {len(exams)} total | {len(pending)} not yet migrated"
          f"{' (' + ', '.join(str(e['id']) for e in pending) + ')' if pending else ''}")


def unmark(args):
    conn = _connect()
    _ensure_migrated_column(conn.cursor(), conn)
    cursor = conn.cursor()
    if "--all" in args:
        cursor.execute("UPDATE exams SET migrated_at = NULL")
    else:
        try:
            ids = [int(a) for a in args if not a.startswith("--")]
        except ValueError:
            sys.exit("Usage: python migrate.py unmark <id> [<id> ...] | --all")
        if not ids:
            sys.exit("Usage: python migrate.py unmark <id> [<id> ...] | --all")
        id_list = ", ".join(str(i) for i in ids)
        cursor.execute(f"UPDATE exams SET migrated_at = NULL WHERE id IN ({id_list})")
    n = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Cleared migrated flag on {n} exam(s). They'll show up under `export --new` again.")


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
    elif mode == "list":
        cmd_list(rest)
    elif mode == "unmark":
        unmark(rest)
    else:
        print(__doc__)
        sys.exit(f"Unknown mode: {mode!r} (use export / import / list / unmark).")


if __name__ == "__main__":
    main()
