#!/usr/bin/env python3
"""Build the FTS5 search index from the OS1 documentation repository.

Usage:
    python scripts/build_index.py [--repo PATH] [--db PATH]

Defaults:
    --repo  ../os1-documentation/Claude Code Playground
    --db    searchdata/search.db
"""

import argparse
import io
import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

# Allow importing from the app package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.search.fts import SearchIndex


# ---------------------------------------------------------------------------
# Shared CSS for document overlay viewer
# ---------------------------------------------------------------------------

HELP_CSS = """<style>
/* All rules scoped under .doc-canvas to avoid infecting the parent page */
.doc-canvas { font-family: 'Source Sans 3', -apple-system, sans-serif; font-size: 14px; line-height: 1.75; color: #2D2D2D; background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 10px; padding: 32px; box-shadow: 0 1px 4px rgba(0,0,0,0.03); max-width: 680px; margin: 0 auto; }
.doc-canvas .doc-title { font-family: 'DM Sans', sans-serif; font-size: 1.3em; font-weight: 700; color: #1E293B; padding-bottom: 0.5em; margin: 0 0 0.8em; border-bottom: 2px solid #E2231A; }
.doc-canvas .doc-subtitle { font-family: 'DM Sans', sans-serif; font-size: 1.05em; font-weight: 600; color: #1E293B; margin: 1.5em 0 0.6em; padding: 8px 0 6px; border-bottom: 1px solid #E8EAED; }
.doc-canvas .field-def { padding: 12px 16px; margin: 8px 0; background: linear-gradient(135deg, #FAFBFC 0%, #F5F6F8 100%); border: 1px solid #EDEEF0; border-left: 3px solid #E2231A; border-radius: 0 8px 8px 0; font-size: 13.5px; line-height: 1.65; }
.doc-canvas .field-name { font-weight: 700; color: #1E293B; font-size: 0.88em; letter-spacing: 0.02em; display: inline; }
.doc-canvas .field-sep { color: #CBD5E1; margin: 0 6px; font-weight: 300; }
.doc-canvas p { margin: 0.6em 0; line-height: 1.75; }
.doc-canvas .doc-screenshot { margin: 1.2em 0; text-align: center; }
.doc-canvas .doc-screenshot img { display: inline-block; border: 1px solid #E5E7EB; border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
.doc-canvas .doc-icon { display: inline; vertical-align: middle; margin: 0 4px; }
.doc-canvas table { border-collapse: collapse; width: 100%; margin: 1em 0; border-radius: 8px; overflow: hidden; border: 1px solid #E5E7EB; }
.doc-canvas th, .doc-canvas td { border: 1px solid #E5E7EB; padding: 10px 14px; text-align: left; font-size: 13px; }
.doc-canvas th { background: #F3F4F6; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; color: #6B7280; }
.doc-canvas tr:nth-child(even) td { background: #FAFBFC; }
.doc-canvas ul, .doc-canvas ol { padding-left: 1.6em; margin: 0.6em 0; }
.doc-canvas li { margin-bottom: 0.35em; line-height: 1.65; }
.doc-canvas li::marker { color: #E2231A; }
.doc-canvas strong, .doc-canvas b { font-weight: 600; color: #1E293B; }
/* Dark mode */
.doc-dark.doc-canvas { color: #E4E6EB; background: #1A1D27; border-color: #2E3140; box-shadow: 0 1px 4px rgba(0,0,0,0.2); }
.doc-dark .doc-title { color: #E4E6EB; border-bottom-color: #EF4444; }
.doc-dark .doc-subtitle { color: #E4E6EB; border-bottom-color: #2E3140; }
.doc-dark .field-def { background: linear-gradient(135deg, #1A1D27 0%, #22252F 100%); border-color: #2E3140; border-left-color: #EF4444; }
.doc-dark .field-name { color: #E4E6EB; }
.doc-dark p, .doc-dark li { color: #C8CCD4; }
.doc-dark table { border-color: #2E3140; }
.doc-dark th, .doc-dark td { border-color: #2E3140; color: #C8CCD4; }
.doc-dark th { background: #22252F; color: #9CA3B4; }
.doc-dark tr:nth-child(even) td { background: #1A1D27; }
.doc-dark .doc-screenshot img { border-color: #2E3140; box-shadow: 0 4px 16px rgba(0,0,0,0.3); }
.doc-dark strong, .doc-dark b { color: #E4E6EB; }
.doc-dark li::marker { color: #EF4444; }
</style>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_md_title(text: str) -> str:
    """Extract the first `# Title` from markdown text."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def extract_table_name(text: str) -> str:
    """Extract the table name from `**Tabella:** `Name`` pattern."""
    m = re.search(r"\*\*Tabella:\*\*\s*`(\w+)`", text)
    return m.group(1) if m else ""


def module_from_path(path: Path, repo: Path) -> str:
    """Derive the OS1 module code from the file path.

    For docs: docs/base-anagrafiche/... → base-anagrafiche
    For help: sources/help/OS1/html/bcge/... → bcge
    """
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return ""
    parts = rel.parts
    if parts[0] == "docs" and len(parts) > 1:
        return parts[1]
    if "html" in parts:
        idx = list(parts).index("html")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def strip_html(html: str) -> tuple[str, str, str]:
    """Strip HTML to plain text. Returns (title, breadcrumbs, body_text)."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    breadcrumbs = ""
    bc_meta = soup.find("meta", attrs={"name": "topic-breadcrumbs"})
    if bc_meta:
        breadcrumbs = bc_meta.get("content", "")

    # Remove script and style elements
    for tag in soup.find_all(["script", "style", "link", "meta"]):
        tag.decompose()

    body = soup.find("body")
    if not body:
        body = soup

    # Get text, preserving some structure
    text = body.get_text(separator="\n", strip=True)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, breadcrumbs, text


def load_integrity_data(integrita_dir: Path) -> dict[str, str]:
    """Load JSON integrity files and build a table_name → text mapping.

    Returns dict like {"Articoli": "Referenziata da: MovMagazzino (IdProdotto), ..."}
    """
    refs: dict[str, str] = {}
    if not integrita_dir.is_dir():
        return refs

    for jf in sorted(integrita_dir.glob("*.json")):
        table_name = jf.stem
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, list):
            continue
        ref_parts = []
        for entry in data:
            ref_table = entry.get("tabella", "")
            ref_fields = entry.get("campi", [])
            if ref_table:
                fields_str = ", ".join(ref_fields) if ref_fields else ""
                ref_parts.append(f"{ref_table} ({fields_str})" if fields_str else ref_table)
        if ref_parts:
            refs[table_name] = "Referenziata da: " + ", ".join(ref_parts)
    return refs


# ---------------------------------------------------------------------------
# Image conversion
# ---------------------------------------------------------------------------

_IMAGE_EXTS = {".png", ".gif", ".jpg", ".jpeg", ".bmp"}


def _convert_image_to_webp(src: Path, dst: Path, quality: int = 85):
    """Convert a single image file to WebP. dst should end in .webp."""
    try:
        img = Image.open(src)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, "WEBP", quality=quality)
    except Exception as e:
        print(f"  WARNING: cannot convert {src.name}: {e}")


def convert_help_images_to_webp(help_src_dir: Path, help_out_dir: Path) -> int:
    """Copy help source images to help_out_dir as WebP, preserving directory structure.

    Also copies .htm files so the bundled help-files/ dir is complete.
    Returns number of images converted.
    """
    count = 0
    if not help_src_dir.is_dir():
        return 0

    for src_file in sorted(help_src_dir.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(help_src_dir)
        if src_file.suffix.lower() in _IMAGE_EXTS:
            dst = help_out_dir / rel.with_suffix(".webp")
            if not dst.exists():
                _convert_image_to_webp(src_file, dst)
            count += 1
        elif src_file.suffix.lower() in (".htm", ".html"):
            # Copy HTML files for static mount
            dst = help_out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src_file, dst)

    return count


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def sanitize_dirname(name: str) -> str:
    """Convert filename to safe directory name (ASCII, no spaces)."""
    name = name.rsplit(".", 1)[0] if "." in name else name
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w\-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name


def extract_pdf_metadata(doc) -> dict:
    """Parse page 0 of a scheda operativa PDF for structured metadata.

    Structure: bold label spans ("Area:") and value spans may be in separate
    blocks but at the same y-coordinate. We aggregate all spans by y-position.
    """
    meta = {"area": "", "titolo": "", "applicazione": "", "revisione": "", "contenuto": ""}
    if doc.page_count == 0:
        return meta

    page = doc[0]
    blocks = page.get_text("dict")["blocks"]

    # Collect all spans with their y-position, group by y (rounded)
    y_groups: dict[int, list] = {}
    for b in blocks:
        if "lines" not in b:
            continue
        for line in b["lines"]:
            y_key = round(line["bbox"][1])
            if y_key not in y_groups:
                y_groups[y_key] = []
            y_groups[y_key].extend(line["spans"])

    current_key = None
    for y_key in sorted(y_groups):
        spans = y_groups[y_key]
        size = max((s["size"] for s in spans), default=0)
        if size >= 16 or size < 8:
            continue

        # Separate bold labels from value text
        labels = []
        values = []
        for s in spans:
            text = s["text"].strip()
            if not text:
                continue
            if s["flags"] & 16:  # bold
                labels.append(text)
            else:
                values.append(text)

        label_text = " ".join(labels).strip()
        value_text = " ".join(values).strip()

        if label_text and ":" in label_text:
            # May contain inline value: "Applicazione: OS1 6.1"
            parts = label_text.split(":", 1)
            key_candidate = parts[0].strip().lower()
            inline_val = parts[1].strip() if len(parts) > 1 else ""
            combined_val = (inline_val + " " + value_text).strip()

            for k in meta:
                if k in key_candidate:
                    meta[k] = combined_val
                    current_key = k
                    break
        elif current_key == "contenuto" and value_text:
            meta["contenuto"] += " " + value_text

    meta["contenuto"] = meta["contenuto"].strip()
    return meta


def identify_logo_xref(doc) -> set:
    """Find header logo xrefs to skip (images on page 0 with logo-like dimensions)."""
    skip = set()
    if doc.page_count == 0:
        return skip
    for img_info in doc[0].get_images(full=True):
        xref = img_info[0]
        try:
            img = doc.extract_image(xref)
            w, h = img["width"], img["height"]
            # OSItalia logos: 500x89 or similar wide-and-short banners
            if w > 300 and h < 120:
                skip.add(xref)
        except Exception:
            pass
    return skip


def extract_pdf_images(doc, output_dir: Path, skip_xrefs: set) -> dict:
    """Extract unique images from PDF as WebP, return {xref: (filename, w, h)}."""
    xref_map = {}
    counter = 0

    for page in doc:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in xref_map or xref in skip_xrefs:
                continue
            try:
                raw = doc.extract_image(xref)
                w, h = raw["width"], raw["height"]
                # Skip tiny images (1x1, 2x2 spacers)
                if w < 10 or h < 10:
                    skip_xrefs.add(xref)
                    continue
                counter += 1
                filename = f"img_{counter:03d}.webp"
                filepath = output_dir / filename
                img = Image.open(io.BytesIO(raw["image"]))
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGBA")
                else:
                    img = img.convert("RGB")
                img.save(filepath, "WEBP", quality=85)
                xref_map[xref] = (filename, w, h)
            except Exception:
                pass

    return xref_map


def build_pdf_content(doc, skip_xrefs: set, xref_map: dict, img_base_url: str,
                      start_page: int = 0) -> tuple[str, str]:
    """Process PDF pages into (plain_text, html_fragment).

    Returns plain text for FTS and HTML for overlay viewer.
    Images >= 200px get [Screenshot: ...] markers in the text.
    """
    text_parts = []
    html_parts = []
    current_heading = ""
    found_first_heading = False

    for page_num in range(start_page, doc.page_count):
        page = doc[page_num]
        blocks = page.get_text("dict")["blocks"]
        blocks.sort(key=lambda b: b["bbox"][1])

        page_height = page.rect.height

        for b in blocks:
            y = b["bbox"][1]
            # Skip header/footer regions
            if y < 85 or y > page_height - 40:
                continue

            # On page 0, skip the metadata area (before first real 18pt+ heading)
            if page_num == 0 and not found_first_heading:
                if "lines" in b:
                    max_sz = max((s["size"] for line in b["lines"] for s in line["spans"]), default=0)
                    is_bold = any(s["flags"] & 16 for line in b["lines"] for s in line["spans"])
                    block_text = " ".join(
                        s["text"] for line in b["lines"] for s in line["spans"]
                    )
                    block_text_norm = re.sub(r"\s+", "", block_text).upper()
                    # Skip "SCHEDA OPERATIVA" and similar decorative headers
                    is_decorative = "SCHEDAOPERATIVA" in block_text_norm
                    if max_sz >= 18 and is_bold and not is_decorative:
                        found_first_heading = True
                    else:
                        continue
                elif b["type"] == 1:
                    continue  # skip images in metadata area too

            if b["type"] == 1:
                # Image block
                xref = b.get("xref", 0)
                # Try to find matching xref from page images
                for img_info in page.get_images(full=True):
                    img_xref = img_info[0]
                    if img_xref in xref_map:
                        fname, w, h = xref_map[img_xref]
                        url = f"{img_base_url}/{fname}"
                        if w >= 200:
                            # Large screenshot — add marker for LLM and HTML
                            desc = current_heading or "Schermata"
                            text_parts.append(f"[Screenshot: {desc} | {url}]")
                            html_parts.append(
                                f'<div class="doc-screenshot">'
                                f'<img src="{url}" style="max-width: {w}px; width: 100%; height: auto;">'
                                f'</div>'
                            )
                        else:
                            # Small icon — only in HTML
                            html_parts.append(
                                f'<img class="doc-icon" src="{url}" '
                                f'style="max-width: {w}px; height: auto;">'
                            )
                        # Only emit once per block
                        break
                continue

            if "lines" not in b:
                continue

            # Accumulate normal text lines within a block to form paragraphs
            pending_lines = []

            def flush_pending():
                if pending_lines:
                    para = " ".join(pending_lines)
                    text_parts.append(para)
                    html_parts.append(f"<p>{_html_escape(para)}</p>")
                    pending_lines.clear()

            for line in b["lines"]:
                spans = line["spans"]
                full_text = "".join(s["text"] for s in spans).strip()
                if not full_text:
                    continue

                max_size = max(s["size"] for s in spans)
                is_bold = any(s["flags"] & 16 for s in spans)

                # Skip copyright and "SCHEDA OPERATIVA" decorative text
                if max_size < 8:
                    continue
                if "SCHEDA OPERATIVA" in full_text.upper() and max_size >= 16:
                    continue

                if max_size >= 18 and is_bold:
                    flush_pending()
                    # Section heading
                    current_heading = full_text
                    text_parts.append(f"\n## {full_text}")
                    html_parts.append(f'<h2 class="doc-title">{_html_escape(full_text)}</h2>')
                elif max_size >= 14 and is_bold:
                    flush_pending()
                    # Sub-heading
                    current_heading = full_text
                    text_parts.append(f"\n### {full_text}")
                    html_parts.append(f'<h3 class="doc-subtitle">{_html_escape(full_text)}</h3>')
                elif is_bold and ":" in full_text and len(full_text) < 120:
                    flush_pending()
                    # Field definition
                    idx = full_text.index(":")
                    name = full_text[:idx].strip()
                    desc = full_text[idx + 1:].strip()
                    text_parts.append(f"{name}: {desc}")
                    html_parts.append(
                        f'<div class="field-def">'
                        f'<span class="field-name">{_html_escape(name)}</span>'
                        f'<span class="field-sep">:</span> {_html_escape(desc)}</div>'
                    )
                else:
                    # Normal text line — accumulate for paragraph merging
                    pending_lines.append(full_text)

            flush_pending()

    plain = "\n".join(text_parts).strip()
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    html = "\n".join(html_parts)
    return plain, html


def _html_escape(text: str) -> str:
    """Minimal HTML escaping."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_by_headings(full_text: str, full_html: str, max_section_chars: int = 15_000) -> list[dict]:
    """Split content at ## headings into sections.

    If a section exceeds max_section_chars, sub-split at ### headings.
    Returns list of {title, text, html} dicts.
    """
    # Split text at ## headings and filter empties
    text_sections = [s.strip() for s in re.split(r"(?=\n## )", full_text) if s.strip()]
    # Split HTML at h2 tags and filter empties
    html_sections = [s.strip() for s in re.split(r'(?=<h2 class="doc-title">)', full_html) if s.strip()]

    sections = []
    for i, text_sec in enumerate(text_sections):
        title = ""
        first_line = text_sec.split("\n")[0]
        if first_line.startswith("## "):
            title = first_line[3:].strip()
        elif first_line.startswith("### "):
            title = first_line[4:].strip()

        html_sec = html_sections[i] if i < len(html_sections) else ""

        if len(text_sec) <= max_section_chars:
            sections.append({"title": title, "text": text_sec, "html": html_sec})
        else:
            # Sub-split at ### headings, filter empties
            sub_texts = [s.strip() for s in re.split(r"(?=\n### )", text_sec) if s.strip()]
            sub_htmls = [s.strip() for s in re.split(r'(?=<h3 class="doc-subtitle">)', html_sec) if s.strip()]
            for j, sub_text in enumerate(sub_texts):
                sub_title = title
                fl = sub_text.split("\n")[0]
                if fl.startswith("### "):
                    sub_title = fl[4:].strip()
                elif fl.startswith("## "):
                    sub_title = fl[3:].strip()
                sub_html = sub_htmls[j] if j < len(sub_htmls) else ""
                sections.append({"title": sub_title, "text": sub_text, "html": sub_html})

    return sections if sections else [{"title": "", "text": full_text, "html": full_html}]


# ---------------------------------------------------------------------------
# Ingestors
# ---------------------------------------------------------------------------

def ingest_table_docs(index: SearchIndex, docs_dir: Path, repo: Path, integrity: dict[str, str]):
    """Ingest markdown table definition files (docs/base-anagrafiche/, docs/vendite/, etc.).
    Each file = 1 chunk. Enriched with integrity data.
    Skips docs/funzionale/ and docs/schema/ (handled separately).
    """
    skip_dirs = {"funzionale", "schema", "filestore"}
    count = 0

    for md_file in sorted(docs_dir.rglob("*.md")):
        # Skip special directories
        rel_to_docs = md_file.relative_to(docs_dir)
        if rel_to_docs.parts[0] in skip_dirs:
            continue
        # Skip README index files
        if md_file.name.lower() == "readme.md":
            continue

        content = md_file.read_text(encoding="utf-8")
        if len(content.strip()) < 20:
            continue

        title = extract_md_title(content)
        table_name = extract_table_name(content)
        module = module_from_path(md_file, repo)

        # Enrich with integrity data
        if table_name and table_name in integrity:
            content += f"\n\n## Relazioni\n{integrity[table_name]}"

        index.index_document(
            content=content,
            source_file=str(md_file.relative_to(repo)),
            title=title or md_file.stem,
            module=module,
            doc_type="table-def",
        )
        count += 1

    return count


def ingest_functional_docs(index: SearchIndex, funzionale_dir: Path, repo: Path):
    """Ingest functional overview files, splitting by ## sections.
    Each section = 1 chunk with parent document context.
    """
    count = 0

    for md_file in sorted(funzionale_dir.glob("*.md")):
        if md_file.name.lower() == "readme.md":
            continue
        if md_file.name == "note-da-approfondire.md":
            continue

        content = md_file.read_text(encoding="utf-8")
        doc_title = extract_md_title(content)
        module = module_from_path(md_file, repo)

        # Split by ## headings
        sections = re.split(r"(?=^## )", content, flags=re.MULTILINE)

        for section in sections:
            section = section.strip()
            if not section or len(section) < 30:
                continue

            # Extract section title
            section_title = ""
            first_line = section.split("\n")[0]
            if first_line.startswith("## "):
                section_title = first_line[3:].strip()
            elif first_line.startswith("# "):
                section_title = first_line[2:].strip()

            # Add parent context
            chunk_content = f"Parte di: {doc_title}\n\n{section}"

            index.index_document(
                content=chunk_content,
                source_file=str(md_file.relative_to(repo)),
                title=section_title or doc_title or md_file.stem,
                module=module,
                doc_type="functional",
            )
            count += 1

    return count


def ingest_schema_census(index: SearchIndex, schema_file: Path, repo: Path):
    """Ingest the schema census file, splitting by ## MODULE sections.
    Each module = 1 chunk.
    """
    if not schema_file.is_file():
        return 0

    content = schema_file.read_text(encoding="utf-8")
    count = 0

    # Split by ## headings (each is a module)
    sections = re.split(r"(?=^## )", content, flags=re.MULTILINE)

    for section in sections:
        section = section.strip()
        if not section or not section.startswith("## "):
            continue

        # Extract module code and description
        first_line = section.split("\n")[0]
        title = first_line[3:].strip()  # e.g., "BABI - Gestione analisi di bilancio"
        module_code = title.split(" - ")[0].strip() if " - " in title else title.split()[0]

        index.index_document(
            content=section,
            source_file=str(schema_file.relative_to(repo)),
            title=title,
            module=module_code,
            doc_type="schema",
        )
        count += 1

    return count


def preprocess_help_html(raw: str, source_file: str, help_base: str) -> str:
    """Transform RoboHelp HTML into clean, professional semantic HTML."""

    soup = BeautifulSoup(raw, "html.parser")

    # Strip unwanted elements
    for tag in soup.find_all(["script", "style", "link", "meta"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag[attr]

    # Rewrite image paths → .webp
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and not src.startswith(("http", "/")):
            src = f"{help_base}/{src}"
        if src:
            src = re.sub(r"\.(png|gif|jpg|jpeg|PNG|GIF|JPG|JPEG|bmp|BMP)$", ".webp", src)
            img["src"] = src

    # Strip dead links
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href and not href.startswith("http"):
            a.replace_with(a.get_text())

    # Remove empty paragraphs and hrs
    for p in soup.find_all("p"):
        text = p.get_text(strip=True).replace("\xa0", "")
        if not text and not p.find("img"):
            p.decompose()
    for hr in soup.find_all("hr"):
        hr.decompose()

    # Transform headings
    for h5 in soup.find_all("h5"):
        h5.name = "h2"
        h5["class"] = ["doc-title"]
        if h5.has_attr("style"): del h5["style"]

    for p in list(soup.find_all("p")):
        style = p.get("style", "")
        if "font-weight" in style and "bold" in style:
            text = p.get_text(strip=True)
            if not text:
                continue
            if "font-size" in style and ("12pt" in style or "14pt" in style):
                new_tag = soup.new_tag("h2")
                new_tag["class"] = ["doc-title"]
                new_tag.string = text
                p.replace_with(new_tag)
            else:
                new_tag = soup.new_tag("h3")
                new_tag["class"] = ["doc-subtitle"]
                new_tag.string = text
                p.replace_with(new_tag)

    # Transform field definitions
    for p in list(soup.find_all("p")):
        style = p.get("style", "")
        has_indent = "text-indent" in style or "margin-left" in style.replace(" ", "")
        text = p.get_text(strip=True)
        if has_indent and ":" in text:
            _build_field_def(soup, p, text)
        elif re.match(r'^[A-Z\s\.\'/]{4,}:', text):
            _build_field_def(soup, p, text)

    # Transform images
    for img in list(soup.find_all("img")):
        style = img.get("style", "")
        mw_match = re.search(r'max-width:\s*(\d+)', style)
        max_w = int(mw_match.group(1)) if mw_match else 0
        if img.has_attr("style"): del img["style"]
        if max_w > 100:
            img["style"] = f"max-width: {max_w}px; width: 100%; height: auto;"
            fig = soup.new_tag("div")
            fig["class"] = ["doc-screenshot"]
            img.replace_with(fig)
            fig.append(img)
        elif max_w > 0:
            img["class"] = ["doc-icon"]
            img["style"] = f"max-width: {max_w}px; height: auto;"
        else:
            img["style"] = "max-width: 100%; height: auto;"

    # Convert single-column tables to lists
    for table in list(soup.find_all("table")):
        cells = table.find_all("td")
        cols = table.find_all("col")
        if len(cols) <= 1:
            texts = [td.get_text(strip=True) for td in cells if td.get_text(strip=True)]
            if texts:
                ul = soup.new_tag("ul")
                for t in texts:
                    li = soup.new_tag("li")
                    li.string = t
                    ul.append(li)
                table.replace_with(ul)

    # Flatten nested list hacks
    for li in list(soup.find_all("li")):
        style = li.get("style", "")
        if "display" in style and "inline" in style:
            inner_ul = li.find("ul")
            if inner_ul and li.parent and li.parent.parent:
                try:
                    li.parent.replace_with(inner_ul)
                except ValueError:
                    pass

    # Strip all remaining inline styles
    for tag in soup.find_all(["p", "span", "div", "td", "th", "li", "ul", "ol", "tr", "table"]):
        if tag.has_attr("style"): del tag["style"]
    for tag in soup.find_all(True, attrs={"align": True}):
        del tag["align"]

    # Unwrap empty spans
    for tag in soup.find_all("span"):
        if not tag.attrs:
            tag.unwrap()

    # Extract body
    body = soup.find("body")
    content_html = body.decode_contents() if body else soup.decode_contents()

    return HELP_CSS + f'<div class="doc-canvas">{content_html}</div>'


def _build_field_def(soup, p, text):
    """Convert a paragraph to a field-def card."""
    idx = text.index(":")
    name = text[:idx].strip()
    desc = text[idx + 1:].strip()
    div = soup.new_tag("div")
    div["class"] = ["field-def"]
    name_span = soup.new_tag("span")
    name_span["class"] = ["field-name"]
    name_span.string = name
    sep = soup.new_tag("span")
    sep["class"] = ["field-sep"]
    sep.string = ":"
    div.append(name_span)
    div.append(sep)
    div.append(" " + desc)
    p.replace_with(div)


def ingest_html_help(index: SearchIndex, help_dir: Path, repo: Path):
    """Ingest HTML help files. Each file = 1 chunk after HTML stripping.
    Also preprocesses and stores professional HTML for the overlay viewer.
    """
    count = 0
    skipped = 0

    for htm_file in sorted(help_dir.rglob("*.htm")):
        try:
            raw = htm_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        title, breadcrumbs, text = strip_html(raw)

        if len(text.strip()) < 50:
            skipped += 1
            continue

        module = module_from_path(htm_file, repo)

        # Plain text for FTS search
        parts = []
        if breadcrumbs:
            parts.append(f"Percorso: {breadcrumbs}")
        if title:
            parts.append(f"# {title}")
        parts.append(text)
        chunk_content = "\n\n".join(parts)

        # Preprocessed HTML for overlay viewer
        rel_path = str(htm_file.relative_to(repo)).replace("\\", "/")
        # Use string split instead of Path (Path converts / to \ on Windows)
        parent_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        prefix = "sources/help/"
        help_base = "/help-files/" + parent_dir[len(prefix):] if parent_dir.startswith(prefix) else "/help-files"
        html_content = preprocess_help_html(raw, rel_path, help_base)

        index.index_document(
            content=chunk_content,
            source_file=str(htm_file.relative_to(repo)),
            title=title or htm_file.stem,
            module=module,
            doc_type="help",
            html_content=html_content,
        )
        count += 1

    return count, skipped


def ingest_pdf_schede(index: SearchIndex, schede_dir: Path, repo: Path, images_out_dir: Path) -> int:
    """Ingest PDF schede operative. Extracts text+images, applies hybrid chunking."""
    import fitz

    if not schede_dir.is_dir():
        return 0

    count = 0
    for pdf_file in sorted(schede_dir.rglob("*.pdf")):
        try:
            doc = fitz.open(str(pdf_file))
        except Exception as e:
            print(f"  WARNING: cannot open {pdf_file.name}: {e}")
            continue

        # 1. Extract metadata from page 0
        meta = extract_pdf_metadata(doc)
        title = meta["titolo"] or pdf_file.stem

        # 2. Identify logos to skip
        logo_xrefs = identify_logo_xref(doc)

        # 3. Extract images as WebP
        img_dir = images_out_dir / sanitize_dirname(pdf_file.name)
        img_dir.mkdir(parents=True, exist_ok=True)
        xref_map = extract_pdf_images(doc, img_dir, logo_xrefs)

        # 4. Build text + HTML
        img_base_url = f"/help-files/schede-operative/{sanitize_dirname(pdf_file.name)}"
        full_text, full_html = build_pdf_content(doc, logo_xrefs, xref_map, img_base_url)

        if len(full_text.strip()) < 50:
            doc.close()
            continue

        # 5. Module from category folder
        try:
            module = str(pdf_file.relative_to(schede_dir)).split("/")[0].split("\\")[0]
        except (ValueError, IndexError):
            module = ""

        rel_path = str(pdf_file.relative_to(repo)).replace("\\", "/")

        # 6. Chunking
        if len(full_text) < 15_000:
            # Single chunk
            content = f"Scheda operativa: {title}\nArea: {meta['area']}\n\n{full_text}"
            html = HELP_CSS + f'<div class="doc-canvas">{full_html}</div>'
            index.index_document(
                content=content,
                source_file=rel_path,
                title=title,
                module=module,
                doc_type="scheda-operativa",
                html_content=html,
            )
            count += 1
        else:
            # Split by headings
            sections = split_by_headings(full_text, full_html)
            for i, section in enumerate(sections):
                # Skip very short sections (< 100 chars of real content)
                if len(section["text"].strip()) < 100:
                    continue
                sec_title = section["title"] or title
                source = f"{rel_path}#sezione-{i + 1}"
                content = f"Scheda operativa: {title}\nArea: {meta['area']}\nSezione: {sec_title}\n\n{section['text']}"
                html = HELP_CSS + f'<div class="doc-canvas">{section["html"]}</div>'
                index.index_document(
                    content=content,
                    source_file=source,
                    title=sec_title,
                    module=module,
                    doc_type="scheda-operativa",
                    html_content=html,
                )
                count += 1

        doc.close()

    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build OS1 docs search index")
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parent.parent.parent / "os1-documentation" / "Claude Code Playground"),
        help="Path to the documentation repository",
    )
    parser.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parent.parent / "searchdata" / "search.db"),
        help="Path to the output SQLite database",
    )
    args = parser.parse_args()

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"ERROR: repo path not found: {repo}")
        sys.exit(1)

    print(f"Repository: {repo}")
    print(f"Database:   {args.db}")
    print()

    # Convert help images to WebP
    help_src = repo / "sources" / "help"
    help_out = Path(__file__).resolve().parent.parent / "help-files"
    n_imgs = convert_help_images_to_webp(help_src, help_out)
    print(f"Help images -> WebP:  {n_imgs:>5} converted")

    # Create fresh index
    index = SearchIndex(args.db)
    index.rebuild()

    # Load integrity data for enrichment
    integrity = load_integrity_data(repo / "sources" / "integrita")
    print(f"Loaded integrity data for {len(integrity)} tables")

    # Ingest table docs
    docs_dir = repo / "docs"
    n = ingest_table_docs(index, docs_dir, repo, integrity)
    index.commit()
    print(f"Table definitions:    {n:>5} chunks")

    # Ingest functional docs
    funzionale_dir = docs_dir / "funzionale"
    n = ingest_functional_docs(index, funzionale_dir, repo)
    index.commit()
    print(f"Functional sections:  {n:>5} chunks")

    # Ingest schema census
    schema_file = docs_dir / "schema" / "censimento-tabelle.md"
    n = ingest_schema_census(index, schema_file, repo)
    index.commit()
    print(f"Schema modules:       {n:>5} chunks")

    # Ingest HTML help
    help_dir = repo / "sources" / "help"
    n, skipped = ingest_html_help(index, help_dir, repo)
    index.commit()
    print(f"HTML help:            {n:>5} chunks ({skipped} skipped)")

    # Ingest PDF schede operative
    schede_dir = repo / "docs" / "schede-operative"
    schede_img_out = help_out / "schede-operative"
    n = ingest_pdf_schede(index, schede_dir, repo, schede_img_out)
    index.commit()
    print(f"Schede operative:     {n:>5} chunks")

    total = index.count()
    print(f"\n{'='*40}")
    print(f"TOTAL:                {total:>5} chunks indexed")
    print(f"Database size:        {Path(args.db).stat().st_size / 1024 / 1024:.1f} MB")

    index.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
