import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    """Establishes and returns a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(
            host=os.getenv("DB_HOST", "localhost"),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", ""),
            database=os.getenv("DB_NAME", "ocr_extractor")
        )
        return connection
    except mysql.connector.Error as e:
        print(f"Error connecting to MySQL Database: {e}")
        return None
