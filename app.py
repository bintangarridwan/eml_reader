import re
import mimetypes
import hashlib
import zipfile
from io import BytesIO
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from typing import Iterable, Optional, Tuple

import streamlit as st
from bs4 import BeautifulSoup


def _parse_eml(raw_bytes: bytes) -> EmailMessage:
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    if not isinstance(msg, EmailMessage):
        msg = EmailMessage(policy=policy.default)
        msg.set_content(raw_bytes.decode(errors="replace"))
    return msg


def _clean_filename(name: str) -> str:
    name = name.strip() or "attachment"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180]


def _iter_body_parts(msg: Message) -> Iterable[Message]:
    if msg.is_multipart():
        yield from msg.walk()
    else:
        yield msg


def _pick_best_bodies(msg: Message) -> Tuple[Optional[str], Optional[str]]:
    text_body = None
    html_body = None

    for part in _iter_body_parts(msg):
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue

        ctype = (part.get_content_type() or "").lower()
        try:
            payload = part.get_content()
        except Exception:
            payload = None

        if payload is None:
            continue

        if ctype == "text/plain" and text_body is None:
            text_body = str(payload)
        elif ctype == "text/html" and html_body is None:
            html_body = str(payload)

        if text_body and html_body:
            break

    return text_body, html_body


def _extract_attachments(msg: Message) -> list[dict]:
    items: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue

        disp = (part.get_content_disposition() or "").lower()
        has_filename = bool(part.get_filename())
        if disp != "attachment" and not has_filename:
            continue

        filename = _clean_filename(part.get_filename() or "attachment")
        ctype = part.get_content_type() or "application/octet-stream"
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            data = b""

        items.append(
            {
                "filename": filename,
                "content_type": ctype,
                "size": len(data),
                "data": data,
            }
        )

    return items


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/(1024*1024):.1f} MB"


def _guess_mime(filename: str, fallback: str) -> str:
    guess, _ = mimetypes.guess_type(filename)
    return guess or fallback or "application/octet-stream"


def _zip_attachments(files: list[tuple[str, bytes]]) -> bytes:
    """
    files: list of (filename, bytes)
    Returns zip bytes in-memory.
    """
    bio = BytesIO()
    seen: dict[str, int] = {}

    with zipfile.ZipFile(bio, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for original_name, data in files:
            name = _clean_filename(original_name or "attachment")
            if name in seen:
                seen[name] += 1
                stem, dot, ext = name.rpartition(".")
                suffix = f" ({seen[name]})"
                name = f"{stem}{suffix}{dot}{ext}" if dot else f"{name}{suffix}"
            else:
                seen[name] = 1
            zf.writestr(name, data or b"")

    return bio.getvalue()


@st.cache_data(show_spinner=False)
def _render_pdf_pages(pdf_bytes: bytes, max_pages: int = 12, zoom: float = 1.7) -> list[bytes]:
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        raise RuntimeError("PyMuPDF tidak tersedia") from e

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out: list[bytes] = []
    page_count = min(len(doc), max_pages)
    mat = fitz.Matrix(zoom, zoom)
    for i in range(page_count):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        out.append(pix.tobytes("png"))
    doc.close()
    return out


def _render_attachment_preview(att: dict) -> None:
    filename: str = att["filename"]
    data: bytes = att["data"]
    ctype: str = _guess_mime(filename, att.get("content_type") or "")

    main = (ctype.split(";")[0].strip().lower() if ctype else "")
    if main.startswith("image/"):
        st.image(data, caption=filename, use_container_width=True)
        return

    if main == "application/pdf" or filename.lower().endswith(".pdf"):
        try:
            pages = _render_pdf_pages(data, max_pages=12, zoom=1.7)
        except Exception:
            st.error("Preview PDF offline butuh dependency `pymupdf`. Jalankan `pip install -r requirements.txt` lalu restart.")
            return

        if not pages:
            st.caption("PDF kosong atau gagal dirender.")
            return

        if len(pages) == 1:
            st.image(pages[0], caption=f"{filename} • page 1", use_container_width=True)
            return

        idx = st.slider("Halaman", min_value=1, max_value=len(pages), value=1, step=1)
        st.image(pages[idx - 1], caption=f"{filename} • page {idx}", use_container_width=True)
        return

    if main.startswith("audio/"):
        st.audio(data, format=main)
        return

    if main.startswith("video/"):
        st.video(data, format=main)
        return

    if main.startswith("text/") or filename.lower().endswith((".txt", ".log", ".csv", ".json", ".xml", ".md")):
        text = data.decode("utf-8", errors="replace")
        st.text_area(filename, value=text, height=420)
        return

    if main == "message/rfc822" or filename.lower().endswith(".eml"):
        try:
            nested = _parse_eml(data)
            st.markdown("**Preview email terlampir**")
            st.markdown(f"**Subject:** {nested.get('Subject') or ''}")
            st.markdown(f"**From:** {nested.get('From') or ''}")
            st.markdown(f"**Date:** {nested.get('Date') or ''}")
            t, h = _pick_best_bodies(nested)
            if h:
                st.components.v1.html(h, height=480, scrolling=True)
            elif t:
                st.text_area("Body", value=t, height=420)
            else:
                st.caption("Body tidak ditemukan.")
        except Exception:
            st.caption("Tidak bisa preview `.eml` terlampir. Silakan download.")
        return

    st.caption("Preview belum didukung untuk tipe ini. Silakan download.")


st.set_page_config(page_title="EML Reader", layout="wide")

st.markdown(
    """
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap');
      
      /* Global Styles */
      * {
        font-family: 'Plus Jakarta Sans', sans-serif !important;
      }
      
      .stApp {
        background: linear-gradient(135deg, #090d16 0%, #0f172a 50%, #1e1b4b 100%) !important;
        color: #f1f5f9 !important;
      }
      
      .block-container {
        max-width: 1400px;
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
      }
      
      /* Scrollbar Styling */
      ::-webkit-scrollbar {
        width: 8px;
        height: 8px;
      }
      ::-webkit-scrollbar-track {
        background: rgba(15, 23, 42, 0.5);
      }
      ::-webkit-scrollbar-thumb {
        background: rgba(99, 102, 241, 0.3);
        border-radius: 9999px;
      }
      ::-webkit-scrollbar-thumb:hover {
        background: rgba(99, 102, 241, 0.6);
      }

      /* Custom Pane Titles */
      .pane-title {
        font-size: 1.5rem;
        font-weight: 700;
        background: linear-gradient(to right, #818cf8, #c084fc);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.25rem;
      }
      .pane-subtitle {
        font-size: 0.85rem;
        color: #94a3b8;
        margin-bottom: 1rem;
      }
      
      /* Cards / Containers styling */
      div[data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(21, 28, 44, 0.65) !important;
        backdrop-filter: blur(12px) !important;
        border: 1px solid rgba(255, 255, 255, 0.06) !important;
        border-radius: 16px !important;
        padding: 1.5rem !important;
        box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5) !important;
      }
      
      /* Left Column (Message List Item Cards) */
      div[data-testid="stColumn"]:first-child:has(.pane-title) div[data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(30, 41, 59, 0.4) !important;
        border: 1px solid rgba(255, 255, 255, 0.05) !important;
        padding: 12px 14px !important;
        margin-bottom: 12px !important;
        border-radius: 12px !important;
        transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
        cursor: pointer;
      }
      
      div[data-testid="stColumn"]:first-child:has(.pane-title) div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: rgba(99, 102, 241, 0.5) !important;
        background: rgba(30, 41, 59, 0.7) !important;
        transform: translateY(-2px);
        box-shadow: 0 8px 20px -5px rgba(99, 102, 241, 0.2) !important;
      }
      
      /* Highlight active message card */
      div[data-testid="stVerticalBlockBorderWrapper"]:has(.active-indicator) {
        border-color: rgba(99, 102, 241, 0.8) !important;
        background: rgba(99, 102, 241, 0.08) !important;
        box-shadow: 0 0 15px -3px rgba(99, 102, 241, 0.3) !important;
      }
      
      /* Style message list item button (Subject) */
      div[data-testid="stColumn"]:first-child:has(.pane-title) button {
        background: transparent !important;
        border: none !important;
        color: #f8fafc !important;
        padding: 0 !important;
        font-weight: 600 !important;
        font-size: 0.95rem !important;
        text-align: left !important;
        justify-content: flex-start !important;
        box-shadow: none !important;
        width: 100% !important;
        display: block !important;
        white-space: normal !important;
        word-break: break-word !important;
        margin-bottom: 4px !important;
        line-height: 1.4 !important;
      }
      
      div[data-testid="stColumn"]:first-child:has(.pane-title) button:hover {
        color: #a5b4fc !important;
      }
      

      /* Message meta & snippet in list */
      .msg-meta {
        font-size: 0.75rem !important;
        color: #818cf8 !important;
        margin-bottom: 4px !important;
        font-weight: 500;
      }
      .msg-preview {
        font-size: 0.8rem !important;
        color: #94a3b8 !important;
        line-height: 1.4 !important;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
      }
      
      /* Standard Button Overrides */
      .stButton > button {
        background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%) !important;
        color: #ffffff !important;
        border: none !important;
        padding: 0.5rem 1.25rem !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 4px 12px rgba(99, 102, 241, 0.25) !important;
      }
      
      .stButton > button:hover {
        background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%) !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 6px 16px rgba(99, 102, 241, 0.4) !important;
      }
      
      .stButton > button:active {
        transform: translateY(0px) !important;
      }
      
      /* Download Button style (secondary-like buttons) */
      div.stDownloadButton button,
      div[data-testid="stDownloadButton"] button,
      button[data-testid="stDownloadButton"] {
        background: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
        color: #ffffff !important;
        border: none !important;
        padding: 0.5rem 1.25rem !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25) !important;
      }
      div.stDownloadButton button:hover,
      div[data-testid="stDownloadButton"] button:hover,
      button[data-testid="stDownloadButton"]:hover {
        background: linear-gradient(135deg, #059669 0%, #047857 100%) !important;
        box-shadow: 0 6px 16px rgba(16, 185, 129, 0.4) !important;
      }
      
      /* Form / Input Field Styling */
      div[data-baseweb="select"] {
        background-color: #1e293b !important;
        border: 1px solid rgba(255, 255, 255, 0.1) !important;
        border-radius: 8px !important;
        color: #f1f5f9 !important;
      }
      
      div[data-baseweb="select"] > div {
        color: #f1f5f9 !important;
        background-color: transparent !important;
      }
      
      /* Dropdown text lists styling */
      ul[role="listbox"] {
        background-color: #1e293b !important;
        border: 1px solid rgba(255, 255, 255, 0.1) !important;
      }
      
      ul[role="listbox"] li {
        color: #f1f5f9 !important;
      }
      
      ul[role="listbox"] li:hover {
        background-color: #334155 !important;
      }
      
      /* Textarea / Text input container styling */
      .stTextArea textarea {
        background-color: #0f172a !important;
        border: 1px solid rgba(255, 255, 255, 0.1) !important;
        border-radius: 8px !important;
        color: #e2e8f0 !important;
        font-family: monospace !important;
      }
      
      .stTextArea textarea:focus {
        border-color: #6366f1 !important;
        box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.25) !important;
      }
      
      /* Custom Headers for EML Viewer details */
      .hdr {
        margin-bottom: 1.5rem;
      }
      .hdr .subject {
        font-size: 1.75rem;
        font-weight: 700;
        color: #ffffff;
        line-height: 1.3;
        margin-bottom: 0.5rem;
      }
      
      /* Metadata rows styling */
      .meta-grid {
        display: grid;
        grid-template-columns: auto 1fr;
        gap: 0.5rem 1.5rem;
        background: rgba(15, 23, 42, 0.4);
        padding: 1rem;
        border-radius: 8px;
        border: 1px solid rgba(255, 255, 255, 0.05);
        margin-bottom: 1.5rem;
      }
      .meta-label {
        font-weight: 600;
        color: #818cf8;
        font-size: 0.85rem;
      }
      .meta-value {
        color: #e2e8f0;
        font-size: 0.85rem;
      }
      
      /* Custom tabs bar styling */
      button[data-baseweb="tab"] {
        color: #94a3b8 !important;
        border-bottom: 2px solid transparent !important;
        font-weight: 500 !important;
        background: transparent !important;
      }
      button[data-baseweb="tab"][aria-selected="true"] {
        color: #6366f1 !important;
        border-bottom-color: #6366f1 !important;
        font-weight: 600 !important;
      }
      
      /* Attachment Chips */
      .attachments-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 1rem;
      }
      .att-chip {
        background: rgba(99, 102, 241, 0.1);
        border: 1px solid rgba(99, 102, 241, 0.2);
        color: #e2e8f0;
        padding: 6px 12px;
        border-radius: 20px;
        font-size: 0.8rem;
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      
      .active-indicator {
        display: none;
      }
      
      /* File Uploader styling */
      div[data-testid="stFileUploader"] {
        background: rgba(21, 28, 44, 0.45) !important;
        border: 2px dashed rgba(99, 102, 241, 0.3) !important;
        border-radius: 12px !important;
        padding: 1.5rem !important;
        transition: all 0.3s ease !important;
      }
      div[data-testid="stFileUploader"]:hover {
        border-color: rgba(99, 102, 241, 0.7) !important;
        background: rgba(21, 28, 44, 0.6) !important;
      }
      /* Hide browse and add buttons inside the uploader, keep only remove file buttons */
      div[data-testid="stFileUploader"] button:not([aria-label*="Remove"]):not([aria-label*="Delete"]):not([aria-label*="remove"]):not([aria-label*="delete"]) {
        display: none !important;
      }
      
      /* Hide horizontal scrollbar in uploaded files container */
      div[data-testid="stFileUploader"] [data-testid="stFileUploaderUploadedFiles"],
      div[data-testid="stFileUploader"] .stFileUploaderUploadedFiles {
        scrollbar-width: none !important; /* Firefox */
      }
      div[data-testid="stFileUploader"] [data-testid="stFileUploaderUploadedFiles"]::-webkit-scrollbar,
      div[data-testid="stFileUploader"] .stFileUploaderUploadedFiles::-webkit-scrollbar {
        display: none !important; /* Webkit */
        width: 0 !important;
        height: 0 !important;
      }
      
      /* Hide all original text nodes inside the dropzone instructions container */
      [data-testid="stFileUploaderDropzoneInstructions"] span,
      .stFileUploaderDropzoneInstructions span {
        display: none !important;
      }
      
      /* Center container and set up the instructions style */
      .stFileUploaderDropzoneInstructions,
      [data-testid="stFileUploaderDropzoneInstructions"] {
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        text-align: center !important;
        width: 100% !important;
        margin: 0 auto !important;
      }
      
      /* Inject "Unggah File" in the center of the dropzone */
      [data-testid="stFileUploaderDropzoneInstructions"]::after,
      .stFileUploaderDropzoneInstructions::after {
        content: "Unggah File" !important;
        font-size: 1.05rem !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        display: block !important;
        text-align: center !important;
        cursor: pointer !important;
        text-decoration: none !important;
        transition: opacity 0.2s ease !important;
        margin-top: 0.5rem !important;
      }
      [data-testid="stFileUploaderDropzoneInstructions"]:hover::after,
      .stFileUploaderDropzoneInstructions:hover::after {
        color: #ffffff !important;
        opacity: 0.8 !important;
      }
      
      /* Style for the delete/clear button of uploaded files */
      div[data-testid="stFileUploader"] [data-testid="stUploadedFile"] button,
      div[data-testid="stFileUploader"] .stUploadedFile button,
      div[data-testid="stFileUploader"] [data-testid="stFileUploaderUploadedFiles"] button,
      div[data-testid="stFileUploader"] .stFileUploaderUploadedFiles button {
        background: rgba(239, 68, 68, 0.1) !important;
        color: #ef4444 !important;
        border: 1px solid rgba(239, 68, 68, 0.2) !important;
        border-radius: 4px !important;
        padding: 2px 6px !important;
        font-size: 0.75rem !important;
        height: auto !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        margin-left: 8px !important;
        transition: all 0.2s ease !important;
      }
      div[data-testid="stFileUploader"] [data-testid="stUploadedFile"] button:hover,
      div[data-testid="stFileUploader"] .stUploadedFile button:hover,
      div[data-testid="stFileUploader"] [data-testid="stFileUploaderUploadedFiles"] button:hover,
      div[data-testid="stFileUploader"] .stFileUploaderUploadedFiles button:hover {
        background: rgba(239, 68, 68, 0.25) !important;
        border-color: rgba(239, 68, 68, 0.5) !important;
        color: #f87171 !important;
      }
      div[data-testid="stFileUploader"] [data-testid="stUploadedFile"] button svg,
      div[data-testid="stFileUploader"] .stUploadedFile button svg,
      div[data-testid="stFileUploader"] [data-testid="stFileUploaderUploadedFiles"] button svg,
      div[data-testid="stFileUploader"] .stFileUploaderUploadedFiles button svg {
        fill: #ef4444 !important;
        color: #ef4444 !important;
      }
      div[data-testid="stFileUploader"] [data-testid="stUploadedFile"] button:hover svg,
      div[data-testid="stFileUploader"] .stUploadedFile button:hover svg,
      div[data-testid="stFileUploader"] [data-testid="stFileUploaderUploadedFiles"] button:hover svg,
      div[data-testid="stFileUploader"] .stFileUploaderUploadedFiles button:hover svg {
        fill: #f87171 !important;
        color: #f87171 !important;
      }
      .att-chip span {
        color: #94a3b8;
        font-size: 0.75rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style="display: flex; align-items: center; gap: 1rem; margin-bottom: 2rem;">
        <div style="background: linear-gradient(135deg, #6366f1 0%, #a855f7 100%); width: 48px; height: 48px; border-radius: 12px; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 20px rgba(99, 102, 241, 0.4);">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg>
        </div>
        <div>
            <h1 style="margin: 0; font-size: 2.25rem; font-weight: 800; background: linear-gradient(to right, #ffffff, #cbd5e1); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">EML Reader</h1>
            <p style="margin: 0; font-size: 0.95rem; color: #94a3b8;">Pratinjau, ekstrak, dan unduh lampiran email secara offline</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

uploaded_files = st.file_uploader("Upload file .eml", type=["eml"], accept_multiple_files=True)

if "closed_files" not in st.session_state:
    st.session_state["closed_files"] = set()

# Cleanup closed files that are no longer present in uploaded_files
if uploaded_files:
    current_ids = {f"{f.name}-{f.size}" for f in uploaded_files}
    st.session_state["closed_files"] = st.session_state["closed_files"].intersection(current_ids)

# Filter active files
active_files = []
if uploaded_files:
    active_files = [f for f in uploaded_files if f"{f.name}-{f.size}" not in st.session_state["closed_files"]]

if not active_files:
    st.info("Silakan upload satu atau beberapa file `.eml` untuk mulai. (Maks. 200MB per file • EML)")
    st.stop()


messages: list[dict] = []
for idx, f in enumerate(active_files):
    raw = f.getvalue()
    m = _parse_eml(raw)
    subj = (m.get("Subject") or f.name or "(no subject)").strip()
    frm = (m.get("From") or "").strip()
    date = (m.get("Date") or "").strip()
    preview = ""
    t, h = _pick_best_bodies(m)
    if t:
        preview = re.sub(r"\s+", " ", t).strip()[:160]
    elif h:
        preview = re.sub(r"\s+", " ", _html_to_text(h)).strip()[:160]
    messages.append(
        {
            "id": f"{idx}-{f.name}-{hashlib.sha1(raw).hexdigest()[:10]}",
            "filename": f.name,
            "file_key": f"{f.name}-{f.size}",
            "raw": raw,
            "msg": m,
            "subject": subj,
            "from": frm,
            "date": date,
            "preview": preview,
        }
    )

if "active_tab_id" not in st.session_state:
    st.session_state["active_tab_id"] = messages[0]["id"]

# Jaga agar active selalu valid terhadap daftar message saat ini
all_ids = [m["id"] for m in messages]
if st.session_state["active_tab_id"] not in all_ids:
    st.session_state["active_tab_id"] = messages[0]["id"]

col_list, col_read = st.columns([1.35, 2.65], gap="large")

selected = next((m for m in messages if m["id"] == st.session_state["active_tab_id"]), messages[0])
msg = selected["msg"]
raw = selected["raw"]

with col_list:
    st.markdown('<div class="pane-title">Messages</div>', unsafe_allow_html=True)
    st.markdown('<div class="pane-subtitle">Klik subject untuk membuka</div>', unsafe_allow_html=True)
    
    # Navigation controls (Prev/Next buttons)
    if len(messages) > 1:
        current_idx = all_ids.index(st.session_state["active_tab_id"])
        nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
        with nav_col1:
            if st.button("◀ Prev", key="prev_msg_nav", use_container_width=True):
                st.session_state["active_tab_id"] = all_ids[(current_idx - 1) % len(messages)]
                st.rerun()
        with nav_col2:
            st.markdown(f'<div style="text-align: center; line-height: 2.2rem; font-size: 0.85rem; color: #94a3b8; font-weight: 600;">{current_idx + 1} / {len(messages)}</div>', unsafe_allow_html=True)
        with nav_col3:
            if st.button("Next ▶", key="next_msg_nav", use_container_width=True):
                st.session_state["active_tab_id"] = all_ids[(current_idx + 1) % len(messages)]
                st.rerun()
        st.markdown("<hr style='margin: 10px 0; border: 0; border-top: 1px dashed rgba(255,255,255,0.1);'>", unsafe_allow_html=True)

    for item in messages:
        is_active = (item["id"] == st.session_state["active_tab_id"])
        with st.container(border=True):
            if is_active:
                st.markdown('<div class="active-indicator"></div>', unsafe_allow_html=True)
            label = f"{item['subject']}".strip() or "(no subject)"
            secondary = " • ".join([p for p in [item["from"], item["date"]] if p]).strip()
            
            if st.button(label, key=f"msg-{item['id']}"):
                st.session_state["active_tab_id"] = item["id"]
                st.rerun()
                    
            if secondary:
                st.markdown(f'<div class="msg-meta">{secondary}</div>', unsafe_allow_html=True)
            if item["preview"]:
                st.markdown(f'<div class="msg-preview">{item["preview"]}</div>', unsafe_allow_html=True)

with col_read:
    st.markdown('<div class="pane-title">Reading pane</div>', unsafe_allow_html=True)
    with st.container(border=True):
        # Refresh selected message sesuai active ID
        selected = next((m for m in messages if m["id"] == st.session_state["active_tab_id"]), messages[0])
        msg = selected["msg"]
        raw = selected["raw"]

        subject = (msg.get("Subject") or "(no subject)").strip()
        frm = (msg.get("From") or "").strip()
        to = (msg.get("To") or "").strip()
        cc = (msg.get("Cc") or "").strip()
        date = (msg.get("Date") or "").strip()

        st.markdown(f'<div class="hdr"><div class="subject">{subject}</div></div>', unsafe_allow_html=True)
        
        meta_html = '<div class="meta-grid">'
        if frm:
            meta_html += f'<div class="meta-label">From</div><div class="meta-value">{frm}</div>'
        if to:
            meta_html += f'<div class="meta-label">To</div><div class="meta-value">{to}</div>'
        if cc:
            meta_html += f'<div class="meta-label">Cc</div><div class="meta-value">{cc}</div>'
        if date:
            meta_html += f'<div class="meta-label">Date</div><div class="meta-value">{date}</div>'
        meta_html += '</div>'
        st.markdown(meta_html, unsafe_allow_html=True)

        st.divider()
        st.markdown("**Body**")
        text_body, html_body = _pick_best_bodies(msg)
        body_tab_plain, body_tab_html, body_tab_raw = st.tabs(["Text", "HTML", "Raw"])

        with body_tab_plain:
            if text_body:
                st.text_area("text/plain", value=text_body, height=420)
            elif html_body:
                st.text_area("HTML → text", value=_html_to_text(html_body), height=420)
            else:
                st.caption("Body tidak ditemukan.")

        with body_tab_html:
            if html_body:
                st.components.v1.html(html_body, height=600, scrolling=True)
            else:
                st.caption("Tidak ada body HTML.")

        with body_tab_raw:
            st.text_area("Raw .eml", value=raw.decode(errors="replace"), height=600)

        # Attachment preview di bagian paling bawah
        st.divider()
        atts = _extract_attachments(msg)
        if atts:
            st.markdown("**Attachments**")
            with st.container(border=True):
                st.markdown('<div class="attachments-bar">', unsafe_allow_html=True)
                for a in atts:
                    st.markdown(
                        f'<span class="att-chip">{a["filename"]} <span class="muted">({_fmt_size(a["size"])})</span></span>',
                        unsafe_allow_html=True,
                    )
                st.markdown("</div>", unsafe_allow_html=True)

                col_dl_all, col_sel = st.columns([1.2, 1.8], gap="medium")
                with col_dl_all:
                    # Download semua attachment sebagai ZIP
                    zip_bytes = _zip_attachments([(a["filename"], a["data"]) for a in atts])
                    zip_name = _clean_filename(f"{(subject or 'attachments')[:80]} attachments.zip")
                    st.download_button(
                        label="Download all (ZIP)",
                        data=zip_bytes,
                        file_name=zip_name,
                        mime="application/zip",
                        key=f"dlzip-{selected['id']}-{len(zip_bytes)}",
                        use_container_width=True,
                    )

                with col_sel:
                    att_names = [a["filename"] for a in atts]
                    selected_att = st.selectbox("Preview attachment", options=att_names, index=0, key=f"att-{selected['id']}", label_visibility="collapsed")
                
                att = next(a for a in atts if a["filename"] == selected_att)
                st.download_button(
                    label=f"Download {att['filename']}",
                    data=att["data"],
                    file_name=att["filename"],
                    mime=_guess_mime(att["filename"], att.get("content_type") or ""),
                    key=f"dl2-{selected['id']}-{att['filename']}-{att['size']}",
                    use_container_width=True,
                )
                
                st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
                _render_attachment_preview(att)
        else:
            st.markdown("**Attachments**")
            st.caption("Tidak ada attachment.")

