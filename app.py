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

GAP_HINDI_NAME_TO_ENGLISH_NAME = 24
GAP_ENGLISH_NAME_TO_DOB = 24
GAP_DOB_TO_GENDER = 24
GAP_GENDER_TO_MOBILE = 24

AADHAAR_NUM_BOX = (0, 485, TEMPLATE_W, 533)
VID_BOX = (0, 536, TEMPLATE_W, 562)

NAME_FONT_SIZE = 34
LABEL_FONT_SIZE = 34
MOBILE_FONT_SIZE = 36
AADHAAR_FONT_SIZE = 42
VID_FONT_SIZE = 26
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
BACK_ADDRESS_FONT_SIZE = 40
BACK_ADDRESS_LINE_GAP = 6

FONT_EN_REGULAR = "times.ttf"
FONT_EN_BOLD = "timesbd.ttf"
FONT_HI_REGULAR = "NotoSansDevanagari-Regular.ttf"
FONT_HI_BOLD = "NotoSansDevanagari-Bold.ttf"

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
DEVANAGARI_MATRA_VIRAMA_RE = re.compile(r"[\u093E-\u094C\u0900-\u0903\u094D]")


def fix_devanagari_spacing(text):
    # Sirf inline space/tab hi hatane hain, newline (line-breaks) kabhi
    # nahi — warna alag-alag lines (jaise Hindi naam aur English naam,
    # jo apni apni line par hote hain) galti se ek dusre se jud jaate
    # hain. Sirf " " ya tab, [ \t]+, hatate hain — \n ko haath nahi
    # lagate.
    return re.sub(r"([\u093E-\u094C\u0900-\u0903\u094D])[ \t]+", r"\1", text)


# ============================================================
# ADDRESS (Hindi + English) — dono ab IMAGE crop se aate hain,
# text-draw se nahi. (Details neeche functions ke comments me.)
# ============================================================
def _cluster_lines(chars, gap_threshold=4.0):
    if not chars:
        return []
    cs = sorted(chars, key=lambda c: c["top"])
    lines = []
    current = [cs[0]]
    current_min_top = cs[0]["top"]
    for c in cs[1:]:
        if c["top"] - current_min_top <= gap_threshold:
            current.append(c)
            current_min_top = min(current_min_top, c["top"])
        else:
            lines.append(current)
            current = [c]
            current_min_top = c["top"]
    lines.append(current)
    return lines


def _line_text(line_chars):
    return "".join(c["text"] for c in sorted(line_chars, key=lambda c: c["x0"]))


def _bbox_from_lines(line_list, pad_x=3, pad_top=2, pad_bottom=2):
    all_chars = [c for lc in line_list for c in lc]
    if not all_chars:
        return None
    x0 = min(c["x0"] for c in all_chars)
    x1 = max(c["x1"] for c in all_chars)
    top = min(c["top"] for c in all_chars)
    bottom = max(c["bottom"] for c in all_chars)
    return (x0 - pad_x, top - pad_top, x1 + pad_x, bottom + pad_bottom)


def crop_address_images(pdf_bytes, password=None, resolution=400):
    """
    Returns (hindi_image, english_image) — dono PIL Image (RGB) ya
    None agar us block ko reliably locate nahi kar paaye.

    Diagnosis (asli PDF pe test karke confirm kiya):
    1. Is font ka ToUnicode encoding kai jagah PERMANENTLY corrupt hai
       (kuch matras NULL character ban jaate hain) — isliye address
       text ki tarah dobara type karne ki jagah, PDF se seedha IMAGE
       crop kar ke card pe paste karte hain (Hindi aur English dono).
    2. Ek hi visual "line" PDF ke andar kabhi-kabhi 2-3 alag font-runs
       me todi hoti hai (Devanagari letters ek run, punctuation/number
       dusra run) jinke top-position me sirf 0.2-2 point ka farak
       hota hai. Isliye characters ko "line" me group karte waqt
       chhoti GAP-TOLERANCE use karte hain, warna beech ki lines
       silently kat jaati hain.
    3. Page par ek ROTATED ("Details As On: dd/mm/yyyy") text element
       bhi hota hai, jiske individual (rotated) characters ka x0/top
       kabhi-kabhi humare address-block ke bilkul paas girta hai aur
       galti se crop me shaamil ho jaata hai. pdfplumber har character
       par `upright` flag deta hai (rotated text ke liye False) —
       isse explicitly exclude karte hain.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
            page = pdf.pages[0]
            chars = page.chars
            if not chars:
                return None, None

            # Right-column (address side) chars — sirf UPRIGHT (non-
            # rotated) text, taaki "Details As On" jaisi rotated date
            # kabhi crop me na aaye.
            region_chars = [
                c for c in chars
                if c["x0"] >= 300 and c.get("upright", True)
            ]
            lines = _cluster_lines(region_chars)

            hindi_label_idx = None
            address_label_idx = None
            end_idx = None

            for i, lc in enumerate(lines):
                t = _line_text(lc)
                if hindi_label_idx is None and (("पत" in t and "आत्मज" not in t) or "आत्मज" in t):
                    hindi_label_idx = i
                if address_label_idx is None and "Address" in t:
                    address_label_idx = i

            if address_label_idx is None:
                return None, None

            for i in range(address_label_idx + 1, len(lines)):
                t = _line_text(lines[i]).replace(" ", "")
                if re.search(r"\d{4}\d{4}\d{4}", t):
                    end_idx = i
                    break
            if end_idx is None:
                end_idx = min(address_label_idx + 6, len(lines))

            hindi_img = None
            if hindi_label_idx is not None and hindi_label_idx < address_label_idx:
                hindi_lines = lines[hindi_label_idx + 1: address_label_idx]
                bbox = _bbox_from_lines(hindi_lines, pad_top=1, pad_bottom=1)
                if bbox and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                    cropped = page.crop(bbox).to_image(resolution=resolution)
                    hindi_img = cropped.original.convert("RGB")

            english_img = None
            english_lines = lines[address_label_idx + 1: end_idx]
            bbox = _bbox_from_lines(english_lines, pad_top=1, pad_bottom=1)
            if bbox and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                cropped = page.crop(bbox).to_image(resolution=resolution)
                english_img = cropped.original.convert("RGB")

            return hindi_img, english_img
    except Exception:
        return None, None


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


def fit_pair_shared_scale(img_a, img_b, box_a, box_b):
    """
    Hindi aur English address crops ko ALAG-ALAG apne box me
    "contain fit" karne se dono ka font-size mismatch ho jaata hai
    (jiska content chhota/kam-lines wala hai wo zyada bada scale ho
    jaata hai). Dono images SAME PDF se SAME resolution par crop hue
    hain, isliye unka original font-size already ek jaisa hai — hume
    bas ek hi SHARED scale factor lagana hai (jo dono ko apne-apne box
    me fit rakhe), taaki visual size hamesha match kare.
    """
    box_a_w, box_a_h = box_a[2] - box_a[0], box_a[3] - box_a[1]
    box_b_w, box_b_h = box_b[2] - box_b[0], box_b[3] - box_b[1]

    scales = []
    if img_a is not None:
        scales.append(min(box_a_w / img_a.width, box_a_h / img_a.height))
    if img_b is not None:
        scales.append(min(box_b_w / img_b.width, box_b_h / img_b.height))

    if not scales:
        return img_a, img_b
    shared_scale = min(scales)

    def resize(img):
        if img is None:
            return None
        new_w = max(1, int(img.width * shared_scale))
        new_h = max(1, int(img.height * shared_scale))
        return img.resize((new_w, new_h), Image.LANCZOS)

    return resize(img_a), resize(img_b)


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

    hindi_crop, english_crop = crop_address_images(pdf_bytes, password)

    if data["hindi_address"]:
        hindi_label_box = scale_box(HINDI_LABEL_BOX)
        hindi_addr_box = scale_box(HINDI_ADDRESS_BOX)
        english_label_box = scale_box(ENGLISH_LABEL_BOX)
        english_addr_box = scale_box(ENGLISH_ADDRESS_BOX)

        draw_mixed_line(draw, hindi_label_box, [("पता:", "hi")], label_font_size)
        draw_mixed_line(draw, english_label_box, [("Address:", "en")], label_font_size)

        if hindi_crop is not None and english_crop is not None:
            # Dono ko SAME shared-scale se resize karte hain, taaki
            # font-size visually match kare
            fitted_hindi, fitted_english = fit_pair_shared_scale(
                hindi_crop, english_crop, hindi_addr_box, english_addr_box
            )
            template.paste(fitted_hindi, (hindi_addr_box[0], hindi_addr_box[1]))
            template.paste(fitted_english, (english_addr_box[0], english_addr_box[1]))
        else:
            # Fallback: koi ek ya dono crop fail ho gaye to purana text-draw
            if hindi_crop is not None:
                box_w = hindi_addr_box[2] - hindi_addr_box[0]
                box_h = hindi_addr_box[3] - hindi_addr_box[1]
                ratio = hindi_crop.width / hindi_crop.height
                if box_w / box_h > ratio:
                    nh, nw = box_h, int(box_h * ratio)
                else:
                    nw, nh = box_w, int(box_w / ratio)
                template.paste(hindi_crop.resize((max(1, nw), max(1, nh)), Image.LANCZOS), (hindi_addr_box[0], hindi_addr_box[1]))
            else:
                draw_wrapped_text(
                    draw, hindi_addr_box, data["hindi_address"],
                    get_font("hi", False, addr_font_size),
                    line_gap=int(BACK_ADDRESS_LINE_GAP * scale_y)
                )

            if english_crop is not None:
                box_w = english_addr_box[2] - english_addr_box[0]
                box_h = english_addr_box[3] - english_addr_box[1]
                ratio = english_crop.width / english_crop.height
                if box_w / box_h > ratio:
                    nh, nw = box_h, int(box_h * ratio)
                else:
                    nw, nh = box_w, int(box_w / ratio)
                template.paste(english_crop.resize((max(1, nw), max(1, nh)), Image.LANCZOS), (english_addr_box[0], english_addr_box[1]))
            else:
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

        if english_crop is not None:
            box_w = only_addr_box[2] - only_addr_box[0]
            box_h = only_addr_box[3] - only_addr_box[1]
            ratio = english_crop.width / english_crop.height
            if box_w / box_h > ratio:
                nh, nw = box_h, int(box_h * ratio)
            else:
                nw, nh = box_w, int(box_w / ratio)
            template.paste(english_crop.resize((max(1, nw), max(1, nh)), Image.LANCZOS), (only_addr_box[0], only_addr_box[1]))
        else:
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
