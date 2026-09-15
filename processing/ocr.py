"""OCR / text recovery via Azure Document Intelligence (prebuilt-read).

Lifts the proven pattern from the Land Records OCR Pipeline notebook:
  1. If the PDF already has a real text layer, skip OCR (born-digital / already OCR'd).
  2. Otherwise call DI prebuilt-read with output=["pdf"] — one call yields BOTH the
     searchable PDF (text layer burned in) AND the recognized page text.

Unlike the notebook (which overwrites SharePoint originals in place), Document Hub
keeps the original and writes the derived searchable PDF alongside it in the UC volume.
"""
import io

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

import config
from . import secrets

DI_MODEL = "prebuilt-read"  # supports output=["pdf"]
MIN_TEXT_CHARS = 50
PAGES_TO_CHECK = 3
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".heif")

_client = None


def _di() -> DocumentIntelligenceClient:
    global _client
    if _client is None:
        key = secrets.get(config.DI_SECRET_SCOPE, config.DI_SECRET_KEY)
        _client = DocumentIntelligenceClient(config.DI_ENDPOINT, AzureKeyCredential(key))
    return _client


def has_text_layer(pdf_bytes: bytes) -> bool:
    """True if the PDF already has extractable text on its first pages."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total = ""
        for page in reader.pages[:PAGES_TO_CHECK]:
            total += page.extract_text() or ""
            if len(total.strip()) >= MIN_TEXT_CHARS:
                return True
        return len(total.strip()) >= MIN_TEXT_CHARS
    except Exception:
        return False


def native_pdf_text(pdf_bytes: bytes) -> list[dict]:
    """Extract per-page text from a born-digital PDF without calling DI."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [{"page": i + 1, "text": (p.extract_text() or "")} for i, p in enumerate(reader.pages)]


def run_di(file_bytes: bytes) -> tuple[bytes, list[dict]]:
    """Run DI prebuilt-read. Returns (searchable_pdf_bytes, [{page, text}])."""
    client = _di()
    poller = client.begin_analyze_document(
        DI_MODEL, AnalyzeDocumentRequest(bytes_source=file_bytes), output=["pdf"]
    )
    result = poller.result()

    # Per-page text straight from the analyze result.
    pages = []
    for p in (result.pages or []):
        lines = [ln.content for ln in (p.lines or [])]
        pages.append({"page": p.page_number, "text": "\n".join(lines)})
    if not pages and getattr(result, "content", None):
        pages = [{"page": 1, "text": result.content}]

    # Retrieve the searchable PDF produced by the same operation.
    op_location = poller.details["operation_location"]
    result_id = op_location.split("/analyzeResults/")[-1].split("?")[0]
    pdf_stream = client.get_analyze_result_pdf(model_id=DI_MODEL, result_id=result_id)
    searchable = b"".join(chunk for chunk in pdf_stream)
    return searchable, pages


def process(file_bytes: bytes, filename: str) -> dict:
    """Return {searchable_pdf: bytes|None, pages: [...], text_source: str, page_count: int}.

    - PDF with a text layer  → keep original as the searchable PDF, use native text.
    - PDF without text / image → Azure DI prebuilt-read (searchable PDF + OCR text).
    """
    lower = filename.lower()
    if lower.endswith(".pdf"):
        if has_text_layer(file_bytes):
            pages = native_pdf_text(file_bytes)
            return {"searchable_pdf": None, "pages": pages,
                    "text_source": "native_pdf", "page_count": len(pages)}
        searchable, pages = run_di(file_bytes)
        return {"searchable_pdf": searchable, "pages": pages,
                "text_source": "di_ocr", "page_count": len(pages)}

    if lower.endswith(IMAGE_EXTS):
        searchable, pages = run_di(file_bytes)
        return {"searchable_pdf": searchable, "pages": pages,
                "text_source": "di_ocr", "page_count": len(pages)}

    # Office / email / other: handled by extract.parse_other (ai_parse_document / programmatic).
    return {"searchable_pdf": None, "pages": [], "text_source": "deferred", "page_count": 0}
