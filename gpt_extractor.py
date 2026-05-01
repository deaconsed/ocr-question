import os
import json
import base64
import argparse
from glob import glob
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel
from typing import Optional
from db import get_db_connection

# Load environment variables from .env file
load_dotenv()

# Configuration
INPUT_DIR = "extracted_frames"

class Options(BaseModel):
    A: Optional[str]
    B: Optional[str]
    C: Optional[str]
    D: Optional[str]

class QuestionData(BaseModel):
    question_number: int
    question_text: str
    options: Options
    has_image: bool
    image_name: str

class FixedContent(BaseModel):
    question_text: str
    options: Options

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def extract_question_with_gpt(client: OpenAI, image_path: str, filename: str) -> dict:
    base64_image = encode_image(image_path)
    
    prompt = f"""
    You are an expert AI assistant that digitizes examination questions from images.
    Please extract the question text and the multiple choice options from this image.
    
    CRITICAL INSTRUCTIONS:
    1. Retain all formatting in the question text using Markdown (e.g., **bold**, *italics*).
    2. TABLES: If the image contains a table, you MUST convert it into a clean Markdown table format and include it in the `question_text`.
    3. MATHEMATICAL FORMULAS: Any mathematical formulas, fractions, equations, or matrices MUST be formatted using KaTeX/LaTeX notation.
    4. MATH DELIMITERS: 
       - For inline math, you MUST exclusively wrap the formula in `$` (e.g., `$x = 5$`). DO NOT use `\\(` and `\\)`.
       - For block/display math (like `\begin{{matrix}}...`), you MUST exclusively wrap the formula in `$$`. DO NOT use `\\[` and `\\]`.
       - NEVER use standard parentheses like `( x = 5 )` or `( \\frac{{1}}{{2}} )` to wrap math, as this breaks the renderer.
    5. DIAGRAMS: Determine if the question contains a diagram, graph, or illustration that cannot be represented by text. If it does, set `has_image` to true. DO NOT set `has_image` to true for tables, as they must be converted to Markdown.
    6. Set `image_name` to exactly "{filename}".
    7. The `options` should be a dictionary mapping the option letter (e.g., "A", "B", "C", "D") to its corresponding text.
    8. DO NOT use KaTeX notation (like \\text{{}}) for regular text or inline options within reading passages (e.g., cloze tests). Inline options should be formatted as plain text.
    """

    response = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                            "detail": "high"
                        }
                    }
                ]
            }
        ],
        response_format=QuestionData,
        temperature=0.0
    )
    
    return response.choices[0].message.parsed.model_dump()

def fix_text_with_gpt(client: OpenAI, question_text: str, options: dict) -> dict:
    prompt = f"""
    You are an expert mathematical formatter. I have an extracted examination question and its multiple choice options.
    The mathematical equations might be wrapped in standard parentheses like `( \\frac{{1}}{{2}} )` or `( x^2 )` instead of proper LaTeX/KaTeX delimiters.
    
    YOUR TASK:
    1. Scan the `question_text` and the `options`.
    2. Identify ANY mathematical formulas, fractions, matrices, or symbols.
    3. Ensure they are strictly wrapped in KaTeX delimiters:
       - For inline math, strictly use `$` (e.g. `$x=5$`). DO NOT use `\\(` and `\\)`.
       - For block math (like `\\begin{{matrix}}...`), strictly use `$$`. DO NOT use `\\[` and `\\]`.
    4. Remove any plain parentheses `( )` that were incorrectly wrapping the math, replacing them with the proper delimiters.
    5. LEAVE ALL OTHER TEXT AND MARKDOWN EXACTLY AS IT IS. Do NOT alter the wording of the question.
    
    Current Question Text:
    {question_text}
    
    Current Options:
    {json.dumps(options, indent=2)}
    """
    
    try:
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": prompt}
            ],
            response_format=FixedContent,
            temperature=0.0
        )
        return response.choices[0].message.parsed.model_dump()
    except Exception as e:
        print(f"Error fixing text with GPT: {e}")
        return None

def process_single_question(question_id, emit_log, client=None):
    if client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            emit_log("Error: OPENAI_API_KEY not found.")
            return False
        client = OpenAI(api_key=api_key)
        
    conn = get_db_connection()
    if not conn:
        emit_log("Error: Could not connect to database in GPT worker.")
        return False
        
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT q.id, q.exam_id, q.question_number, q.image_name, s.name as subject_name 
        FROM questions q
        JOIN subjects s ON q.subject_id = s.id
        WHERE q.id = %s AND q.question_text IS NULL
    """, (question_id,))
    
    q = cursor.fetchone()
    if not q:
        cursor.close()
        conn.close()
        return False # Already processed or doesn't exist
        
    subject_folder = q['subject_name'].lower().replace(" ", "_")
    
    # Try exam-scoped path first, then legacy path
    img_path = os.path.join(INPUT_DIR, f"exam_{q['exam_id']}", subject_folder, q['image_name'])
    if not os.path.exists(img_path):
        img_path = os.path.join(INPUT_DIR, subject_folder, q['image_name'])
    
    emit_log(f"  [AI] Extracting {q['subject_name']} Q{q['question_number']}...")
    
    if not os.path.exists(img_path):
        emit_log(f"  [AI Error] Image not found: {img_path}")
        cursor.close()
        conn.close()
        return False
        
    try:
        data = extract_question_with_gpt(client, img_path, q['image_name'])
        options_json = json.dumps(data.get('options', {}))
        
        cursor.execute("""
            UPDATE questions 
            SET question_text = %s, options = %s, has_image = %s
            WHERE id = %s
        """, (data.get('question_text', ''), options_json, data.get('has_image', False), q['id']))
        conn.commit()
        emit_log(f"  [AI] Success: {q['subject_name']} Q{q['question_number']}")
        success = True
    except Exception as e:
        emit_log(f"  [AI Error] Processing {q['image_name']}: {e}")
        success = False
        
    cursor.close()
    conn.close()
    return success

def main():
    parser = argparse.ArgumentParser(description="Extract questions using OpenAI Vision API.")
    parser.add_argument("--limit", type=int, help="Limit the number of images processed for testing.", default=None)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY not found.")
        return

    client = OpenAI(api_key=api_key)
    conn = get_db_connection()
    if not conn:
        print("Error: Could not connect to database.")
        return
    cursor = conn.cursor(dictionary=True)

    query = "SELECT id FROM questions WHERE question_text IS NULL"
    if args.limit:
        query += f" LIMIT {args.limit}"
        
    cursor.execute(query)
    unextracted = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not unextracted:
        print("No questions found that need extraction.")
        return

    print(f"Found {len(unextracted)} questions to process.")
    for row in unextracted:
        process_single_question(row['id'], print, client)
        
    print("Extraction complete.")

if __name__ == "__main__":
    main()
