# Linux Installation Guide for CBT OCR Pipeline

This guide provides instructions to set up and run the CBT Verification and OCR Pipeline on a Linux system (Ubuntu/Debian recommended).

## 1. System Requirements

Ensure your system has the following installed:
- Python 3.10 or higher
- MySQL Server (or MariaDB)
- Essential system libraries for OpenCV and EasyOCR

## 2. Install System Dependencies

Open a terminal and run the following commands to install required system libraries:

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv \
    libgl1-mesa-glx libglib2.0-0 \
    libsm6 libxext6 libxrender-dev \
    mysql-server
```

## 3. Database Setup

1. Start the MySQL service:
   ```bash
   sudo systemctl start mysql
   ```

2. Create the database and a user (replace `your_password` with a strong password):
   ```bash
   sudo mysql -u root
   ```
   Inside the MySQL prompt:
   ```sql
   CREATE DATABASE ocr_extractor;
   CREATE USER 'ocr_user'@'localhost' IDENTIFIED BY 'your_password';
   GRANT ALL PRIVILEGES ON ocr_extractor.* TO 'ocr_user'@'localhost';
   FLUSH PRIVILEGES;
   EXIT;
   ```

## 4. Application Setup

1. **Clone the repository** (if you haven't already):
   ```bash
   git clone <repository_url>
   cd ocr-pipeline
   ```

2. **Create a Virtual Environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Python Packages**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

## 5. Environment Configuration

Create a `.env` file in the root directory:

```bash
touch .env
nano .env
```

Add the following content (update with your actual credentials):

```env
DB_HOST=localhost
DB_USER=ocr_user
DB_PASSWORD=your_password
DB_NAME=ocr_extractor
OPENAI_API_KEY=sk-your-openai-api-key-here
FLASK_SECRET_KEY=any-random-secret-string
```

## 6. Initialize the Database

Run the initialization script to create the required tables and initial user roles:

```bash
python3 init_db.py
```

## 7. Running the Application

Start the Flask development server:

```bash
python3 app.py
```

The application will be accessible at `http://127.0.0.1:5000`.

## 8. Troubleshooting

- **OpenCV Errors**: If you encounter `libGL.so.1` errors, ensure `libgl1-mesa-glx` is installed.
- **Stream Disconnected / Application Crash**: On Linux, if the application crashes immediately when OCR starts, it is likely due to multiple instances of `libiomp5` being initialized (common with PyTorch). The application has been updated to handle this automatically, but ensure you are using the CPU version of Torch if you don't have a GPU:
  ```bash
  pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
  ```
- **EasyOCR GPU/pin_memory warning**: On Linux systems without an NVIDIA GPU, you may see a warning about `pin_memory`. This is a harmless PyTorch warning that occurs when running on CPU. The application has been configured to suppress this warning automatically to keep your logs clean.
- **Permissions**: Ensure the `extracted_frames/` directory is writable by the user running the application.

## 9. Resetting the System

If you need to wipe all data and start from scratch, you can use the included reset script:

```bash
chmod +x reset_system.sh
./reset_system.sh
```

This will:
- Drop and recreate the MySQL database.
- Clear the `extracted_frames/` directory.
- Delete the `extraction_state.json` file.
