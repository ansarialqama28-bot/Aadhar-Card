import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont, ImageOps
import io
import base64
import re
import requests

app = Flask(__name__)
# CORS setup taaki kisi bhi website se API connect ho sake
CORS(app, resources={r"/*": {"origins": "*"}})

TEMPLATE_URL = "https://i.ibb.co/BH688zxP/Whats-App-Image-2026-08-01-at-6-54-00-PM.jpg"

# Fonts isi folder (api/) ke andar rakhne honge. __file__ ka use karke
# absolute path banaya hai taaki Vercel ke serverless environment me bhi
# sahi jagah se font mile (relative path "times.ttf" wahan kaam nahi karta).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def font_path(name):
    return os.path.join(BASE_DIR, name)


# === TEST ROUTE (Checking ke liye) ===
@app.route('/', methods=['GET'])
def home():
    return "API is running perfectly! Backend is Connected.", 200


def extract_pdf_data(pdf_bytes, password, want_mobile):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.needs_pass:
        doc.authenticate(password)

    page = doc[0]
    text = page.get_text("text")

    # 1. Extract Photo (Find largest image in PDF)
    images = page.get_images(full=True)
    photo_bytes = None
    max_size = 0
    for img in images:
        xref = img[0]
        base_image = doc.extract_image(xref)
        img_size = len(base_image["image"])
        if img_size > max_size:
            max_size = img_size
            photo_bytes = base_image["image"]

    # 2. Extract Text Data
    data = {
        "dob": "", "gender": "", "vid": "", "aadhaar": "",
        "issue_date": "", "hi_name": "", "en_name": "", "mobile": ""
    }

    # DOB
    dob_match = re.search(r'(?:DOB|Year|जन्म)[^\d]*([0-9/]{4,10})', text, re.IGNORECASE)
    if dob_match:
        data["dob"] = dob_match.group(1)

    # Gender
    if "MALE" in text.upper() and "FEMALE" not in text.upper():
        data["gender"] = "पुरुष/ MALE"
    elif "FEMALE" in text.upper():
        data["gender"] = "महिला/ FEMALE"

    # VID Extract & Clean
    vid_match = re.search(r'[0-9]{4}\s[0-9]{4}\s[0-9]{4}\s[0-9]{4}', text)
    if vid_match:
        data["vid"] = vid_match.group(0)
        text = text.replace(data["vid"], "")

    # Aadhaar Extract (Masked & Visible dono pakdega)
    aadhaar_match = re.search(r'(?:[0-9]{4}|[xX*]{4})\s(?:[0-9]{4}|[xX*]{4})\s[0-9]{4}', text)
    if aadhaar_match:
        data["aadhaar"] = aadhaar_match.group(0)

    # Issue Date (Rotated text)
    issue_match = re.search(r'issued:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})', text, re.IGNORECASE)
    if issue_match:
        data["issue_date"] = issue_match.group(1)

    # Mobile Number (If requested)
    if want_mobile == 'yes':
        mob_match = re.search(r'[6-9][0-9]{9}', text.replace(" ", ""))
        if mob_match:
            data["mobile"] = mob_match.group(0)

    # Name Parsing (Lines above DOB)
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    for i, line in enumerate(lines):
        if "DOB" in line or "जन्म" in line:
            if i >= 2:
                en_candidate = lines[i - 1]
                hi_candidate = lines[i - 2]

                # Check if Hindi name exists
                if any("\u0900" <= c <= "\u097F" for c in hi_candidate):
                    data["hi_name"] = hi_candidate
                    data["en_name"] = en_candidate
                else:
                    data["en_name"] = en_candidate  # Only English Name found
            break

    return data, photo_bytes


@app.route('/generate', methods=['POST', 'OPTIONS'])
def generate_card():
    # Preflight request ko allow karne ke liye
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    try:
        file = request.files.get('pdf')
        password = request.form.get('password', '')
        want_mobile = request.form.get('mobile', 'no')

        if not file:
            return jsonify({"error": "No file uploaded"}), 400

        pdf_bytes = file.read()
        try:
            data, photo_bytes = extract_pdf_data(pdf_bytes, password, want_mobile)
        except Exception:
            return jsonify({"error": "Incorrect Password or Corrupted PDF"}), 400

        if not photo_bytes:
            return jsonify({"error": "Photo not found in PDF"}), 400

        # --- IMAGE PROCESSING ---
        # 1. Download Blank Template from IBB
        response = requests.get(TEMPLATE_URL, timeout=15)
        template = Image.open(io.BytesIO(response.content)).convert("RGBA")
        draw = ImageDraw.Draw(template)

        # 2. Add 2px Border to Photo & Resize
        photo = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
        photo = photo.resize((175, 220))  # Width, Height adjusted for template
        photo_with_border = ImageOps.expand(photo, border=2, fill='black')
        template.paste(photo_with_border, (50, 140))  # X, Y coordinates

        # 3. Load Fonts (must live inside the api/ folder in this repo)
        try:
            font_eng = ImageFont.truetype(font_path("times.ttf"), 24)
            font_eng_bold = ImageFont.truetype(font_path("timesbd.ttf"), 26)
            font_aadhaar = ImageFont.truetype(font_path("timesbd.ttf"), 45)
            font_hi = ImageFont.truetype(font_path("mangal.ttf"), 28)
            font_small = ImageFont.truetype(font_path("times.ttf"), 18)
        except Exception:
            font_eng = ImageFont.load_default()
            font_eng_bold = font_eng
            font_aadhaar = font_eng
            font_hi = font_eng
            font_small = font_eng

        # 4. Draw Issue Date (Rotated 90 degrees)
        if data["issue_date"]:
            issue_text = f"Aadhaar No. Issued: {data['issue_date']}"
            txt_img = Image.new('RGBA', (300, 30), (255, 255, 255, 0))
            txt_draw = ImageDraw.Draw(txt_img)
            txt_draw.text((0, 0), issue_text, font=font_small, fill="black")
            rotated_txt = txt_img.rotate(90, expand=1)
            # Adjust coordinates so it aligns to the left of the photo
            template.paste(rotated_txt, (15, 140), rotated_txt)

        # 5. Draw Dynamic Spacing Text (Name, DOB, Gender, Mobile)
        x_text = 250
        y_curr = 135

        y_gap = 45 if data["hi_name"] else 55

        if data["hi_name"]:
            draw.text((x_text, y_curr), data["hi_name"], font=font_hi, fill="black")
            y_curr += y_gap

        draw.text((x_text, y_curr), data["en_name"], font=font_eng, fill="black")
        y_curr += y_gap

        draw.text((x_text, y_curr), f"जन्म तिथि/DOB: {data['dob']}", font=font_eng, fill="black")
        y_curr += y_gap

        draw.text((x_text, y_curr), data["gender"], font=font_hi, fill="black")
        y_curr += y_gap

        if want_mobile == 'yes' and data["mobile"]:
            draw.text((x_text, y_curr), f"Mob: {data['mobile']}", font=font_eng_bold, fill="black")

        # 6. Draw Aadhaar & VID (Centered above bottom red strip)
        center_x = template.width / 2
        draw.text((center_x, 480), data["aadhaar"], font=font_aadhaar, fill="black", anchor="mm")

        if data["vid"]:
            draw.text((center_x, 530), f"VID: {data['vid']}", font=font_small, fill="black", anchor="mm")

        # Output final image
        output_buffer = io.BytesIO()
        template.convert("RGB").save(output_buffer, format="JPEG", quality=95)
        base64_str = base64.b64encode(output_buffer.getvalue()).decode("utf-8")

        return jsonify({"image": f"data:image/jpeg;base64,{base64_str}"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Note: app.run() yahan jaanboojh kar nahi hai. Vercel serverless is file
# ko import karke seedha `app` (Flask/WSGI object) use karta hai — apna
# development server chalane ke liye "flask --app api/index run" use karein.
