import os
import re
import io
import requests
import pdfplumber
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIG — FRONT CARD (Aadhaar)
# ============================================================
# NOTE: Ye wahi blank template hai jo aapne upload kiya (red box
# pehle se usi mein fix hai) — isko kisi image host (ibb.co jaisa)
# par upload karke uska direct link yahan daal dena.
FRONT_CARD_TEMPLATE_URL = "PASTE_YOUR_BLANK_TEMPLATE_IMAGE_URL_HERE"

TEMPLATE_W, TEMPLATE_H = 1016, 638

PHOTO_BOX = (38, 184, 277, 466)
PHOTO_BORDER_WIDTH = 2

# Photo ke bilkul left side wali vertical "Aadhaar No. Issued: DATE" patti
VERTICAL_TEXT_X0 = 16
VERTICAL_TEXT_X1 = 46

CONTENT_X0 = 290
CONTENT_X1 = 975

TEXT_COL_TOP = 165
TEXT_COL_BOTTOM = 348
ROW_GAP_DEFAULT = 8      # jab Hindi naam mil jaye
ROW_GAP_NO_HINDI = 20    # jab Hindi naam na mile — gap zyada karke adjust

AADHAAR_NUM_BOX = (CONTENT_X0, 485, CONTENT_X1, 533)
VID_BOX = (CONTENT_X0, 536, CONTENT_X1, 562)

NAME_FONT_SIZE = 34
LABEL_FONT_SIZE = 26
AADHAAR_FONT_SIZE = 42
VID_FONT_SIZE = 22
ISSUED_FONT_SIZE = 20

# ------------------------------------------------------------
# FONTS
# English/numbers ke liye Times New Roman (jaisa maanga gaya hai).
# Hindi (Devanagari) ke liye Times New Roman kaam nahi karta (usme
# Devanagari glyphs hote hi nahi) — isliye Hindi ke liye alag se
# Devanagari font chahiye. Dono font files repo mein honi chahiye:
#   times.ttf      -> Times New Roman Regular
#   timesbd.ttf    -> Times New Roman Bold
#   NotoSansDevanagari-Regular.ttf
#   NotoSansDevanagari-Bold.ttf
# ------------------------------------------------------------
FONT_EN_REGULAR = "times.ttf"
FONT_EN_BOLD = "timesbd.ttf"
FONT_HI_REGULAR = "NotoSansDevanagari-Regular.ttf"
FONT_HI_BOLD = "NotoSansDevanagari-Bold.ttf"

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def get_font(lang, bold, size):
    if lang == "hi":
        path = FONT_HI_BOLD if bold else FONT_HI_REGULAR
    else:
        path = FONT_EN_BOLD if bold else FONT_EN_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except Exception:
            return ImageFont.load_default()


# ============================================================
# PDF SE DATA NIKALNA
# ============================================================
def find_face_photo_image(page):
    """
    PDF mein QR codes, logos, banners bhi images hote hain — sirf
    asli chehre wali photo pakadne ke liye uska typical ID-photo
    jaisa aspect ratio (chaudai < unchai, chौkोर nahi) use karte hain.
    """
    candidates = []
    for im in page.images:
        w = im["x1"] - im["x0"]
        h = im["bottom"] - im["top"]
        if h <= 0 or w <= 0:
            continue
        ratio = w / h
        if 0.55 <= ratio <= 0.95 and 15 <= w <= 260 and 15 <= h <= 320:
            candidates.append((im, w * h))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[1])
    im = candidates[0][0]
    bbox = (im["x0"], im["top"], im["x1"], im["bottom"])
    cropped = page.crop(bbox).to_image(resolution=400)
    return cropped.original.convert("RGB")


def find_issued_date(lines, full_text):
    """
    Photo ke saath wali "Aadhaar No. Issued: DD/MM/YYYY" patti PDF
    mein 90 degree ghumi hui hoti hai, isliye text-extraction mein
    ye ULTA (reversed) aata hai — jaise "7102/50/30" jo asal mein
    "03/05/2017" hai. Ye function usko dhoondh kar seedha karta hai.
    """
    for i, line in enumerate(lines):
        cleaned = re.sub(r"[^a-z]", "", line.strip().lower())
        if cleaned == "deussi":
            if i > 0:
                candidate = lines[i - 1].strip()
                rev = candidate[::-1]
                if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", rev):
                    return rev

    # Fallback — poore text mein ulta-shaped date pattern dhoondo
    m = re.search(r"\b(\d{4}/\d{2}/\d{1,2})\b", full_text)
    if m:
        return m.group(1)[::-1]
    return "N/A"


def extract_front_data(pdf_bytes, password=None):
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        lines = text.split("\n")

        hindi_name = "N/A"
        english_name = "N/A"
        for i, line in enumerate(lines):
            if line.strip() == "To":
                if i + 1 < len(lines):
                    hindi_name = lines[i + 1].strip()
                if i + 2 < len(lines):
                    english_name = lines[i + 2].strip()
                break

        # Agar "hindi_name" mein Devanagari characters hi nahi hain,
        # to matlab Hindi naam mila hi nahi — sirf English available hai.
        if hindi_name != "N/A" and not DEVANAGARI_RE.search(hindi_name):
            if english_name == "N/A":
                english_name = hindi_name
            hindi_name = "N/A"

        m = re.search(r"\b(\d{4}\s\d{4}\s\d{4})\b", text)
        aadhaar_number = m.group(1) if m else "N/A"

        m = re.search(r"VID\s*:?\s*([\d ]{15,30}\d)", text)
        vid = re.sub(r"\s+", " ", m.group(1)).strip() if m else "N/A"

        m = re.search(r"DOB:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
        dob = m.group(1) if m else "N/A"

        m = re.search(r"\b(MALE|FEMALE|TRANSGENDER)\b", text, re.IGNORECASE)
        gender = m.group(1).upper() if m else "N/A"

        issued_date = find_issued_date(lines, text)

        photo_img = find_face_photo_image(page)

        return {
            "hindi_name": hindi_name,
            "english_name": english_name,
            "aadhaar_number": aadhaar_number,
            "vid": vid,
            "dob": dob,
            "gender": gender,
            "issued_date": issued_date,
            "photo": photo_img,
        }


# ============================================================
# IMAGE HELPERS
# ============================================================
def cover_fit(img, box_w, box_h):
    img_ratio = img.width / img.height
    box_ratio = box_w / box_h
    if img_ratio > box_ratio:
        new_h = box_h
        new_w = int(new_h * img_ratio)
    else:
        new_w = box_w
        new_h = int(new_w / img_ratio)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - box_w) // 2
    top = (new_h - box_h) // 2
    return resized.crop((left, top, left + box_w, top + box_h))


def draw_centered_text(draw, box, text, font, fill="#1A2238"):
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + max((bw - tw) // 2, 0)
    ty = y0 + (bh - th) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=fill)


def draw_mixed_line(draw, box, segments, font_size, fill="#1A2238"):
    """
    segments: [(text, 'hi'/'en'), ...] — ek hi line mein Hindi aur
    English/number dono ko unke apne-apne font (Devanagari / Times
    New Roman) se, ek ke baad ek jodkar draw karta hai.
    """
    x0, y0, x1, y1 = box
    box_h = y1 - y0

    seg_data = []
    max_h = 0
    for text, lang in segments:
        font = get_font(lang, False, font_size)
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        seg_data.append((text, font, bbox, w))
        max_h = max(max_h, h)

    cx = x0
    for text, font, bbox, w in seg_data:
        ty = y0 + (box_h - max_h) // 2 - bbox[1]
        draw.text((cx, ty), text, font=font, fill=fill)
        cx += w


def build_content_rows(has_hindi):
    n_rows = 4 if has_hindi else 3
    total_h = TEXT_COL_BOTTOM - TEXT_COL_TOP
    gap = ROW_GAP_DEFAULT if has_hindi else ROW_GAP_NO_HINDI
    row_h = (total_h - (n_rows - 1) * gap) / n_rows

    rows = []
    cursor = TEXT_COL_TOP
    for _ in range(n_rows):
        top = cursor
        bottom = top + row_h
        rows.append((CONTENT_X0, top, CONTENT_X1, bottom))
        cursor = bottom + gap
    return rows


GENDER_HI = {"MALE": "पुरुष", "FEMALE": "महिला", "TRANSGENDER": "ट्रांसजेंडर"}


def build_front_card_image(pdf_bytes, password=None):
    data = extract_front_data(pdf_bytes, password)
    if data["photo"] is None:
        raise ValueError("PDF mein se chehre wali photo nahi mil payi")

    resp = requests.get(FRONT_CARD_TEMPLATE_URL, timeout=15)
    template = Image.open(io.BytesIO(resp.content)).convert("RGB")

    scale_x = template.width / TEMPLATE_W
    scale_y = template.height / TEMPLATE_H

    def scale_box(box):
        x0, y0, x1, y1 = box
        return (int(x0 * scale_x), int(y0 * scale_y), int(x1 * scale_x), int(y1 * scale_y))

    draw = ImageDraw.Draw(template)

    # ---------- PHOTO + 2px BORDER ----------
    photo_box = scale_box(PHOTO_BOX)
    pw, ph = photo_box[2] - photo_box[0], photo_box[3] - photo_box[1]
    fitted = cover_fit(data["photo"], pw, ph)
    template.paste(fitted, (photo_box[0], photo_box[1]))

    border_w = max(2, int(PHOTO_BORDER_WIDTH * scale_x))
    draw.rectangle(
        [photo_box[0], photo_box[1], photo_box[2] - 1, photo_box[3] - 1],
        outline="black", width=border_w
    )

    # ---------- VERTICAL "Aadhaar No. Issued: DATE" ----------
    issued_text = f"Aadhaar No. Issued: {data['issued_date']}"
    vfont = get_font("en", False, int(ISSUED_FONT_SIZE * scale_y))

    tmp = Image.new("RGBA", (900, 70), (255, 255, 255, 0))
    tdraw = ImageDraw.Draw(tmp)
    tbbox = tdraw.textbbox((0, 0), issued_text, font=vfont)
    tw, th = tbbox[2] - tbbox[0], tbbox[3] - tbbox[1]
    tdraw.text((-tbbox[0], -tbbox[1]), issued_text, font=vfont, fill="black")
    tmp = tmp.crop((0, 0, tw + 4, th + 4))
    rotated = tmp.transpose(Image.ROTATE_90)

    vx0 = int(VERTICAL_TEXT_X0 * scale_x)
    vx1 = int(VERTICAL_TEXT_X1 * scale_x)
    vx = vx0 + ((vx1 - vx0) - rotated.width) // 2
    vy = photo_box[1] + ((photo_box[3] - photo_box[1]) - rotated.height) // 2
    template.paste(rotated, (vx, vy), rotated)

    # ---------- NAME / DOB / GENDER ROWS ----------
    has_hindi = data["hindi_name"] != "N/A"
    raw_rows = build_content_rows(has_hindi)
    rows = [scale_box(r) for r in raw_rows]

    name_font_size = int(NAME_FONT_SIZE * scale_y)
    label_font_size = int(LABEL_FONT_SIZE * scale_y)

    idx = 0
    if has_hindi:
        draw_mixed_line(draw, rows[idx], [(data["hindi_name"], "hi")], name_font_size)
        idx += 1

    draw_mixed_line(draw, rows[idx], [(data["english_name"], "en")], name_font_size)
    idx += 1

    draw_mixed_line(
        draw, rows[idx],
        [("जन्म तिथि/DOB: ", "hi"), (data["dob"], "en")],
        label_font_size
    )
    idx += 1

    gender_hi = GENDER_HI.get(data["gender"], "")
    draw_mixed_line(
        draw, rows[idx],
        [(gender_hi + "/ ", "hi"), (data["gender"], "en")],
        label_font_size
    )

    # ---------- AADHAAR NUMBER + VID ----------
    aadhaar_box = scale_box(AADHAAR_NUM_BOX)
    draw_centered_text(
        draw, aadhaar_box, data["aadhaar_number"],
        get_font("en", True, int(AADHAAR_FONT_SIZE * scale_y))
    )

    vid_box = scale_box(VID_BOX)
    draw_centered_text(
        draw, vid_box, f"VID: {data['vid']}",
        get_font("en", False, int(VID_FONT_SIZE * scale_y))
    )

    return template


# ============================================================
# ENDPOINT
# ============================================================
@app.route("/generate-card", methods=["POST"])
def generate_card():
    if "pdf" not in request.files:
        return jsonify({"error": "PDF file is required (field name: pdf)"}), 400

    password = request.form.get("password", "").strip()
    pdf_file = request.files["pdf"]
    pdf_bytes = pdf_file.read()

    try:
        template = build_front_card_image(pdf_bytes, password)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except pdfplumber.pdfminer.pdfdocument.PDFPasswordIncorrect:
        return jsonify({"error": "Password galat hai ya PDF unlock nahi ho paayi"}), 400
    except Exception as e:
        return jsonify({"error": f"Could not generate the card: {str(e)}"}), 500

    output = io.BytesIO()
    template.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", as_attachment=False, download_name="aadhaar-card-front.png")


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Aadhaar Card Generator API is running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
