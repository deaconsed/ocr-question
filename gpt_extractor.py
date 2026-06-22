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
    5. DIAGRAMS: Only set `has_image` to true for genuine visual content that cannot be transcribed as text: graphs, circuit diagrams, biological/anatomical diagrams, geometric figures, maps, or illustrations.
       - NEVER set `has_image` to true for: tables (convert to Markdown), chemical equations (transcribe using KaTeX), or any mathematical/physical formula.
       - Chemical equations such as "N₂(g) + O₂(g) ⇌ 2NO(g) ΔH = +180 kJ mol⁻¹" MUST be transcribed as KaTeX in `question_text` (e.g., $N_{{2(g)}} + O_{{2(g)}} \rightleftharpoons 2NO_{{(g)}}\ \Delta H = +180\ \text{{kJ mol}}^{{-1}}$). They are NOT images.
    6. Set `image_name` to exactly "{filename}".
    7. The `options` should be a dictionary mapping the option letter (e.g., "A", "B", "C", "D") to its corresponding text.
    8. Options that contain ANY mathematical expression (variables, exponents, fractions, symbols, equations) MUST use KaTeX notation just like the question text. The only exception is reading-comprehension cloze tests where options are plain words or phrases (e.g., "however", "therefore") — those should remain plain text.
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
    You are an expert at formatting extracted examination questions for Markdown + KaTeX rendering.
    I have an extracted examination question and its multiple choice options. They may contain
    mathematical formulas that are not (or wrongly) wrapped in delimiters, and/or tabular data
    (e.g. a frequency distribution) that is broken or not valid Markdown.

    A) MATH
    1. Identify ANY mathematical formulas, fractions, matrices, or symbols (they might be wrapped
       in plain parentheses like `( \\frac{{1}}{{2}} )` or `( x^2 )` instead of proper delimiters).
    2. Wrap them strictly in KaTeX delimiters:
       - Inline math: use single `$ ... $` (e.g. `$x=5$`). DO NOT use `\\(` and `\\)`.
       - Block math (like `\\begin{{matrix}}...`): use `$$ ... $$`. DO NOT use `\\[` and `\\]`.
    3. Remove any plain parentheses `( )` that were incorrectly wrapping math, replacing them with
       the proper delimiters.

    B) TABLES
    1. If the text contains tabular data — rows of cells separated by `|`, or values that clearly
       form a table such as a frequency distribution — reformat it into a VALID GitHub-Flavored
       Markdown (GFM) table.
    2. A valid GFM table MUST:
       - Put each row on its own single line, with NO blank lines between the rows.
       - Have the header row followed IMMEDIATELY by a separator row of dashes, one `---` per
         column, e.g. `| --- | --- | --- |`.
       - Have the same number of columns in every row.
    3. If the separator row is missing, ADD it right after the first (header) row. If there are
       stray blank lines between table rows, REMOVE them so the rows are contiguous.
    4. Keep every cell value EXACTLY as given (including any math, which stays wrapped in `$...$`).
       Do not add, drop, reorder, or change any cell.

    Example of fixing a broken table:
    INPUT:
    | $X$ | 1 | 2 | 3 | 4 | 5 |

    | Frequency | 5 | 4 | 3 | 2 | 1 |
    OUTPUT:
    | $X$ | 1 | 2 | 3 | 4 | 5 |
    | --- | --- | --- | --- | --- | --- |
    | Frequency | 5 | 4 | 3 | 2 | 1 |

    C) GENERAL
    - Do NOT alter the wording of the question or the meaning of any cell/option.
    - Leave all other text and Markdown exactly as it is.

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

def process_single_question(question_id, emit_log, client=None, force=False):
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
    # When force=True, re-extract even if question_text is already populated
    # (used by the manual-review "Re-run AI" button).
    where_clause = "WHERE q.id = %s" if force else "WHERE q.id = %s AND q.question_text IS NULL"
    cursor.execute(f"""
        SELECT q.id, q.exam_id, q.question_number, q.image_name, s.name as subject_name
        FROM questions q
        JOIN subjects s ON q.subject_id = s.id
        {where_clause}
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
