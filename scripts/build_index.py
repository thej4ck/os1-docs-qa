#!/usr/bin/env python3
"""Build the FTS5 search index from the OS1 documentation repository.

Usage:
    python scripts/build_index.py [--repo PATH] [--db PATH]

Defaults:
    --repo  ../os1-documentation/Claude Code Playground
    --db    data/search.db
"""

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

# Allow importing from the app package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.search.fts import SearchIndex


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
    from bs4 import BeautifulSoup

    HELP_CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700&family=Source+Sans+3:wght@400;500;600&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Source Sans 3', -apple-system, sans-serif; font-size: 14px; line-height: 1.75; color: #2D2D2D; background: #FAFBFC; padding: 24px; }
.doc-canvas { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 10px; padding: 32px; box-shadow: 0 1px 4px rgba(0,0,0,0.03); max-width: 680px; margin: 0 auto; }
.doc-title { font-family: 'DM Sans', sans-serif; font-size: 1.3em; font-weight: 700; color: #1E293B; padding-bottom: 0.5em; margin: 0 0 0.8em; border-bottom: 2px solid #E2231A; }
.doc-subtitle { font-family: 'DM Sans', sans-serif; font-size: 1.05em; font-weight: 600; color: #1E293B; margin: 1.5em 0 0.6em; padding: 8px 0 6px; border-bottom: 1px solid #E8EAED; }
.field-def { padding: 12px 16px; margin: 8px 0; background: linear-gradient(135deg, #FAFBFC 0%, #F5F6F8 100%); border-left: 3px solid #E2231A; border-radius: 0 8px 8px 0; font-size: 13.5px; line-height: 1.65; border: 1px solid #EDEEF0; border-left: 3px solid #E2231A; }
.field-name { font-weight: 700; color: #1E293B; font-size: 0.88em; letter-spacing: 0.02em; display: inline; }
.field-sep { color: #CBD5E1; margin: 0 6px; font-weight: 300; }
p { margin: 0.6em 0; line-height: 1.75; }
.doc-screenshot { margin: 1.2em 0; text-align: center; }
.doc-screenshot img { display: inline-block; border: 1px solid #E5E7EB; border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
.doc-icon { display: inline; vertical-align: middle; margin: 0 4px; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; border-radius: 8px; overflow: hidden; border: 1px solid #E5E7EB; }
th, td { border: 1px solid #E5E7EB; padding: 10px 14px; text-align: left; font-size: 13px; }
th { background: #F3F4F6; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; color: #6B7280; }
tr:nth-child(even) td { background: #FAFBFC; }
ul, ol { padding-left: 1.6em; margin: 0.6em 0; }
li { margin-bottom: 0.35em; line-height: 1.65; }
li::marker { color: #E2231A; }
strong, b { font-weight: 600; color: #1E293B; }
.doc-dark body, .doc-dark { color: #E4E6EB; background: #0F1117; }
.doc-dark .doc-canvas { background: #1A1D27; border-color: #2E3140; box-shadow: 0 1px 4px rgba(0,0,0,0.2); }
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

    soup = BeautifulSoup(raw, "html.parser")

    # Strip unwanted elements
    for tag in soup.find_all(["script", "style", "link", "meta"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag[attr]

    # Rewrite image paths
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and not src.startswith(("http", "/")):
            img["src"] = f"{help_base}/{src}"

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
        parent_dir = str(Path(rel_path).parent)
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
        default=str(Path(__file__).resolve().parent.parent / "data" / "search.db"),
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

    total = index.count()
    print(f"\n{'='*40}")
    print(f"TOTAL:                {total:>5} chunks indexed")
    print(f"Database size:        {Path(args.db).stat().st_size / 1024 / 1024:.1f} MB")

    index.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
