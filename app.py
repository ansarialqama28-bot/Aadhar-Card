import os
import re
import io
import numpy as np
import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
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

FRONT_INFO_BOX = (CONTENT_X0, 155, CONTENT_X1, 345)

MOBILE_BOX = (CONTENT_X0 + 13, 333, CONTENT_X1, 380)

AADHAAR_NUM_BOX = (0, 485, TEMPLATE_W, 533)
VID_BOX = (0, 536, TEMPLATE_W, 562)

MOBILE_FONT_SIZE = 32
AADHAAR_FONT_SIZE = 42
VID_FONT_SIZE = 26
ISSUED_FONT_SIZE = 20
LABEL_FONT_SIZE = 34

PHOTO_BRIGHTNESS = 1.3

FRONT_INFO_CROP_LEFT = 130
FRONT_INFO_CROP_TOP = 608
FRONT_INFO_CROP_RIGHT_BASE = 222
FRONT_INFO_CROP_RIGHT_EXTRA_DEFAULT = 8
FRONT_INFO_CROP_RIGHT_EXTRA_NO_HINDI = 10
FRONT_INFO_CROP_BOTTOM_MAX = 700
FRONT_INFO_CROP_RESOLUTION = 500
RED_LINE_MARGIN_PX = 8
FRONT_INFO_PHOTO_GAP_PT = 3        # photo ke right edge ke baad itna gap chhod ke crop shuru
FRONT_INFO_TOP_ABOVE_PHOTO_PT = 0  # photo ke top se kitna upar se scan shuru kare
FRONT_MOBILE_BOTTOM_LIMIT = 378    # Mobile line ka neeche ka kinara isse neeche na jaye (template px, red box ke upar)
FRONT_MOBILE_DESCENT_RATIO = 0.25  # baseline ke neeche descender ke liye font size ka hissa
FRONT_INFO_MAX_FONT_PX = 31         # label (name/DOB/gender) ka max font size, card px me
FRONT_INFO_SHRINK = 0.95           # Mobile ke saath poora block thoda chhota (size + gap)
FRONT_INFO_MAX_GAP_PT = 14         # text columns ke beech is se bada gap = block khatam
FRONT_INFO_INK_PAD_PX = 4          # tight text crop ke charon taraf padding

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
ADDR_MIN_X_PT = 250          # 'Address:' label is x se right me hi maana jayega (address column)
ADDR_MAX_LINE_GAP_PT = 22    # lines ke beech is se bada gap = block khatam
ADDR_CROP_PAD_PT = 2         # detected text bbox ke charon taraf padding

# QR code — real, PDF se seedha crop hoke aata hai (regenerate nahi
# hota, kyunki uske andar UIDAI ka digitally-signed data hota hai)
QR_CROP_PDF_BBOX = (461.4, 601.6, 558.5, 696.5)
QR_CROP_RESOLUTION = 600
QR_MIN_RATIO, QR_MAX_RATIO = 0.9, 1.1   # square image = QR
QR_OUTPUT_MIN_PX = 800                  # QR output kam se kam itne px ka
QR_QUIET_ZONE_RATIO = 0.05              # QR ke charon taraf white margin
QR_MIN_DENSITY = 0.25                   # QR rows/cols me kam se kam itni ink density
QR_BAND_TOL_PT = 80                     # photo ke top se itna upar/neeche tak same card-band maane
QR_MIN_SIZE_PT = 50                     # isse chhoti square images (logo/icon) ignore

BACK_QR_BOX = (700, 180, 985, 460)

# ============================================================
# CONFIG — A4 PRINT LAYOUT
# ============================================================
A4_DPI = 300
A4_WIDTH_PX = int(8.27 * A4_DPI)
A4_HEIGHT_PX = int(11.69 * A4_DPI)
PRINT_CARD_GAP_PX = 60          # front aur back card ke beech gap
PRINT_CROP_MARK_LEN = 18        # corner crop-marks ki length (cutting guide ke liye)

FONT_EN_REGULAR = "times.ttf"
FONT_EN_BOLD = "timesbd.ttf"
FONT_HI_REGULAR = "NotoSansDevanagari-Regular.ttf"
FONT_HI_BOLD = "NotoSansDevanagari-Bold.ttf"

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
DEVANAGARI_MATRA_VIRAMA_RE = re.compile(r"[\u093E-\u094C\u0900-\u0903\u094D]")
PIN_END_RE = re.compile(r"-\s*\d{6}\b")
GENDER_HI = {"MALE": "पुरुष", "FEMALE": "महिला", "TRANSGENDER": "ट्रांसजेंडर"}


# ============================================================
# HELPERS — text
# ============================================================
def fix_devanagari_spacing(text):
    return re.sub(r"([\u093E-\u094C\u0900-\u0903\u094D])[ \t]+", r"\1", text)


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
    candidates = [path]
    if lang != "hi":
        # Times na mile to usi jaise serif fonts (PDF me LiberationSerif hota hai)
        candidates += (
            ["LiberationSerif-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"]
            if bold else
            ["LiberationSerif-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"]
        )
    for cand in candidates:
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def extract_aadhaar_number(text):
    """
    Aadhaar number ko dono format me support karta hai:
    1. Unmasked: poore 12 digits, jaise "7111 9373 9991"
    2. Masked: pehle 8 characters "X" (ya "x") hote hain, aakhri
       me 4 real digits — jaise "XXXX XXXX 1234".

    Masked wale ke liye robust regex use kiya hai — PDF font ke
    metrics kabhi-kabhi X characters ke BEECH me bhi stray space
    daal dete hain (jaisa Hindi text me dekha tha), isliye
    "(?:[Xx]\\s*){8}" pattern har X ke turant baad OPTIONAL space
    allow karta hai — chahe PDF "XXXX XXXX", "XXXXXXXX", ya
    "X X X X X X X X" kisi bhi tarah se de, sab match ho jaayega.
    """
    m = re.search(r"\b(\d{4}\s\d{4}\s\d{4})\b", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    m = re.search(r"((?:[Xx]\s*){8})\s*(\d{4})\b", text)
    if m:
        return f"XXXX XXXX {m.group(2)}"

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


def find_issued_date(full_text):
    m = re.search(r"issued\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", full_text, re.IGNORECASE)
    if m:
        return m.group(1)
    return "N/A"


def find_details_as_on_date(lines, full_text):
    m = re.search(r"Details\s+as\s+on\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", full_text, re.IGNORECASE)
    if m:
        return m.group(1)
    return "N/A"


# ============================================================
# HELPERS — image crop
# ============================================================
def make_white_transparent(img, threshold=245):
    img = img.convert("RGBA")
    arr = np.array(img)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    white_mask = (r >= threshold) & (g >= threshold) & (b >= threshold)
    arr[..., 3] = np.where(white_mask, 0, 255)
    return Image.fromarray(arr, mode="RGBA")


def detect_red_line_row(img):
    arr = np.array(img.convert("RGB"))
    h, w, _ = arr.shape
    r, g, b = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)

    red_mask = (r > 150) & (g < 110) & (b < 110)
    red_fraction_per_row = red_mask.sum(axis=1) / w

    for row in range(h):
        if red_fraction_per_row[row] > 0.5:
            return row
    return None


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


def _group_words_into_lines(words, tol=3.0):
    """Words ko unke 'top' ke hisaab se lines me group karta hai (upar se neeche)."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1]["top"] - w["top"]) <= tol:
            ln = lines[-1]
            ln["words"].append(w)
            ln["top"] = min(ln["top"], w["top"])
            ln["bottom"] = max(ln["bottom"], w["bottom"])
        else:
            lines.append({"top": w["top"], "bottom": w["bottom"], "words": [w]})
    for ln in lines:
        ln["text"] = " ".join(w["text"] for w in sorted(ln["words"], key=lambda w: w["x0"]))
        ln["x0"] = min(w["x0"] for w in ln["words"])
        ln["x1"] = max(w["x1"] for w in ln["words"])
    return lines


def detect_address_bbox(page):
    """
    Back card ka address block (Hindi + English) text detect karke dhoondhta hai.
      - Right limit : QR ka left edge
      - English start: "Address:" label
      - Hindi start  : us label ke upar ki contiguous Devanagari/text lines
      - End          : English address ki woh line jisme 6-digit PIN ho
    Return (x0, top, x1, bottom) PDF points me, ya None.
    """
    qr_bbox = find_qr_bbox_from_images(page) or QR_CROP_PDF_BBOX
    qr_left, qr_top = qr_bbox[0], qr_bbox[1]
    right_limit = qr_left - 2

    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    words = [w for w in words if w["x1"] <= right_limit + 1]

    labels = [w for w in words
              if w["text"].lower().startswith("address") and w["x0"] >= ADDR_MIN_X_PT]
    if not labels:
        return None
    # QR ke sabse paas (vertically) wala "Address:" label
    label = min(labels, key=lambda w: abs(w["top"] - qr_top))
    col_left = label["x0"] - 1

    col_words = [w for w in words if w["x0"] >= col_left - 1 and w["x1"] <= right_limit + 1]
    lines = _group_words_into_lines(col_words)

    idx = next((i for i, ln in enumerate(lines) if abs(ln["top"] - label["top"]) <= 3), None)
    if idx is None:
        return None

    # neeche: PIN wali line tak (English end)
    end_idx = idx
    for j in range(idx, len(lines)):
        if j > idx and lines[j]["top"] - lines[j - 1]["bottom"] > ADDR_MAX_LINE_GAP_PT:
            break
        end_idx = j
        if re.search(r"\b\d{6}\b", lines[j]["text"]):
            break

    # upar: Hindi block tak (contiguous lines)
    start_idx = idx
    for j in range(idx - 1, -1, -1):
        if lines[j + 1]["top"] - lines[j]["bottom"] > ADDR_MAX_LINE_GAP_PT:
            break
        start_idx = j

    block = lines[start_idx:end_idx + 1]
    x0 = min(ln["x0"] for ln in block)
    x1 = min(max(ln["x1"] for ln in block), right_limit)
    top = block[0]["top"]
    bottom = block[-1]["bottom"]
    if x1 <= x0 or bottom <= top:
        return None
    return (x0, top, x1, bottom)


def crop_combined_address_block(pdf_bytes, password=None):
    """
    Hindi + English address ko text-detect karke crop karta hai
    (fixed coordinates nahi). Detect fail ho to purane fixed bbox ko
    search-area maan kar usme se ink-tight crop leta hai.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
            page = pdf.pages[0]

            bbox = None
            try:
                bbox = detect_address_bbox(page)
            except Exception:
                bbox = None

            if bbox is not None:
                p = ADDR_CROP_PAD_PT
                bbox = (bbox[0] - p, bbox[1] - p, bbox[2] + p, bbox[3] + p)
            else:
                bbox = ADDR_CROP_PDF_BBOX

            safe_bbox = (
                max(0, bbox[0]), max(0, bbox[1]),
                min(page.width, bbox[2]), min(page.height, bbox[3])
            )
            if safe_bbox[2] <= safe_bbox[0] or safe_bbox[3] <= safe_bbox[1]:
                return None

            img = page.crop(safe_bbox).to_image(resolution=ADDR_CROP_RESOLUTION).original.convert("RGB")

            ink = find_ink_bbox(img)
            if ink is not None:
                pad = FRONT_INFO_INK_PAD_PX
                img = img.crop((max(0, ink[0] - pad), max(0, ink[1] - pad),
                                min(img.width, ink[2] + pad), min(img.height, ink[3] + pad)))
            return make_white_transparent(img)
    except Exception:
        return None


def get_face_photo_bbox(page):
    """PDF page par chehre wali photo ka bbox (x0, top, x1, bottom) deta hai."""
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
    return (im["x0"], im["top"], im["x1"], im["bottom"])


def find_ink_bbox(img, bg_threshold=235):
    """Image me non-white (text) pixels ka tight bounding box: (l, t, r, b)."""
    arr = np.array(img.convert("RGB"))
    ink = (arr < bg_threshold).any(axis=2)
    rows = np.where(ink.any(axis=1))[0]
    cols = np.where(ink.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def trim_right_by_column_gap(img, max_gap_px, bg_threshold=235):
    """
    Left se start karke text ke columns follow karta hai; jaise hi
    max_gap_px se bada khaali gap mile, wahin right side kaat deta hai
    (taaki side ka koi aur block crop me na aa jaye).
    """
    arr = np.array(img.convert("RGB"))
    ink_cols = (arr < bg_threshold).any(axis=2).any(axis=0)
    last_ink = None
    gap = 0
    for x, has_ink in enumerate(ink_cols):
        if has_ink:
            last_ink = x
            gap = 0
        elif last_ink is not None:
            gap += 1
            if gap > max_gap_px:
                break
    if last_ink is None:
        return img
    return img.crop((0, 0, last_ink + 1, img.height))


def crop_front_info_block(pdf_bytes, password=None, has_hindi_name=True, meta_out=None):
    """
    Auto-detect crop (fixed coordinates nahi):
      - LEFT   : photo ke right edge ke baad se
      - BOTTOM : red line ke upar tak
      - TOP/RIGHT/LEFT-tight : text ke asli pixels detect karke tight crop
    Photo na mile to purane fixed values fallback me use hote hain.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
            page = pdf.pages[0]

            photo_bbox = get_face_photo_bbox(page)
            if photo_bbox is not None:
                left = photo_bbox[2] + FRONT_INFO_PHOTO_GAP_PT
                top = max(0, photo_bbox[1] - FRONT_INFO_TOP_ABOVE_PHOTO_PT)
            else:
                left = FRONT_INFO_CROP_LEFT
                top = FRONT_INFO_CROP_TOP

            right = ADDR_CROP_PDF_BBOX[0] - 2
            generous_bbox = (
                max(0, left),
                max(0, top),
                min(page.width, right),
                min(page.height, FRONT_INFO_CROP_BOTTOM_MAX),
            )
            if generous_bbox[2] <= generous_bbox[0] or generous_bbox[3] <= generous_bbox[1]:
                return None

            cropped = page.crop(generous_bbox).to_image(resolution=FRONT_INFO_CROP_RESOLUTION)
            img = cropped.original.convert("RGB")

            # 1) Neeche se: red line ke upar tak
            red_row = detect_red_line_row(img)
            if red_row is not None:
                trim_to = max(1, red_row - RED_LINE_MARGIN_PX)
                img = img.crop((0, 0, img.width, trim_to))

            limit_pt = generous_bbox[1] + img.height * 72.0 / FRONT_INFO_CROP_RESOLUTION  # red line ke upar ki limit

            # 2) Right: bade khaali gap par ruk jao (side ke dusre block se bachne ke liye)
            gap_px = int(FRONT_INFO_MAX_GAP_PT * FRONT_INFO_CROP_RESOLUTION / 72)
            img = trim_right_by_column_gap(img, gap_px)

            # 3) Text ke asli pixels par tight crop (top/left/right/bottom)
            bbox = find_ink_bbox(img)
            if bbox is None:
                return None
            pad = FRONT_INFO_INK_PAD_PX
            l = max(0, bbox[0] - pad)
            t = max(0, bbox[1] - pad)
            r = min(img.width, bbox[2] + pad)
            b = min(img.height, bbox[3] + pad)
            img = img.crop((l, t, r, b))

            # 4) Lines ka asli font size + line pitch PDF se (Mobile line same size/gap me
            #    draw karne ke liye). Baseline = chars ke text-matrix se.
            if meta_out is not None:
                try:
                    px_per_pt = FRONT_INFO_CROP_RESOLUTION / 72.0
                    sub = page.crop(generous_bbox)
                    groups = []
                    cand = [ch for ch in sub.chars
                            if ch.get("text", "").strip() and ch["text"].isascii()
                            and page.height - ch["matrix"][5] <= limit_pt]
                    if cand:
                        # Side ka chhota (vertical "Aadhaar No. Issued") text ignore: sirf asli line-size wale chars
                        max_sz = max(ch["size"] for ch in cand)
                        cand = [ch for ch in cand if ch["size"] >= 0.7 * max_sz]
                    for ch in cand:
                        base = page.height - ch["matrix"][5]
                        for g in groups:
                            if abs(g["base"] - base) <= 2.0:
                                g["sizes"].append(ch["size"])
                                break
                        else:
                            groups.append({"base": base, "sizes": [ch["size"]]})
                    groups.sort(key=lambda g: g["base"])
                    if len(groups) >= 2:
                        bases = [g["base"] for g in groups]
                        pitches = sorted(bases[i + 1] - bases[i] for i in range(len(bases) - 1))
                        pitch_pt = pitches[len(pitches) // 2]
                        sizes = sorted(sz for g in groups for sz in g["sizes"])
                        size_pt = sizes[len(sizes) // 2]
                        meta_out.update({
                            "font_px": size_pt * px_per_pt,
                            "pitch_px": pitch_pt * px_per_pt,
                            "last_baseline_px": (bases[-1] - generous_bbox[1]) * px_per_pt - t,
                        })
                except Exception:
                    pass

            return make_white_transparent(img)
    except Exception:
        return None


def estimate_info_meta_from_image(info_crop):
    """
    PDF se font metrics na mile to label ki image se hi line-pitch aur font size
    estimate karta hai (text ke row-bands se). Mobile ka size phir bhi label ke
    size ke saath auto-adjust hota hai, kabhi fixed nahi.
    """
    try:
        alpha = np.array(info_crop.split()[-1]) > 40
        rows = np.where(alpha.any(axis=1))[0]
        if rows.size == 0:
            return {}
        bands, start, prev = [], rows[0], rows[0]
        for r in rows[1:]:
            if r - prev > 2:
                bands.append((start, prev))
                start = r
            prev = r
        bands.append((start, prev))
        if len(bands) < 2:
            return {}
        centers = [(a + b) / 2.0 for a, b in bands]
        diffs = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
        band_pitch = diffs[len(diffs) // 2]
        # Hindi glyphs bands ko thoda lamba dikhate hain; standard Aadhaar layout par calibrated:
        pitch = band_pitch * 0.918                      # asli baseline-to-baseline gap
        font_px = band_pitch * 0.579                    # asli font size (em)
        last_baseline = bands[-1][1] - 0.138 * band_pitch   # last band ka neeche ka kinara -> baseline
        return {"font_px": float(font_px), "pitch_px": float(pitch), "last_baseline_px": float(last_baseline)}
    except Exception:
        return {}


def find_qr_bbox_from_images(page):
    """
    PDF ke embedded images me se square wali (QR) image dhoondhta hai.
    Photo (portrait ratio) aur chhote logos ignore hote hain.

    Page par ek se zyada QR-jaisi square images ho sakti hain (jaise upar
    letter wala QR), isliye card wali band ka QR chunte hain:
      - photo mile to: jo square image photo ki hi vertical band me ho
      - warna: page me sabse neeche wali square image
    Return: (x0, top, x1, bottom) ya None.
    """
    candidates = []
    for im in page.images:
        w = im["x1"] - im["x0"]
        h = im["bottom"] - im["top"]
        if w <= 0 or h <= 0:
            continue
        ratio = w / h
        if QR_MIN_RATIO <= ratio <= QR_MAX_RATIO and w >= QR_MIN_SIZE_PT and h >= QR_MIN_SIZE_PT:
            candidates.append(im)
    if not candidates:
        return None

    photo = get_face_photo_bbox(page)
    if photo is not None:
        same_band = [im for im in candidates if abs(im["top"] - photo[1]) <= QR_BAND_TOL_PT]
        if same_band:
            candidates = same_band

    # photo ke saath same band me bhi kai ho to sabse right wali (QR card ke right side me hota hai)
    im = max(candidates, key=lambda c: (c["top"], c["x0"]))
    if photo is not None:
        im = max(candidates, key=lambda c: c["x0"])
    return (im["x0"], im["top"], im["x1"], im["bottom"])


def squarify_ink(img, bg_threshold=200, quiet_ratio=0.04):
    """
    Image me dark pixels (QR modules) ka tight bbox nikaal kar, usko
    perfect square banata hai (quiet-zone padding ke saath). Bahar ka
    koi text/line crop me nahi aata kyunki sirf ink-bbox ke andar ka
    square liya jata hai.
    """
    arr = np.array(img.convert("L"))
    ink = arr < bg_threshold
    # QR dense hota hai; aas-paas ke chhote text/lines sparse. Isliye sirf wahi
    # rows/columns lete hain jinme ink ki density kaafi ho.
    row_density = ink.sum(axis=1) / ink.shape[1]
    col_density = ink.sum(axis=0) / ink.shape[0]
    rows = np.where(row_density >= QR_MIN_DENSITY)[0]
    cols = np.where(col_density >= QR_MIN_DENSITY)[0]
    if rows.size == 0 or cols.size == 0:
        return img
    l, r = int(cols[0]), int(cols[-1]) + 1
    t, b = int(rows[0]), int(rows[-1]) + 1
    side = max(r - l, b - t)
    pad = int(side * quiet_ratio)
    side += 2 * pad
    cx, cy = (l + r) // 2, (t + b) // 2
    x0, y0 = cx - side // 2, cy - side // 2
    canvas = Image.new("RGB", (side, side), "white")
    src_box = (max(0, x0), max(0, y0), min(img.width, x0 + side), min(img.height, y0 + side))
    piece = img.convert("RGB").crop(src_box)
    canvas.paste(piece, (src_box[0] - x0, src_box[1] - y0))

    # Quiet-zone me aas-paas ke text/lines ke stray marks aa gaye hon to saaf kar do:
    # QR ke dense area ke bahar sab kuch white.
    clean = Image.new("RGB", canvas.size, "white")
    qr_box = (l - x0, t - y0, r - x0, b - y0)
    clean.paste(canvas.crop(qr_box), (qr_box[0], qr_box[1]))
    return clean


def extract_qr_via_pdfium(pdf_bytes, password, bbox):
    """
    QR ki asli embedded image ko seedha PDF se nikaalta hai (page render
    nahi) — isse aas-paas ka koi text/line QR ke saath nahi aata, aur
    QR ke modules bilkul original rehte hain (scan hota rahega).
    bbox: pdfplumber coords (x0, top, x1, bottom).
    """
    pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes), password=password or None)
    try:
        page = pdf[0]
        page_h = page.get_height()
        best = None
        for obj in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]):
            l, b, r, t = obj.get_bounds()
            top = page_h - t
            dist = abs(l - bbox[0]) + abs(top - bbox[1]) + abs(r - bbox[2]) + abs((page_h - b) - bbox[3])
            if best is None or dist < best[0]:
                best = (dist, obj)
        if best is None or best[0] > 8:
            return None
        img = best[1].get_bitmap().to_pil().convert("RGB")
    finally:
        pdf.close()

    # Sharp modules ke liye NEAREST se bada karo + chaaron taraf white quiet zone
    scale = max(1, QR_OUTPUT_MIN_PX // max(img.width, img.height))
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    side = max(img.width, img.height)
    qz = int(side * QR_QUIET_ZONE_RATIO)
    canvas = Image.new("RGB", (img.width + 2 * qz, img.height + 2 * qz), "white")
    canvas.paste(img, (qz, qz))
    return canvas


def crop_real_qr_image(pdf_bytes, password=None):
    """
    QR ko auto-detect karke sirf QR deta hai (real QR, regenerate nahi —
    kyunki usme UIDAI ka signed data hota hai).
      1) PDF me square image dhoondo, aur wahi image seedha extract karo
      2) Na ho paye to us jagah ko render karke dense-QR area se square crop
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
            page = pdf.pages[0]

            bbox = find_qr_bbox_from_images(page)
            if bbox is not None:
                try:
                    qr = extract_qr_via_pdfium(pdf_bytes, password, bbox)
                    if qr is not None:
                        return qr
                except Exception:
                    pass
            else:
                bbox = QR_CROP_PDF_BBOX  # fallback search area

            safe_bbox = (
                max(0, bbox[0]), max(0, bbox[1]),
                min(page.width, bbox[2]), min(page.height, bbox[3])
            )
            if safe_bbox[2] <= safe_bbox[0] or safe_bbox[3] <= safe_bbox[1]:
                return None
            cropped = page.crop(safe_bbox).to_image(resolution=QR_CROP_RESOLUTION)
            img = cropped.original.convert("RGB")
            return squarify_ink(img)
    except Exception:
        return None


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


# ============================================================
# DATA EXTRACTION
# ============================================================
def extract_front_data(pdf_bytes, password=None):
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
        page = pdf.pages[0]
        photo_img = find_face_photo_image(page)

    text = extract_text_pdfium(pdf_bytes, password)
    lines = text.split("\n")

    hindi_name, english_name, _ = detect_name_block(lines)

    aadhaar_number = extract_aadhaar_number(text)

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


def extract_back_data(pdf_bytes, password=None):
    text = extract_text_pdfium(pdf_bytes, password)
    lines = text.split("\n")

    aadhaar_number = extract_aadhaar_number(text)

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


# ============================================================
# CARD BUILDERS
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

    info_box = scale_box(FRONT_INFO_BOX)
    info_meta = {}
    info_fit_scale = None
    info_crop = crop_front_info_block(pdf_bytes, password, has_hindi_name=has_hindi, meta_out=info_meta)

    if info_crop is not None and not info_meta.get("font_px"):
        info_meta = estimate_info_meta_from_image(info_crop)

    if info_crop is not None:
        box_w = info_box[2] - info_box[0]
        box_h = info_box[3] - info_box[1]
        fitted_info = contain_fit(info_crop, box_w, box_h)
        if info_meta.get("font_px"):
            # Hamesha (mobile print ho ya na ho) Mobile line ki jagah chhod ke fit karo:
            #   1) width box me aaye
            #   2) block + Mobile line red-border box ke upar rahe
            #   3) label ka font FRONT_INFO_MAX_FONT_PX se bada na ho
            # Size aur gap dono ek saath scale hote hain, isliye hamesha barabar rehte hain.
            need_h = (info_meta["last_baseline_px"] + info_meta["pitch_px"]
                      + FRONT_MOBILE_DESCENT_RATIO * info_meta["font_px"])
            avail_h = int(FRONT_MOBILE_BOTTOM_LIMIT * scale_y) - info_box[1]
            max_font_px = FRONT_INFO_MAX_FONT_PX * scale_y
            s_fit = min(
                box_w / info_crop.width,
                avail_h / need_h,
                max_font_px / info_meta["font_px"],
            ) * FRONT_INFO_SHRINK
            new_w = max(1, int(info_crop.width * s_fit))
            new_h = max(1, int(info_crop.height * s_fit))
            fitted_info = info_crop.resize((new_w, new_h), Image.LANCZOS)
        info_fit_scale = fitted_info.width / info_crop.width
        template.paste(fitted_info, (info_box[0], info_box[1]), fitted_info)
    else:
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
        mobile_text = f"Mobile No: {data['mobile_number']}"
        if info_fit_scale is not None and info_meta.get("font_px"):
            # Name/DOB/Gender wali lines jaisa hi font size aur wahi line-gap
            font_px = max(8, int(round(info_meta["font_px"] * info_fit_scale)))
            baseline_y = info_box[1] + int(round(
                (info_meta["last_baseline_px"] + info_meta["pitch_px"]) * info_fit_scale))
            text_x = info_box[0] + int(round(FRONT_INFO_INK_PAD_PX * info_fit_scale))  # crop padding ke baad, baaki lines ke left se aligned
            draw.text((text_x, baseline_y), mobile_text,
                      font=get_font("en", False, font_px), fill="black", anchor="ls")
        else:
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

    qr_crop = crop_real_qr_image(pdf_bytes, password)
    qr_box = scale_box(BACK_QR_BOX)
    if qr_crop is not None:
        qr_box_w = qr_box[2] - qr_box[0]
        qr_box_h = qr_box[3] - qr_box[1]
        fitted_qr = contain_fit(qr_crop, qr_box_w, qr_box_h)
        template.paste(fitted_qr, (qr_box[0], qr_box[1]))

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
# A4 PRINT PAGE BUILDER
# ============================================================
def build_print_pdf_page(front_img, back_img):
    """
    Front aur back card ko ek A4 sheet par unke asli (CR-80) size mein,
    ek doosre ke neeche center karke rakhta hai — print karke seedha
    cut kiya ja sake, isliye har card ke 4 corners par chhote
    crop-marks (cutting guide lines) bhi bana diye hain.
    """
    a4 = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), "white")
    draw = ImageDraw.Draw(a4)

    card_w, card_h = front_img.width, front_img.height
    total_h = card_h * 2 + PRINT_CARD_GAP_PX
    x = (A4_WIDTH_PX - card_w) // 2
    y_front = (A4_HEIGHT_PX - total_h) // 2
    y_back = y_front + card_h + PRINT_CARD_GAP_PX

    a4.paste(front_img.convert("RGB"), (x, y_front))
    a4.paste(back_img.convert("RGB"), (x, y_back))

    corners = [
        (x, y_front), (x + card_w, y_front),
        (x, y_front + card_h), (x + card_w, y_front + card_h),
        (x, y_back), (x + card_w, y_back),
        (x, y_back + card_h), (x + card_w, y_back + card_h),
    ]
    m = PRINT_CROP_MARK_LEN
    for (cx, cy) in corners:
        draw.line([(cx - m, cy), (cx + m, cy)], fill="black", width=1)
        draw.line([(cx, cy - m), (cx, cy + m)], fill="black", width=1)

    return a4


# ============================================================
# ROUTES
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


@app.route("/generate-print-pdf", methods=["POST"])
def generate_print_pdf():
    if "pdf" not in request.files:
        return jsonify({"error": "PDF file is required (field name: pdf)"}), 400

    password = request.form.get("password", "").strip()
    print_mobile = request.form.get("print_mobile", "no").strip().lower() == "yes"
    pdf_file = request.files["pdf"]
    pdf_bytes = pdf_file.read()

    try:
        front = build_front_card_image(pdf_bytes, password, print_mobile)
        back = build_back_card_image(pdf_bytes, password)
        page = build_print_pdf_page(front, back)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except pdfplumber.pdfminer.pdfdocument.PDFPasswordIncorrect:
        return jsonify({"error": "Password galat hai ya PDF unlock nahi ho paayi"}), 400
    except Exception as e:
        return jsonify({"error": f"Could not generate the print PDF: {str(e)}"}), 500

    output = io.BytesIO()
    page.save(output, format="PDF", resolution=float(A4_DPI))
    output.seek(0)
    return send_file(output, mimetype="application/pdf", as_attachment=False, download_name="aadhaar-card-print.pdf")


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Aadhaar Card Generator API is running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
