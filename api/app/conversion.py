import os
import subprocess
from pathlib import Path
from typing import Tuple
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def txt_to_pdf(txt_path: str, pdf_path: str) -> None:
    text = Path(txt_path).read_text(encoding="utf-8", errors="ignore")
    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter
    margin = 54
    x = margin
    y = height - margin
    c.setFont("Courier", 10)
    for raw_line in text.splitlines():
        # Limit line length
        line = raw_line[:120]
        c.drawString(x, y, line)
        y -= 12
        if y <= margin:
            c.showPage()
            c.setFont("Courier", 10)
            y = height - margin
    c.save()


def pdf_to_tiff(pdf_path: str, tiff_path: str) -> Tuple[int, str]:
    # Convert PDF to TIFF suitable for fax (204x196 or 204x98 DPI, Group 3/4)
    # Using Ghostscript to generate fax-optimized TIFF (g4)
    cmd = [
        "gs",
        "-dNOPAUSE",
        "-dBATCH",
        "-sDEVICE=tiffg4",
        "-r204x196",
        "-sCompression=lzw",
        f"-sOutputFile={tiff_path}",
        pdf_path,
    ]
    subprocess.run(cmd, check=True)
    # Page count: use gs to count or fallback to 1
    pages = 1
    try:
        out = subprocess.check_output([
            "gs", "-q", "-dNODISPLAY", "-c",
            f"({pdf_path}) (r) file runpdfbegin pdfpagecount = quit"
        ])
        pages = int(out.strip() or b"1")
    except Exception:
        pass
    return pages, tiff_path
