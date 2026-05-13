#!/bin/bash

# CBT OCR Pipeline - System Reset Script (Linux)
# This script will wipe all data and reset the system to its initial state.

echo "⚠️  WARNING: This will delete all extracted frames, reset the database, and clear all progress."
read -p "Are you sure you want to proceed? (y/N): " confirm

if [[ $confirm != [yY] && $confirm != [yY][eE][sS] ]]; then
    echo "Reset aborted."
    exit 1
fi

# 1. Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo "Error: .env file not found. Please ensure you are in the project root."
    exit 1
fi

echo "--- 🛠️  Resetting Database... ---"
# Drop and recreate database
mysql -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASSWORD" -e "DROP DATABASE IF EXISTS $DB_NAME; CREATE DATABASE $DB_NAME;"

# Re-run initialization script
python3 init_db.py

echo "--- 📂 Cleaning Filesystem... ---"
# Remove extracted frames
rm -rf extracted_frames/*
echo "Deleted extracted_frames content."

# Remove session state
rm -f extraction_state.json
echo "Deleted extraction_state.json."

echo "--- ✅ Reset Complete! ---"
echo "You can now start the application with 'python3 app.py'."
echo "Default login: admin / password"
