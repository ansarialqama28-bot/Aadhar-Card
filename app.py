import os
import re
import io
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

TEXT_COL_TOP = 158

NAME_ROW_HEIGHT = 20
LABEL_ROW_HEIGHT = 20

GAP_HINDI_NAME_TO_ENGLISH_NAME = 14
GAP_ENGLISH_NAME_TO_DOB = 14
GAP_DOB_TO_GENDER = 14
GAP_GENDER_TO_MOBILE = 14

AADHAAR_NUM_BOX = (0, 485, TEMPLATE_W, 533)
VID_BOX = (0, 536, TEMPLATE_W, 562)

NAME_FONT_SIZE = 34
LABEL_FONT_SIZE = 34
MOBILE_FONT_SIZE = 36
AADHAAR_FONT_SIZE = 42
VID_FONT_SIZE = 22
ISSUED_FONT_SIZE = 20

PHOTO_BRIGHTNESS = 1.3

# ============================================================
# CONFIG — BACK CARD (Aadhaar)
# ============================================================
BACK_TEMPLATE_FILENAME = "aadhar_template_back.jpg"
BACK_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), BACK_TEMPLATE_FILENAME)

BACK_TEMPLATE_W, BACK_TEMPLATE_H = 1016, 638

BACK_VERTICAL_TEXT_X0 = 16
BACK_VERTICAL_TEXT_X1 = 46

BACK_CONTENT_X0 = 55
BACK_CONTENT_X1 = 670

HINDI_LABEL_BOX   = (BACK_CONTENT_X0, 178, BACK_CONTENT_X1, 206)
HINDI_ADDRESS_BOX = (BACK_CONTENT_X0, 208, BACK_CONTENT_X1, 307)

ENGLISH_LABEL_BOX   = (BACK_CONTENT_X0, 321, BACK_CONTENT_X1, 349)
ENGLISH_ADDRESS_BOX = (BACK_CONTENT_X0, 351, BACK_CONTENT_X1, 450)

BACK_LABEL_FONT_SIZE = 26
BACK_ADDRESS_FONT_SIZE = 24
BACK_ADDRESS_LINE_GAP = 6

FONT_EN_REGULAR = "times.ttf"
FONT_EN_BOLD = "timesbd.ttf"
FONT_HI_REGULAR = "NotoSansDevanagari-Regular.ttf"
FONT_HI_BOLD = "NotoSansDevanagari-Bold.ttf"

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
DEVANAGARI_MATRA_VIRAMA_RE = re.compile(r"[\u093E-\u094C\u0900-\u0903\u094D]")

# Characters that can NEVER be the first character of a Devanagari word
# (dependent vowel signs/matras, virama, nukta, anusvara/candrabindu/
# visarga, Vedic accents, invisible joiners). A "space" is only allowed
# to be inserted directly BEFORE a character that is NOT in this set —
# this is what stops words from getting split in the middle of a
# conjunct or matra, which is what broke the earlier character-percent
# approach.
DEVANAGARI_NONSTART_RE = re.compile(
    r"[\u0900-\u0903\u093C\u093E-\u094D\u0951-\u0957\u200C\u200D]"
)


def fix_devanagari_spacing(text):
    # Sirf inline space/tab hi hatane hain, newline (line-breaks) kabhi
    # nahi — warna alag-alag lines (jaise Hindi naam aur English naam,
    # jo apni apni line par hote hain) galti se ek dusre se jud jaate
    # hain. Sirf " " ya tab, [ \t]+, hatate hain — \n ko haath nahi
    # lagate.
    return re.sub(r"([\u093E-\u094C\u0900-\u0903\u094D])[ \t]+", r"\1", text)


# ============================================================
# NAYA (v2): Comma-segment + valid-word-boundary based Hindi
# spacing fix.
#
# Purani approach (character ka % position copy karna) tootti thi
# kyunki Hindi conjuncts/matras ka character-count English letters se
# match nahi karta — thoda sa mismatch hote hi space ekdum galat jagah
# (matra ke beech me) chala jaata tha.
#
# Naya tareeka:
#   1. Hindi aur English address ko COMMA se segments me todte hain.
#      Agar segment-count match nahi hui, kuch bhi nahi badalte
#      (safe fallback — purana Hindi address hi wapas).
#   2. Har matching segment-pair ke liye: Hindi segment ke saare
#      spaces hata kar ek compact string banate hain, phir sirf UN
#      positions par space insert karte hain jo Devanagari ke hisaab
#      se ek naye word ki VALID shuruaat ho sakti hain (matra/virama
#      ke turant pehle kabhi nahi) — English word-lengths ka ratio
#      sirf ye decide karne ke liye use hota hai ki in valid
#      positions me se KAUNSI sahi jagah hai.
# ============================================================
def _align_segment_spacing(hindi_seg, english_seg):
    compact = re.sub(r"\s+", "", hindi_seg)
    if not compact:
        return hindi_seg

    english_words = english_seg.split()
    if len(english_words) <= 1:
        return compact  # single word — koi internal space chahiye hi nahi

    n = len(compact)
    weights = [len(w) for w in english_words]
    total_weight = sum(weights) or 1

    # Har word-boundary ki target (proportional) position nikalna
    target_positions = []
    cum = 0
    for w in weights[:-1]:
        cum += w
        target_positions.append(round((cum / total_weight) * n))

    # Sirf wahi positions valid hain jahan naya word shuru ho sakta hai
    valid_positions = [
        i for i in range(1, n)
        if not DEVANAGARI_NONSTART_RE.match(compact[i])
    ]
    if not valid_positions:
        return compact  # koi safe break point nahi mila — chhedo mat

    chosen = []
    for target in target_positions:
        candidates = sorted(valid_positions, key=lambda p: abs(p - target))
        for c in candidates:
            if c not in chosen and (not chosen or c > chosen[-1]):
                chosen.append(c)
                break

    chosen = sorted(set(chosen))
    result_chars = list(compact)
    for idx in reversed(chosen):
        result_chars.insert(idx, " ")

    return "".join(result_chars)


def align_hindi_spacing_using_english(hindi_addr, english_addr):
    if not hindi_addr or hindi_addr == "N/A":
        return hindi_addr
    if not english_addr or english_addr == "N/A":
        return hindi_addr

    hindi_segments = [s.strip() for s in hindi_addr.split(",")]
    english_segments = [s.strip() for s in english_addr.split(",")]

    # Segment count match nahi hua to risk nahi lete — original wapas
    if len(hindi_segments) != len(english_segments):
        return hindi_addr

    fixed_segments = []
    for h_seg, e_seg in zip(hindi_segments, english_segments):
        if not h_seg:
            fixed_segments.append(h_seg)
            continue
        fixed_segments.append(_align_segment_spacing(h_seg, e_seg))

    result = ", ".join(s for s in fixed_segments if s)
    return result if result else hindi_addr


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
    """
    "DOB:" marker line dhoondh kar, uske turant upar wali 1-2 lines ko
    "naam" maanta hai (1 agar sirf English hai, 2 agar Hindi+English
    dono). Ye "To" ke turant baad wali line lene se ZYADA reliable hai,
    kyunki address ka pehla hissa kabhi kabhi label ke bina (jaise
    "Rahika Tola,") bhi aata hai aur naam jaisa dikh sakta hai — DOB ke
    bilkul upar wali line hamesha naam hi hoti hai, kabhi address nahi.
    """
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

    # Kuch PDFs "पता" likhte hain, kuch "पत्ता" (doubled-त, Maharashtra
    # style) — dono ko check karte hain.
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

    # v2 fix: comma-segment + valid-word-boundary based realignment
    if hindi_address and english_address:
        hindi_address = align_hindi_spacing_using_english(hindi_address, english_address)

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


def build_content_rows(has_hindi):
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
    vx = vx0 + ((vx1 - vx0) - rotated.width) // 2
    vy = (template.height - rotated.height) // 2
    template.paste(rotated, (vx, vy), rotated)

    label_font_size = int(BACK_LABEL_FONT_SIZE * scale_y)
    addr_font_size = int(BACK_ADDRESS_FONT_SIZE * scale_y)

    if data["hindi_address"]:
        hindi_label_box = scale_box(HINDI_LABEL_BOX)
        hindi_addr_box = scale_box(HINDI_ADDRESS_BOX)
        draw_mixed_line(draw, hindi_label_box, [("पता:", "hi")], label_font_size)
        draw_wrapped_text(
            draw, hindi_addr_box, data["hindi_address"],
            get_font("hi", False, addr_font_size),
            line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y)
        )

        english_label_box = scale_box(ENGLISH_LABEL_BOX)
        english_addr_box = scale_box(ENGLISH_ADDRESS_BOX)
        draw_mixed_line(draw, english_label_box, [("Address:", "en")], label_font_size)
        draw_wrapped_text(
            draw, english_addr_box, data["english_address"],
            get_font("en", False, addr_font_size),
            line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y)
        )
    else:
        only_label_box = scale_box(HINDI_LABEL_BOX)
        combined_box_raw = (HINDI_ADDRESS_BOX[0], HINDI_ADDRESS_BOX[1], ENGLISH_ADDRESS_BOX[2], ENGLISH_ADDRESS_BOX[3])
        only_addr_box = scale_box(combined_box_raw)

        draw_mixed_line(draw, only_label_box, [("Address:", "en")], label_font_size)
        draw_wrapped_text(
            draw, only_addr_box, data["english_address"],
            get_font("en", False, addr_font_size),
            line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y)
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
