@echo off
REM CBT OCR Pipeline - System Reset Script (Windows)
REM This script will wipe all data and reset the system to its initial state.

echo.
echo WARNING: This will delete all extracted frames, reset the database, and clear all progress.
echo.
set /p confirm="Are you sure you want to proceed? (y/N): "

if /i NOT "%confirm%"=="y" (
    echo Reset aborted.
    exit /b 1
)

echo.
echo --- Resetting Database... ---
py -c "from dotenv import load_dotenv; load_dotenv(); import os, mysql.connector; conn = mysql.connector.connect(host=os.getenv('DB_HOST','localhost'), user=os.getenv('DB_USER','root'), password=os.getenv('DB_PASSWORD','')); c = conn.cursor(); db = os.getenv('DB_NAME','ocr_extractor'); c.execute(f'DROP DATABASE IF EXISTS {db}'); c.execute(f'CREATE DATABASE {db}'); conn.commit(); print(f'Database [{db}] dropped and recreated.'); conn.close()"

REM Re-run initialization script
py init_db.py

echo.
echo --- Cleaning Filesystem... ---

REM Remove extracted frames
if exist extracted_frames (
    rd /s /q extracted_frames
    mkdir extracted_frames
    echo Deleted extracted_frames content.
) else (
    echo extracted_frames folder not found, skipping.
)

REM Remove session state
if exist extraction_state.json (
    del /f extraction_state.json
    echo Deleted extraction_state.json.
) else (
    echo extraction_state.json not found, skipping.
)

echo.
echo --- Reset Complete! ---
echo You can now start the application with 'py app.py'.
echo Default login: admin / password
echo.
pause
