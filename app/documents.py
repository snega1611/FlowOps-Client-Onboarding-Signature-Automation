"""Local document intelligence: validation, OCR (Tesseract), regex extraction, normalisation, validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime


ALLOWED_EXT = {
    ".txt": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".pdf": "pdf",
}

MAGIC = {".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}
MIN_CONF = 0.6


SPECS = {
    "government_id": {
        "full_name": r"(?:full\s*name|name)\s*[:\-]\s*(.+)",
        "id_number": r"(?:id|document)\s*(?:no\.?|number)\s*[:\-]\s*([A-Za-z0-9\-]{6,})",
        "date_of_birth": r"(?:date\s*of\s*birth|dob)\s*[:\-]\s*(.+)",
        "expiry_date": r"(?:expiry|expires|expiration)(?:\s*date)?\s*[:\-]\s*(.+)",
    },
    "proof_of_address": {
        "full_name": r"(?:full\s*name|name)\s*[:\-]\s*(.+)",
        "address": r"address\s*[:\-]\s*(.+)",
        "statement_date": r"(?:statement|issue)\s*date\s*[:\-]\s*(.+)",
    },
}



REQUIRED_FIELDS = {
    "government_id": {"full_name"},
    "proof_of_address": {"full_name", "address"},
}


DATE_FIELDS = {"date_of_birth", "expiry_date", "statement_date"}
FMTS = ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y"]


class UnsupportedDocument(ValueError):
    pass


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class Field:
    name: str
    raw: str
    normalized: str
    confidence: float
    evidence: str
    status: str = "ok"  # ok | ambiguous | conflict


@dataclass
class Extraction:
    doc_type: str
    status: str  # validated | needs_correction | needs_review | failed
    fields: list[Field] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    ocr_confidence: float = 1.0
    text: str = ""


def check_upload(filename: str, data: bytes, max_bytes: int) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise UnsupportedDocument(f"unsupported file type '{ext}'")
    if not data or len(data) > max_bytes:
        raise UnsupportedDocument("file empty or too large")
    if ext in MAGIC and not data.startswith(MAGIC[ext]):
        raise UnsupportedDocument("file content does not match its extension")
    return ext


def ocr_image(path: str) -> tuple[str, float]:
    try:
        import pytesseract
        from PIL import Image

        pytesseract.get_tesseract_version()
        d = pytesseract.image_to_data(Image.open(path), output_type=pytesseract.Output.DICT)
    except Exception as e:  # noqa: BLE001
        raise OCRUnavailable(str(e)) from e
    words, confs, lines = [], [], {}
    for i, w in enumerate(d["text"]):
        if w.strip():
            c = float(d["conf"][i])
            if c >= 0:
                confs.append(c)
            lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(w)
            words.append(w)
    text = "\n".join(" ".join(v) for v in lines.values())
    return text, (sum(confs) / len(confs) / 100 if confs else 0.0)


def normalize_date(raw: str) -> tuple[str | None, bool]:
    """Returns (iso, ambiguous). Numeric d/m vs m/d with both parts <=12 is ambiguous."""
    s = raw.strip().rstrip(".")
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        try:
            if a > 12:
                return date(y, b, a).isoformat(), False
            if b > 12:
                return date(y, a, b).isoformat(), False
            return date(y, b, a).isoformat(), a != b  # assume DD/MM, flag if it could be MM/DD
        except ValueError:
            return None, False
    for f in FMTS:
        try:
            return datetime.strptime(s, f).date().isoformat(), False
        except ValueError:
            pass
    return None, False


def norm_name(s: str) -> str:
    return " ".join(sorted(re.sub(r"[^a-z ]", " ", s.lower()).split()))


def normalize(name: str, raw: str) -> tuple[str, bool]:
    raw = raw.strip()
    if name in DATE_FIELDS:
        iso, amb = normalize_date(raw)
        return (iso or ""), amb
    if name == "id_number":
        return re.sub(r"[\s\-]", "", raw).upper(), False
    if name == "full_name":
        return " ".join(w.capitalize() for w in raw.split()), False
    return " ".join(raw.split()), False


def extract_fields(text: str, doc_type: str, base_conf: float) -> list[Field]:
    out = []
    for name, pat in SPECS[doc_type].items():
        hits = [(m.group(1).strip(), m.group(0).strip()) for m in re.finditer(pat, text, re.I | re.M)]
        hits = [h for h in hits if h[0]]
        if not hits:
            continue
        norms = {normalize(name, h[0])[0] for h in hits}
        raw, ev = hits[0]
        n, amb = normalize(name, raw)
        status, conf = "ok", base_conf
        if len(norms) > 1:
            status, conf = "conflict", min(conf, 0.4)
        elif amb or (name in DATE_FIELDS and not n):
            status, conf = "ambiguous", min(conf, 0.5)
        out.append(Field(name, raw, n, round(conf, 3), ev, status))
    return out


def validate(doc_type: str, fields: list[Field], client_name: str | None, text: str, conf: float,
             today: date | None = None) -> Extraction:
    today = today or date.today()
    issues: list[dict] = []
    if len(text.strip()) < 15:
        return Extraction(doc_type, "failed", [], [{"type": "unreadable", "detail": "no usable text"}], conf, text)
    by = {f.name: f for f in fields}
    
    for req in REQUIRED_FIELDS[doc_type]:
        if req not in by or not by[req].normalized.strip():
            issues.append({"type": "missing_field", "field": req})

    for f in fields:
        if f.status == "conflict":
            issues.append({"type": "conflicting_values", "field": f.name})
        elif f.status == "ambiguous" or f.confidence < MIN_CONF:
            issues.append({"type": "low_confidence", "field": f.name})
    if "full_name" in by and client_name and norm_name(by["full_name"].normalized) != norm_name(client_name):
        issues.append({"type": "name_mismatch", "field": "full_name"})
    if "expiry_date" in by and by["expiry_date"].normalized:
        if date.fromisoformat(by["expiry_date"].normalized) < today:
            issues.append({"type": "expired", "field": "expiry_date"})
    if "statement_date" in by and by["statement_date"].normalized:
        if (today - date.fromisoformat(by["statement_date"].normalized)).days > 90:
            issues.append({"type": "stale", "field": "statement_date"})
    types = {i["type"] for i in issues}
    if types & {"conflicting_values", "low_confidence", "name_mismatch"}:
        status = "needs_review"
    elif issues:
        status = "needs_correction"
    else:
        status = "validated"
    return Extraction(doc_type, status, fields, issues, conf, text)



def process_file(
    path: str,
    doc_type: str,
    client_name: str | None,
    today: date | None = None,
) -> Extraction:
    if doc_type not in SPECS:
        raise UnsupportedDocument(f"unknown document type {doc_type}")

    if path.lower().endswith(".txt"):
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        conf = 0.95

    elif path.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(path)
            text = "\n".join(
                page.extract_text() or ""
                for page in reader.pages
            ).strip()

            if not text:
                return Extraction(
                    doc_type,
                    "needs_review",
                    [],
                    [{
                        "type": "pdf_no_extractable_text",
                        "detail": "PDF may be scanned or contain no extractable text",
                    }],
                    0.0,
                    "",
                )

            conf = 0.95

        except Exception as e:
            return Extraction(
                doc_type,
                "needs_review",
                [],
                [{
                    "type": "pdf_extraction_failed",
                    "detail": str(e)[:120],
                }],
                0.0,
                "",
            )

    else:
        text, conf = ocr_image(path)

    return process_text(text, doc_type, client_name, conf, today)



def process_text(text: str, doc_type: str, client_name: str | None, conf: float = 0.95,
                 today: date | None = None) -> Extraction:
    fields = extract_fields(text, doc_type, conf)
    return validate(doc_type, fields, client_name, text, conf, today)
