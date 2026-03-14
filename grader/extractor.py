import io
import os


class ExtractionError(Exception):
    pass


def extract_text(filename: str, file_bytes: bytes) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext == '.txt':
        return _extract_txt(file_bytes)
    elif ext == '.docx':
        return _extract_docx(file_bytes)
    elif ext == '.pdf':
        return _extract_pdf(file_bytes)
    else:
        raise ExtractionError(f"Unsupported file type: {ext}")


def _extract_txt(file_bytes: bytes) -> str:
    try:
        text = file_bytes.decode('utf-8')
    except UnicodeDecodeError:
        text = file_bytes.decode('latin-1')
    return text.strip()


def _extract_docx(file_bytes: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        text = '\n\n'.join(paragraphs)
        if not text.strip():
            raise ExtractionError("DOCX file appears to be empty or contains no readable text.")
        return text
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"Failed to read DOCX file: {e}")


def _extract_pdf(file_bytes: bytes) -> str:
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
        text = '\n\n'.join(pages)
        if not text.strip():
            raise ExtractionError(
                "PDF appears to be scanned or contains no extractable text. "
                "OCR is not supported."
            )
        return text
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"Failed to read PDF file: {e}")
