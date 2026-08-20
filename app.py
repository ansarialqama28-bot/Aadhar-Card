import os
import re
import io
import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIG — FRONT CARD (Aadhaar)
# ============================================================
TEMPLATE_FILENAME = "aadhar_template_front.jpg"
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), TEMPLATE_FILENAME)

TEMPLATE_W, TEMPLATE_H = 1016, 638

PHOTO_BOX = (38, 160, 277, 466)
PHOTO_BORDER_WIDTH = 2

VERTICAL_TEXT_X0 = 16
VERTICAL_TEXT_X1 = 46

CONTENT_X0 = 305
CONTENT_X1 = 975

# Naam/DOB/Gender crop box — height badhayi (145 -> 190) taaki
# "medium" size dikhe. Crop bbox bhi tight kiya hai (kam white
# margin), isliye ab jo bhi height milegi usme text zyada dense/
# bada render hoga.
FRONT_INFO_BOX = (CONTENT_X0, 155, CONTENT_X1, 345)

# Mobile number text hi rehta hai. Font-size ab info-crop ke render
# hone wale effective size se match karta hai — info box height 190,
# 4 lines, tight-crop ka width:height ratio ~2.1 hai, box ratio
# zyada hai isliye height-bound rehta hai: 4 lines 190 units me,
# ~47.5 units/line — Mobile ka font bhi usi range me rakha hai.
MOBILE_BOX = (CONTENT_X0 + 16, 333, CONTENT_X1, 380)

AADHAAR_NUM_BOX = (0, 485, TEMPLATE_W, 533)
VID_BOX = (0, 536, TEMPLATE_W, 562)

MOBILE_FONT_SIZE = 32
AADHAAR_FONT_SIZE = 42
VID_FONT_SIZE = 26
ISSUED_FONT_SIZE = 20
LABEL_FONT_SIZE = 34

PHOTO_BRIGHTNESS = 1.3

# Tight bbox — kam white margin, isliye text box ke andar zyada
# "dense"/bada render hota hai
FRONT_INFO_CROP_PDF_BBOX = (130, 608, 213, 650)
FRONT_INFO_CROP_RESOLUTION = 500

# ============================================================
# CONFIG — BACK CARD (Aadhaar)
# ============================================================
BACK_TEMPLATE_FILENAME = "aadhar_template_back.jpg"
BACK_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), BACK_TEMPLATE_FILENAME)

BACK_TEMPLATE_W, BACK_TEMPLATE_H = 1016, 638

BACK_VERTICAL_TEXT_X0 = 16
BACK_VERTICAL_TEXT_X1 = 46

BACK_CONTENT_X0 = 55
BACK_CONTENT_X1 = 690
COMBINED_ADDRESS_BOX = (BACK_CONTENT_X0, 145, BACK_CONTENT_X1, 482)

BACK_LABEL_FONT_SIZE = 26
BACK_ADDRESS_FONT_SIZE = 24
BACK_ADDRESS_LINE_GAP = 6

ADDR_CROP_PDF_BBOX = (321, 602, 460, 690)
ADDR_CROP_RESOLUTION = 500

FONT_EN_REGULAR = "times.ttf"
FONT_EN_BOLD = "timesbd.ttf"
FONT_HI_REGULAR = "NotoSansDevanagari-Regular.ttf"
FONT_HI_BOLD = "NotoSansDevanagari-Bold.ttf"

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
DEVANAGARI_MATRA_VIRAMA_RE = re.compile(r"[\u093E-\u094C\u0900-\u0903\u094D]")


def fix_devanagari_spacing(text):
    return re.sub(r"([\u093E-\u094C\u0900-\u0903\u094D])[ \t]+", r"\1", text)


def make_white_transparent(img, threshold=245):
    img = img.convert("RGBA")
    arr = np.array(img)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    white_mask = (r >= threshold) & (g >= threshold) & (b >= threshold)
    arr[..., 3] = np.where(white_mask, 0, 255)
    return Image.fromarray(arr, mode="RGBA")


def crop_pdf_region(pdf_bytes, password, bbox, resolution):
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
            page = pdf.pages[0]
            safe_bbox = (
                max(0, bbox[0]), max(0, bbox[1]),
                min(page.width, bbox[2]), min(page.height, bbox[3])
            )
            if safe_bbox[2] <= safe_bbox[0] or safe_bbox[3] <= safe_bbox[1]:
                return None
            cropped = page.crop(safe_bbox).to_image(resolution=resolution)
            img = cropped.original.convert("RGB")
            return make_white_transparent(img)
    except Exception:
        return None


def crop_combined_address_block(pdf_bytes, password=None):
    return crop_pdf_region(pdf_bytes, password, ADDR_CROP_PDF_BBOX, ADDR_CROP_RESOLUTION)


def crop_front_info_block(pdf_bytes, password=None):
    return crop_pdf_region(pdf_bytes, password, FRONT_INFO_CROP_PDF_BBOX, FRONT_INFO_CROP_RESOLUTION)


def extract_text_pdfium(pdf_bytes, password=None):
    pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes), password=password or None)
    try:
        page = pdf[0]
        textpage = page.get_textpage()
        raw_text = textpage.get_text_range()
    finally:
        pdf.close()

    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    text = fix_devanagari_spacing(text)
    return text


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


def find_face_photo_image(page):
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


def find_issued_date(full_text):
    m = re.search(r"issued\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", full_text, re.IGNORECASE)
    if m:
        return m.group(1)
    return "N/A"


def detect_name_block(lines):
    dob_idx = None
    for i, line in enumerate(lines):
        if re.search(r"DOB\s*:", line, re.IGNORECASE):
            dob_idx = i
            break
    if dob_idx is None:
        return "N/A", "N/A", 0

    def clean_name_candidate(raw):
        return re.split(r"\s*(?:Address\s*:|VID\s*:?|S/O:|C/O:|D/O:|W/O:)", raw, maxsplit=1)[0].strip()

    collected = []
    j = dob_idx - 1
    while j >= 0 and len(collected) < 2:
        stripped = clean_name_candidate(lines[j].strip())
        if not stripped:
            j -= 1
            continue
        if re.search(r"issued|Aadhaar\s*no|Details\s*as\s*on|^\d", stripped, re.IGNORECASE):
            break
        collected.insert(0, stripped)
        j -= 1

    if not collected:
        return "N/A", "N/A", 0
    if len(collected) == 1:
        if DEVANAGARI_RE.search(collected[0]):
            return collected[0], "N/A", 1
        return "N/A", collected[0], 1
    if DEVANAGARI_RE.search(collected[0]):
        return collected[0], collected[1], 2
    return "N/A", collected[-1], len(collected)


def collect_labeled_block(lines, start_idx, label_pattern, stop_re):
    collected = []
    first_line = lines[start_idx]
    parts = re.split(label_pattern, first_line, maxsplit=1)
    remainder = parts[1].strip() if len(parts) > 1 else ""
    if remainder:
        collected.append(remainder)
        if stop_re.search(remainder):
            return re.sub(r"\s+", " ", " ".join(collected)).strip(" ,")

    for j in range(start_idx + 1, min(start_idx + 8, len(lines))):
        raw_line = lines[j].strip()
        if not raw_line:
            continue
        if re.match(r"^\d{4}\s\d{4}\s\d{4}$", raw_line) or re.match(r"^VID\s*:", raw_line, re.IGNORECASE):
            break
        collected.append(raw_line)
        if stop_re.search(raw_line):
            break

    return re.sub(r"\s+", " ", " ".join(collected)).strip(" ,")


PIN_END_RE = re.compile(r"-\s*\d{6}\b")


def extract_front_data(pdf_bytes, password=None):
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
        page = pdf.pages[0]
        photo_img = find_face_photo_image(page)

    text = extract_text_pdfium(pdf_bytes, password)
    lines = text.split("\n")

    hindi_name, english_name, _ = detect_name_block(lines)

    m = re.search(r"\b(\d{4}\s\d{4}\s\d{4})\b", text)
    aadhaar_number = m.group(1) if m else "N/A"

    m = re.search(r"VID\s*:?\s*([\d ]{15,30}\d)", text)
    vid = re.sub(r"\s+", " ", m.group(1)).strip() if m else "N/A"

    m = re.search(r"DOB:\s*(\d{1,2}/\d{1,2}/\d{4})", text)
    dob = m.group(1) if m else "N/A"

    m = re.search(r"\b(MALE|FEMALE|TRANSGENDER)\b", text, re.IGNORECASE)
    gender = m.group(1).upper() if m else "N/A"

    m = re.search(r"Mobile:\s*(\d{10})", text)
    mobile_number = m.group(1) if m else "N/A"

    issued_date = find_issued_date(text)

    return {
        "hindi_name": hindi_name,
        "english_name": english_name,
        "aadhaar_number": aadhaar_number,
        "vid": vid,
        "dob": dob,
        "gender": gender,
        "mobile_number": mobile_number,
        "issued_date": issued_date,
        "photo": photo_img,
    }


def find_details_as_on_date(lines, full_text):
    m = re.search(r"Details\s+as\s+on\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", full_text, re.IGNORECASE)
    if m:
        return m.group(1)
    return "N/A"


def extract_back_data(pdf_bytes, password=None):
    text = extract_text_pdfium(pdf_bytes, password)
    lines = text.split("\n")

    m = re.search(r"\b(\d{4}\s\d{4}\s\d{4})\b", text)
    aadhaar_number = m.group(1) if m else "N/A"

    m = re.search(r"VID\s*:?\s*([\d ]{15,30}\d)", text)
    vid = re.sub(r"\s+", " ", m.group(1)).strip() if m else "N/A"

    english_address = None
    for i, line in enumerate(lines):
        if line.strip() == "Address:" or line.strip().startswith("Address:"):
            candidate = collect_labeled_block(lines, i, r"Address\s*:", PIN_END_RE)
            if candidate:
                english_address = candidate
            break
    if not english_address:
        english_address = "N/A"

    hindi_address = None
    for i, line in enumerate(lines):
        if "पत्ता" in line or "पता" in line:
            candidate = collect_labeled_block(lines, i, r"पत्ता\s*:?|पता\s*:?", PIN_END_RE)
            if candidate and DEVANAGARI_RE.search(candidate):
                hindi_address = candidate
            break

    if not hindi_address:
        for i, line in enumerate(lines):
            if "आत्मज" in line:
                candidate = collect_labeled_block(lines, i, r"^", PIN_END_RE)
                if candidate and DEVANAGARI_RE.search(candidate):
                    hindi_address = candidate
                break

    details_as_on = find_details_as_on_date(lines, text)

    return {
        "hindi_address": hindi_address,
        "english_address": english_address,
        "aadhaar_number": aadhaar_number,
        "vid": vid,
        "details_as_on": details_as_on,
    }


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


def contain_fit(img, box_w, box_h):
    img_ratio = img.width / img.height
    box_ratio = box_w / box_h
    if img_ratio > box_ratio:
        new_w = box_w
        new_h = max(1, int(box_w / img_ratio))
    else:
        new_h = box_h
        new_w = max(1, int(box_h * img_ratio))
    return img.resize((new_w, new_h), Image.LANCZOS)


def draw_centered_text(draw, box, text, font, fill="#1A2238"):
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + max((bw - tw) // 2, 0)
    ty = y0 + (bh - th) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=fill)


def draw_mixed_line(draw, box, segments, font_size, fill="#1A2238"):
    x0, y0, x1, y1 = box
    box_h = y1 - y0

    seg_data = []
    max_ascent = 0
    max_descent = 0
    for text, lang in segments:
        font = get_font(lang, False, font_size)
        ascent, descent = font.getmetrics()
        seg_data.append((text, font))
        max_ascent = max(max_ascent, ascent)
        max_descent = max(max_descent, descent)

    total_h = max_ascent + max_descent
    baseline_y = y0 + (box_h - total_h) // 2 + max_ascent

    cx = x0
    for text, font in seg_data:
        draw.text((cx, baseline_y), text, font=font, fill=fill, anchor="ls")
        bbox = draw.textbbox((cx, baseline_y), text, font=font, anchor="ls")
        cx = bbox[2]


def draw_wrapped_text(draw, box, text, font, line_gap=6, fill="#1A2238"):
    x0, y0, x1, y1 = box
    max_width = x1 - x0

    words = text.split(" ")
    lines_out = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width or not current:
            current = trial
        else:
            lines_out.append(current)
            current = word
    if current:
        lines_out.append(current)

    ascent, descent = font.getmetrics()
    line_h = ascent + descent + line_gap

    y = y0
    for line in lines_out:
        draw.text((x0, y), line, font=font, fill=fill)
        y += line_h


GENDER_HI = {"MALE": "पुरुष", "FEMALE": "महिला", "TRANSGENDER": "ट्रांसजेंडर"}


def build_front_card_image(pdf_bytes, password=None, print_mobile=False):
    data = extract_front_data(pdf_bytes, password)
    if data["photo"] is None:
        raise ValueError("PDF mein se chehre wali photo nahi mil payi")

    template = Image.open(TEMPLATE_PATH).convert("RGB")

    scale_x = template.width / TEMPLATE_W
    scale_y = template.height / TEMPLATE_H

    def scale_box(box):
        x0, y0, x1, y1 = box
        return (int(x0 * scale_x), int(y0 * scale_y), int(x1 * scale_x), int(y1 * scale_y))

    draw = ImageDraw.Draw(template)

    photo_box = scale_box(PHOTO_BOX)
    pw, ph = photo_box[2] - photo_box[0], photo_box[3] - photo_box[1]
    fitted = cover_fit(data["photo"], pw, ph)
    fitted = ImageEnhance.Brightness(fitted).enhance(PHOTO_BRIGHTNESS)
    template.paste(fitted, (photo_box[0], photo_box[1]))

    border_w = max(2, int(PHOTO_BORDER_WIDTH * scale_x))
    draw.rectangle(
        [photo_box[0], photo_box[1], photo_box[2] - 1, photo_box[3] - 1],
        outline="black", width=border_w
    )

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

    info_box = scale_box(FRONT_INFO_BOX)
    info_crop = crop_front_info_block(pdf_bytes, password)

    if info_crop is not None:
        box_w = info_box[2] - info_box[0]
        box_h = info_box[3] - info_box[1]
        fitted_info = contain_fit(info_crop, box_w, box_h)
        template.paste(fitted_info, (info_box[0], info_box[1]), fitted_info)
    else:
        has_hindi = data["hindi_name"] != "N/A"
        y = info_box[1]
        row_h = 34
        if has_hindi:
            draw_mixed_line(draw, (info_box[0], y, info_box[2], y + row_h), [(data["hindi_name"], "hi")], 34)
            y += row_h + 10
        draw_mixed_line(draw, (info_box[0], y, info_box[2], y + row_h), [(data["english_name"], "en")], 34)
        y += row_h + 10
        draw_mixed_line(draw, (info_box[0], y, info_box[2], y + row_h), [("जन्म तिथि/DOB: ", "hi"), (data["dob"], "en")], 34)
        y += row_h + 10
        gender_hi = GENDER_HI.get(data["gender"], "")
        draw_mixed_line(draw, (info_box[0], y, info_box[2], y + row_h), [(gender_hi + "/ ", "hi"), (data["gender"], "en")], 34)

    if print_mobile and data["mobile_number"] != "N/A":
        mobile_box = scale_box(MOBILE_BOX)
        mobile_font_size = int(MOBILE_FONT_SIZE * scale_y)
        draw_mixed_line(
            draw, mobile_box,
            [("Mobile No: ", "en"), (data["mobile_number"], "en")],
            mobile_font_size
        )

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


def build_back_card_image(pdf_bytes, password=None):
    data = extract_back_data(pdf_bytes, password)

    template = Image.open(BACK_TEMPLATE_PATH).convert("RGB")

    scale_x = template.width / BACK_TEMPLATE_W
    scale_y = template.height / BACK_TEMPLATE_H

    def scale_box(box):
        x0, y0, x1, y1 = box
        return (int(x0 * scale_x), int(y0 * scale_y), int(x1 * scale_x), int(y1 * scale_y))

    draw = ImageDraw.Draw(template)

    details_text = f"Details As On: {data['details_as_on']}"
    vfont = get_font("en", False, int(ISSUED_FONT_SIZE * scale_y))

    tmp = Image.new("RGBA", (900, 70), (255, 255, 255, 0))
    tdraw = ImageDraw.Draw(tmp)
    tbbox = tdraw.textbbox((0, 0), details_text, font=vfont)
    tw, th = tbbox[2] - tbbox[0], tbbox[3] - tbbox[1]
    tdraw.text((-tbbox[0], -tbbox[1]), details_text, font=vfont, fill="black")
    tmp = tmp.crop((0, 0, tw + 4, th + 4))
    rotated = tmp.transpose(Image.ROTATE_90)

    vx0 = int(BACK_VERTICAL_TEXT_X0 * scale_x)
    vx1 = int(BACK_VERTICAL_TEXT_X1 * scale_x)
    vy = (template.height - rotated.height) // 2
    vx = vx0 + ((vx1 - vx0) - rotated.width) // 2
    template.paste(rotated, (vx, vy), rotated)

    addr_font_size = int(BACK_ADDRESS_FONT_SIZE * scale_y)
    label_font_size = int(BACK_LABEL_FONT_SIZE * scale_y)

    combined_crop = crop_combined_address_block(pdf_bytes, password)
    combined_box = scale_box(COMBINED_ADDRESS_BOX)

    if combined_crop is not None:
        box_w = combined_box[2] - combined_box[0]
        box_h = combined_box[3] - combined_box[1]
        fitted = contain_fit(combined_crop, box_w, box_h)
        template.paste(fitted, (combined_box[0], combined_box[1]), fitted)
    else:
        if data["hindi_address"]:
            hindi_box = (combined_box[0], combined_box[1], combined_box[2], combined_box[1] + (combined_box[3] - combined_box[1]) // 2 - 10)
            english_box = (combined_box[0], hindi_box[3] + 20, combined_box[2], combined_box[3])
            draw_mixed_line(draw, (combined_box[0], combined_box[1] - 25, combined_box[2], combined_box[1] - 3), [("पता:", "hi")], label_font_size)
            draw_wrapped_text(draw, hindi_box, data["hindi_address"], get_font("hi", False, addr_font_size), line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y))
            draw_mixed_line(draw, (english_box[0], english_box[1] - 25, english_box[2], english_box[1] - 3), [("Address:", "en")], label_font_size)
            draw_wrapped_text(draw, english_box, data["english_address"], get_font("en", False, addr_font_size), line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y))
        else:
            draw_mixed_line(draw, (combined_box[0], combined_box[1] - 25, combined_box[2], combined_box[1] - 3), [("Address:", "en")], label_font_size)
            draw_wrapped_text(draw, combined_box, data["english_address"], get_font("en", False, addr_font_size), line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y))

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


@app.route("/generate-card", methods=["POST"])
def generate_card():
    if "pdf" not in request.files:
        return jsonify({"error": "PDF file is required (field name: pdf)"}), 400

    password = request.form.get("password", "").strip()
    print_mobile = request.form.get("print_mobile", "no").strip().lower() == "yes"
    pdf_file = request.files["pdf"]
    pdf_bytes = pdf_file.read()

    try:
        template = build_front_card_image(pdf_bytes, password, print_mobile)
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


@app.route("/generate-card-back", methods=["POST"])
def generate_card_back():
    if "pdf" not in request.files:
        return jsonify({"error": "PDF file is required (field name: pdf)"}), 400

    password = request.form.get("password", "").strip()
    pdf_file = request.files["pdf"]
    pdf_bytes = pdf_file.read()

    try:
        template = build_back_card_image(pdf_bytes, password)
    except pdfplumber.pdfminer.pdfdocument.PDFPasswordIncorrect:
        return jsonify({"error": "Password galat hai ya PDF unlock nahi ho paayi"}), 400
    except Exception as e:
        return jsonify({"error": f"Could not generate the back card: {str(e)}"}), 500

    output = io.BytesIO()
    template.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", as_attachment=False, download_name="aadhaar-card-back.png")


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Aadhaar Card Generator API is running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
