import os
import re
import io
import pdfplumber
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

TEXT_COL_TOP = 158

# Har row ke beech ka gap ab ALAG-ALAG control hota hai (pehle sab
# rows ek hi ROW_GAP use karte the). Abhi sabki value same (34) rakhi
# hai jaisa pehle tha — ab tum inhe ek-ek karke independently badal
# sakte ho.
NAME_ROW_HEIGHT = 20          # Hindi Name aur English Name row ki height
LABEL_ROW_HEIGHT = 20         # DOB / Gender / Mobile row ki height

GAP_HINDI_NAME_TO_ENGLISH_NAME = 34   # Hindi Name -> English Name
GAP_ENGLISH_NAME_TO_DOB = 34          # English Name -> DOB
GAP_DOB_TO_GENDER = 34                # DOB -> Gender
GAP_GENDER_TO_MOBILE = 34             # Gender -> Mobile No

AADHAAR_NUM_BOX = (0, 485, TEMPLATE_W, 533)
VID_BOX = (0, 536, TEMPLATE_W, 562)

NAME_FONT_SIZE = 34
LABEL_FONT_SIZE = 34
MOBILE_FONT_SIZE = 36
AADHAAR_FONT_SIZE = 42
VID_FONT_SIZE = 22
ISSUED_FONT_SIZE = 20

# Front photo ki brightness kitni badhani hai (1.3 = 30% zyada)
PHOTO_BRIGHTNESS = 1.3

# ============================================================
# CONFIG — BACK CARD (Aadhaar)
# ============================================================
# Back ka blank template bhi repo mein isi tarah rakhna hai (app.py ke
# saath, usi folder mein) — filename yahan se badal sakte ho.
BACK_TEMPLATE_FILENAME = "aadhar_template_back.jpg"
BACK_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), BACK_TEMPLATE_FILENAME)

# Agar back template ka actual pixel size front se alag hai, to yahan
# badal dena — scale khud-ba-khud sambhal lega, coordinates proportion
# mein rahenge.
BACK_TEMPLATE_W, BACK_TEMPLATE_H = 1016, 638

# QR back template mein pehle se fixed/printed hai, isliye yahan draw
# nahi karna — sirf text fields likhne hain.

BACK_VERTICAL_TEXT_X0 = 16
BACK_VERTICAL_TEXT_X1 = 46

# Content column QR (jo roughly x:690-985, y:172-460 par hai) se pehle
# tak hi rakha hai, taaki kabhi overlap na ho.
BACK_CONTENT_X0 = 55
BACK_CONTENT_X1 = 670

HINDI_LABEL_BOX   = (BACK_CONTENT_X0, 178, BACK_CONTENT_X1, 206)
HINDI_ADDRESS_BOX = (BACK_CONTENT_X0, 208, BACK_CONTENT_X1, 307)   # ~3 lines tak jagah

ENGLISH_LABEL_BOX   = (BACK_CONTENT_X0, 321, BACK_CONTENT_X1, 349)  # Hindi block se 14px gap
ENGLISH_ADDRESS_BOX = (BACK_CONTENT_X0, 351, BACK_CONTENT_X1, 450)  # ~3 lines, Aadhaar box (485) se 35px gap

# Aadhaar Number aur VID — FRONT wale AADHAAR_NUM_BOX / VID_BOX / font
# size hi reuse kar rahe hain, jaisa maanga gaya tha (bilkul same
# size/font/jagah, taaki dono card consistent dikhein). Ye QR (jo y:460
# tak hai) ke neeche (y:485 se) shuru hote hain, isliye QR se bhi clear hai.

BACK_LABEL_FONT_SIZE = 26
BACK_ADDRESS_FONT_SIZE = 24
BACK_ADDRESS_LINE_GAP = 6

# ------------------------------------------------------------
# FONTS
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
# PDF SE DATA NIKALNA — FRONT
# ============================================================
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


def find_issued_date(lines, full_text):
    for i, line in enumerate(lines):
        cleaned = re.sub(r"[^a-z]", "", line.strip().lower())
        if cleaned == "deussi":
            if i > 0:
                candidate = lines[i - 1].strip()
                rev = candidate[::-1]
                if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", rev):
                    return rev

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

        m = re.search(r"Mobile:\s*(\d{10})", text)
        mobile_number = m.group(1) if m else "N/A"

        issued_date = find_issued_date(lines, text)

        photo_img = find_face_photo_image(page)

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


# ============================================================
# PDF SE DATA NIKALNA — BACK
# ============================================================
def find_details_as_on_date(lines, full_text):
    """
    Back ki "Details As On: DATE" wali vertical patti bhi PDF mein
    90-degree ghumi hoti hai, isliye text ULTA (reversed) nikalta hai —
    front ki "Aadhaar No. Issued" patti jaisa hi. Marker "sliateD"
    ("Details" ulta) dhoondh kar, uske thodi lines upar ulti date
    dhoondte hain.
    """
    for i, line in enumerate(lines):
        cleaned = re.sub(r"[^a-z]", "", line.strip().lower())
        if cleaned == "sliated":
            for k in range(max(0, i - 5), i):
                candidate = lines[k].strip()
                rev = candidate[::-1]
                if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", rev):
                    return rev

    matches = re.findall(r"\b(\d{4}/\d{2}/\d{1,2})\b", full_text)
    if len(matches) >= 2:
        return matches[1][::-1]
    elif matches:
        return matches[0][::-1]
    return "N/A"


def extract_back_data(pdf_bytes, password=None):
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        lines = text.split("\n")

        # --- Aadhaar number & VID: front jaisa hi (pehla match) ---
        m = re.search(r"\b(\d{4}\s\d{4}\s\d{4})\b", text)
        aadhaar_number = m.group(1) if m else "N/A"

        m = re.search(r"VID\s*:?\s*([\d ]{15,30}\d)", text)
        vid = re.sub(r"\s+", " ", m.group(1)).strip() if m else "N/A"

        # --- English name: sirf Hindi address ke beech se front-column
        # ka "naam" tukda saaf karne ke liye chahiye ---
        english_name = "N/A"
        for i, line in enumerate(lines):
            if line.strip() == "To":
                if i + 2 < len(lines):
                    english_name = lines[i + 2].strip()
                break

        # --- English address: back ke "Address:" paragraph ko seedha PDF
        # se nikalna reliable nahi hai (front column ke text ke saath
        # bahut zyada interleave/garbled ho jaata hai). Isliye UIDAI ke
        # FIXED back-address format se, front ke saaf structured fields
        # (S/O, Village, Post, VTC, PO, District, State, PIN) jodkar khud
        # banate hain: "S/O: X, Village- Y, Post- Z, VTC, PO: A, DIST: B,
        # State - PIN" — ye bilkul waisa hi banta hai jaisa asli card par
        # chhapa hota hai.
        def field(pattern):
            fm = re.search(pattern, text)
            return fm.group(1).strip().rstrip(",") if fm else ""

        guardian = field(r"S/O:\s*([^\n,]+)")
        village = field(r"Village-\s*([^\n,]+)")
        post = field(r"Post-\s*([^\n,]+)")
        vtc = field(r"VTC:\s*([^\n,]+)")
        po = field(r"PO:\s*([^\n,]+)")
        district = field(r"(?<!Sub )District:\s*([^\n,]+)")
        state = field(r"State:\s*([^\n,]+)")
        pincode = field(r"PIN Code:\s*(\d+)")

        english_parts = []
        if guardian: english_parts.append(f"S/O: {guardian}")
        if village: english_parts.append(f"Village- {village}")
        if post: english_parts.append(f"Post- {post}")
        if vtc: english_parts.append(vtc)
        if po: english_parts.append(f"PO: {po}")
        if district: english_parts.append(f"DIST: {district}")
        english_address = ", ".join(english_parts)
        tail = ", ".join(x for x in [state, pincode] if x)
        if tail:
            english_address = f"{english_address}, {state} - {pincode}" if (state and pincode) else f"{english_address}, {tail}"
        if not english_address:
            english_address = "N/A"

        # --- Hindi address: poore PDF text mein jahan bhi Devanagari
        # address wala block mile (sirf "पता" label ke baad hi nahi,
        # "आत्मज" jaisa guardian-marker milne par bhi try karte hain),
        # wahan se "- PIN" wali last line tak collect karte hain. Beech
        # mein aa gaye front-column ke English tukdo (naam, DOB) ko
        # surgically hata dete hain.
        def clean_address_line(raw_line):
            cleaned = raw_line
            if english_name and english_name != "N/A":
                cleaned = cleaned.replace(english_name, " ")
            cleaned = re.sub(r"जन्म.*?DOB:\s*\d{1,2}/\d{1,2}/\d{4}", " ", cleaned)
            cleaned = re.sub(r"/?\s*DOB:\s*\d{1,2}/\d{1,2}/\d{4}", " ", cleaned)
            cleaned = cleaned.replace("\x00", "")
            return re.sub(r"\s+", " ", cleaned).strip()

        def collect_hindi_block(start_idx, label_pattern=None):
            collected = []
            first_line = lines[start_idx]
            if label_pattern:
                parts = re.split(label_pattern, first_line, maxsplit=1)
                remainder = parts[1].strip() if len(parts) > 1 else ""
            else:
                remainder = first_line.strip()

            remainder = clean_address_line(remainder)
            if remainder and DEVANAGARI_RE.search(remainder):
                collected.append(remainder)

            if re.search(r"[\u0900-\u097F].*-\s*\d{6}\b", first_line):
                return re.sub(r"\s+", " ", " ".join(collected)).strip(" ,")

            for j in range(start_idx + 1, min(start_idx + 8, len(lines))):
                raw_line = lines[j]
                cleaned = clean_address_line(raw_line)
                if cleaned and DEVANAGARI_RE.search(cleaned):
                    collected.append(cleaned)
                if re.search(r"[\u0900-\u097F].*-\s*\d{6}\b", raw_line):
                    break

            return re.sub(r"\s+", " ", " ".join(collected)).strip(" ,")

        hindi_address = None

        # Try 1: "पता" label ke baad se
        for i, line in enumerate(lines):
            if "पता" in line:
                candidate = collect_hindi_block(i, label_pattern=r"पता\s*:?")
                if candidate:
                    hindi_address = candidate
                break

        # Try 2: agar upar se nahi mila, "आत्मज" (guardian marker) ke
        # aas-paas se dhoondo — PDF ke kisi bhi hisse mein ho sakta hai
        if not hindi_address:
            for i, line in enumerate(lines):
                if "आत्मज" in line:
                    candidate = collect_hindi_block(i)
                    if candidate:
                        hindi_address = candidate
                    break

        # Try 3: last resort — poore text mein jahan bhi Devanagari +
        # "- 6 digit PIN" wali line mile, wahi se peeche ki 1-2 Devanagari
        # lines bhi jod kar address bana lo
        if not hindi_address:
            for i, line in enumerate(lines):
                if re.search(r"[\u0900-\u097F].*-\s*\d{6}\b", line):
                    window_start = max(0, i - 2)
                    candidate = collect_hindi_block(window_start)
                    if candidate:
                        hindi_address = candidate
                    break

        # Fallback: Hindi address kahi bhi nahi mila to Hindi ki jagah
        # English address dikha do, aur English wali jagah khaali chhod do.
        if hindi_address:
            english_address_final = english_address
        else:
            hindi_address = english_address if english_address != "N/A" else "N/A"
            english_address_final = ""

        details_as_on = find_details_as_on_date(lines, text)

        return {
            "hindi_address": hindi_address,
            "english_address": english_address_final,
            "aadhaar_number": aadhaar_number,
            "vid": vid,
            "details_as_on": details_as_on,
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
    """
    Diye gaye box ki width mein text ko word-wrap karke, kai lines mein
    left-aligned draw karta hai (Hindi/English address paragraphs ke liye).
    """
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


def build_content_rows(has_hindi):
    """
    Rows ko upar se neeche stack karta hai, har pair ke beech apna
    ALAG gap use karke (GAP_HINDI_NAME_TO_ENGLISH_NAME, waghera).
    Return: (rows list, gender row ke neeche wala cursor — isi se
    Mobile row ki position GAP_GENDER_TO_MOBILE jodkar nikalti hai).
    """
    rows = []
    cursor = TEXT_COL_TOP

    if has_hindi:
        rows.append((CONTENT_X0, cursor, CONTENT_X1, cursor + NAME_ROW_HEIGHT))
        cursor = cursor + NAME_ROW_HEIGHT + GAP_HINDI_NAME_TO_ENGLISH_NAME

    rows.append((CONTENT_X0, cursor, CONTENT_X1, cursor + NAME_ROW_HEIGHT))
    cursor = cursor + NAME_ROW_HEIGHT + GAP_ENGLISH_NAME_TO_DOB

    rows.append((CONTENT_X0, cursor, CONTENT_X1, cursor + LABEL_ROW_HEIGHT))
    cursor = cursor + LABEL_ROW_HEIGHT + GAP_DOB_TO_GENDER

    rows.append((CONTENT_X0, cursor, CONTENT_X1, cursor + LABEL_ROW_HEIGHT))
    cursor = cursor + LABEL_ROW_HEIGHT

    return rows, cursor


GENDER_HI = {"MALE": "पुरुष", "FEMALE": "महिला", "TRANSGENDER": "ट्रांसजेंडर"}


# ============================================================
# FRONT CARD BUILD
# ============================================================
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

    has_hindi = data["hindi_name"] != "N/A"
    raw_rows, cursor_after_gender = build_content_rows(has_hindi)
    rows = [scale_box(r) for r in raw_rows]

    mobile_top = cursor_after_gender + GAP_GENDER_TO_MOBILE
    mobile_row_raw = (CONTENT_X0, mobile_top, CONTENT_X1, mobile_top + LABEL_ROW_HEIGHT)

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

    if print_mobile and data["mobile_number"] != "N/A":
        mobile_box = scale_box(mobile_row_raw)
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


# ============================================================
# BACK CARD BUILD
# ============================================================
def build_back_card_image(pdf_bytes, password=None):
    data = extract_back_data(pdf_bytes, password)

    template = Image.open(BACK_TEMPLATE_PATH).convert("RGB")

    scale_x = template.width / BACK_TEMPLATE_W
    scale_y = template.height / BACK_TEMPLATE_H

    def scale_box(box):
        x0, y0, x1, y1 = box
        return (int(x0 * scale_x), int(y0 * scale_y), int(x1 * scale_x), int(y1 * scale_y))

    draw = ImageDraw.Draw(template)

    # ---------- VERTICAL "Details As On: DATE" (front ki patti jaisi hi) ----------
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
    vx = vx0 + ((vx1 - vx0) - rotated.width) // 2
    vy = (template.height - rotated.height) // 2
    template.paste(rotated, (vx, vy), rotated)

    # ---------- HINDI ADDRESS ----------
    hindi_label_box = scale_box(HINDI_LABEL_BOX)
    hindi_addr_box = scale_box(HINDI_ADDRESS_BOX)
    label_font_size = int(BACK_LABEL_FONT_SIZE * scale_y)
    addr_font_size = int(BACK_ADDRESS_FONT_SIZE * scale_y)

    draw_mixed_line(draw, hindi_label_box, [("पता:", "hi")], label_font_size)
    draw_wrapped_text(
        draw, hindi_addr_box, data["hindi_address"],
        get_font("hi", False, addr_font_size),
        line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y)
    )

    # ---------- ENGLISH ADDRESS ----------
    english_label_box = scale_box(ENGLISH_LABEL_BOX)
    english_addr_box = scale_box(ENGLISH_ADDRESS_BOX)

    draw_mixed_line(draw, english_label_box, [("Address:", "en")], label_font_size)
    draw_wrapped_text(
        draw, english_addr_box, data["english_address"],
        get_font("en", False, addr_font_size),
        line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y)
    )

    # ---------- AADHAAR NUMBER + VID — front jaisi hi jagah/size/font ----------
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
# ENDPOINTS
# ============================================================
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
