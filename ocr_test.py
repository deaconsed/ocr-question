"""
OCR Test v2: Upscale crops before OCR for better accuracy.
Tests multiple regions and scale factors on the problematic frame.
Run: py ocr_test.py
"""
import cv2
import easyocr

IMAGE_PATH = r"d:\OCR\extracted_frames\_unidentified\frame_112341.jpg"

reader = easyocr.Reader(['en'], gpu=False, verbose=False)

img = cv2.imread(IMAGE_PATH)
h, w = img.shape[:2]
print(f"Image size: {w}x{h}\n")

# The saved workspace image layout (from the screenshots):
#   Row 0-25:  Subject button bar (green/orange buttons with white text)
#   Row ~45-60:  Subject name in colored text on WHITE background (e.g. "ECONOMICS")
#   Row ~65-85:  "Question 10" in bold black text on WHITE background
#
# We want to target the WHITE area below the buttons, not the buttons themselves.

# ---- Test: Subject + Question text on white background ----
# Skip the button bar (top ~10%), grab the subject name + question line
crops = {
    "SUBJECT_LINE (y:10%-16%)": img[int(h*0.10):int(h*0.16), 0:int(w*0.4)],
    "QUESTION_LINE (y:16%-22%)": img[int(h*0.16):int(h*0.22), 0:int(w*0.4)],
    "BOTH_LINES (y:10%-22%)": img[int(h*0.10):int(h*0.22), 0:int(w*0.4)],
    "WIDER_BOTH (y:8%-25%)": img[int(h*0.08):int(h*0.25), 0:int(w*0.5)],
}

for label, crop in crops.items():
    print(f"{'=' * 60}")
    print(f"CROP: {label}  |  size: {crop.shape[1]}x{crop.shape[0]}")
    print(f"{'=' * 60}")
    
    # Upscale 3x for better OCR accuracy
    scaled = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    
    # Raw (no preprocessing)
    results_raw = reader.readtext(scaled, detail=0, paragraph=False)
    print(f"  Raw:       {results_raw}")
    
    # Grayscale + threshold
    gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
    results_thresh = reader.readtext(thresh, detail=0, paragraph=False)
    print(f"  Threshold: {results_thresh}")
    
    print()

print("Done.")
