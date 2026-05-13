import mysql.connector
import os
from dotenv import load_dotenv

load_dotenv()

def init_db():
    try:
        # First connect without DB to ensure it exists
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST", "localhost"),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", "")
        )
        cursor = conn.cursor()
        
        db_name = os.getenv("DB_NAME", "ocr_extractor")
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_name}")
        cursor.execute(f"USE {db_name}")
        
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) DEFAULT 'verifier',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("Table 'users' ensured.")
        
        # Create exams table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exams (
                id INT AUTO_INCREMENT PRIMARY KEY,
                video_filename VARCHAR(255) NOT NULL,
                session_index INT NOT NULL DEFAULT 1,
                year INT NULL,
                label VARCHAR(255),
                status ENUM('in_progress','complete') DEFAULT 'in_progress',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY unique_session (video_filename, session_index)
            )
        """)
        print("Table 'exams' ensured.")
        
        # Create subjects table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) UNIQUE NOT NULL
            )
        """)
        print("Table 'subjects' ensured.")

        # Create questions table (with exam_id and role workflow columns)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                exam_id INT NOT NULL,
                subject_id INT NOT NULL,
                question_number INT NOT NULL,
                image_name VARCHAR(255) NOT NULL,
                video_timestamp VARCHAR(50),
                question_text TEXT,
                options JSON,
                has_image BOOLEAN DEFAULT FALSE,
                question_image VARCHAR(255),
                verified BOOLEAN DEFAULT FALSE,
                verified_by INT NULL,
                teacher_answer VARCHAR(10) NULL,
                ai_answer VARCHAR(10) NULL,
                solution_image VARCHAR(255) NULL,
                completed BOOLEAN DEFAULT FALSE,
                completed_by INT NULL,
                locked_by INT NULL,
                locked_at TIMESTAMP NULL,
                FOREIGN KEY (exam_id) REFERENCES exams(id),
                FOREIGN KEY (subject_id) REFERENCES subjects(id),
                FOREIGN KEY (verified_by) REFERENCES users(id),
                FOREIGN KEY (completed_by) REFERENCES users(id),
                FOREIGN KEY (locked_by) REFERENCES users(id),
                UNIQUE KEY unique_question (exam_id, subject_id, question_number)
            )
        """)
        print("Table 'questions' ensured.")

        # Create unidentified_frames table — holds OCR misses for later manual review
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
        print("Table 'unidentified_frames' ensured.")

        # ── Lazy migration for new workflow columns ──
        workflow_cols = [
            ("teacher_answer", "VARCHAR(10) NULL"),
            ("ai_answer", "VARCHAR(10) NULL"),
            ("solution_image", "VARCHAR(255) NULL"),
            ("completed", "BOOLEAN DEFAULT FALSE"),
            ("completed_by", "INT NULL REFERENCES users(id)")
        ]
        
        for col_name, col_type in workflow_cols:
            try:
                cursor.execute(f"SELECT {col_name} FROM questions LIMIT 1")
                cursor.fetchall()
            except mysql.connector.Error:
                print(f"Migrating: Adding {col_name} to questions...")
                cursor.execute(f"ALTER TABLE questions ADD COLUMN {col_name} {col_type}")
                conn.commit()

        # ── Lazy migration for year column ──
        try:
            cursor.execute("SELECT year FROM exams LIMIT 1")
            cursor.fetchall()
        except mysql.connector.Error:
            print("Migrating: Adding year column to exams...")
            cursor.execute("ALTER TABLE exams ADD COLUMN year INT NULL")
            cursor.execute("UPDATE exams SET year = 2024 WHERE year IS NULL")
            conn.commit()

        # ── Existing legacy migration (exam_id) ──
        try:
            cursor.execute("SELECT exam_id FROM questions LIMIT 1")
            cursor.fetchall()
        except mysql.connector.Error:
            print("Migrating old questions table -> adding exam_id...")
            cursor.execute("""
                INSERT IGNORE INTO exams (video_filename, session_index, label, status)
                VALUES ('legacy', 1, 'Legacy Import', 'complete')
            """)
            conn.commit()
            cursor.execute("SELECT id FROM exams WHERE video_filename = 'legacy' AND session_index = 1")
            default_exam_id = cursor.fetchone()[0]
            
            try:
                cursor.execute("ALTER TABLE questions ADD COLUMN exam_id INT NULL AFTER id")
            except mysql.connector.Error: pass
            cursor.execute("UPDATE questions SET exam_id = %s WHERE exam_id IS NULL", (default_exam_id,))
            try:
                cursor.execute("ALTER TABLE questions MODIFY COLUMN exam_id INT NOT NULL")
                cursor.execute("ALTER TABLE questions ADD FOREIGN KEY (exam_id) REFERENCES exams(id)")
            except mysql.connector.Error: pass
            try:
                cursor.execute("ALTER TABLE questions DROP INDEX unique_question")
            except mysql.connector.Error: pass
            try:
                cursor.execute("ALTER TABLE questions ADD UNIQUE KEY unique_question (exam_id, subject_id, question_number)")
            except mysql.connector.Error: pass
            conn.commit()

        from werkzeug.security import generate_password_hash
        # Create default admin
        cursor.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            default_pw = generate_password_hash("password")
            cursor.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', %s, 'admin')", (default_pw,))
            print("Default admin user created (admin / password)")
        
        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialization completed successfully.")
        
    except mysql.connector.Error as err:
        print(f"Error: {err}")

if __name__ == "__main__":
    init_db()
