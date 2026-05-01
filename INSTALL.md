# Installation Guide: CBT Frame Extractor

This guide will help you install and configure the CBT Frame Extractor application on a brand new computer.

## Prerequisites

Before starting, ensure you have the following installed on your machine:
1. **Python 3.8+**: [Download Python](https://www.python.org/downloads/) (Make sure to check "Add Python to PATH" during installation)
2. **MySQL Server**: [Download MySQL](https://dev.mysql.com/downloads/installer/) (Ensure the MySQL service is running)
3. **Git** (optional, if you are cloning the repository)

## Step 1: Set up the Python Environment

1. Open your terminal (Command Prompt or PowerShell on Windows).
2. Navigate to the project directory:
   ```bash
   cd path\to\your\OCR
   ```
3. Create a virtual environment to isolate dependencies:
   ```bash
   python -m venv venv
   ```
4. Activate the virtual environment:
   - **Windows:**
     ```bash
     venv\Scripts\activate
     ```
   - **Mac/Linux:**
     ```bash
     source venv/bin/activate
     ```
5. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

## Step 2: Configure Environment Variables

1. In the root directory (`d:\OCR`), locate or create a `.env` file.
2. Open `.env` in a text editor and add your configuration. It should look like this:

   ```ini
   OPENAI_API_KEY=your_openai_api_key_here
   DB_HOST=localhost
   DB_USER=root
   DB_PASSWORD=your_mysql_password
   DB_NAME=ocr_extractor
   FLASK_SECRET_KEY=your_random_secret_key
   ```
   > **Note:** Update `DB_PASSWORD` to match your MySQL root password. Replace the `OPENAI_API_KEY` with a valid key for GPT extraction to work.

## Step 3: Initialize the Database

The application comes with a script to set up the necessary database tables, migrations, and a default admin user.

1. Ensure your MySQL server is running.
2. Run the initialization script from your terminal:
   ```bash
   python init_db.py
   ```
3. If successful, you will see output confirming that tables have been ensured and the database initialization completed successfully. 
   - A default admin user is automatically created with the username `admin` and password `password`.

## Step 4: Run the Application

1. Start the Flask server by running:
   ```bash
   python app.py
   ```
2. Open your web browser and navigate to:
   [http://127.0.0.1:5000](http://127.0.0.1:5000)
3. Log in using the default credentials:
   - **Username:** admin
   - **Password:** password

> **Security Tip:** Once logged in, it is highly recommended to create a new admin account and delete the default `admin` account from the user management dashboard.

## Troubleshooting

- **Database Connection Failed:** Double-check your `.env` file to ensure `DB_HOST`, `DB_USER`, and `DB_PASSWORD` are correct and that the MySQL service is actively running.
- **Missing Module Errors:** Ensure your virtual environment is activated (`venv\Scripts\activate`) before running scripts.
- **OpenAI Errors:** Ensure your `OPENAI_API_KEY` is correct and has available credits.
