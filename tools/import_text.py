#!/usr/bin/env python3
"""Import a plain-text or Markdown file into the active project's source.json.

    python3 tools/import_text.py [NAME]     # reads the file named in project.json → input.file

Format (all optional):
  # Title             a top-level heading starts a new section (chapter)
  ## Subtitle         lower-level headings become heading blocks
  > quoted text       block quote
  [^3]                footnote reference;  "[^3]: note text" on its own line defines it
  === page 12 ===     explicit page break (a form-feed character works too)

Paragraphs are separated by blank lines. Without explicit page breaks the text is cut
into "pages" of input.per_page blocks (default 4), so you can still work a page at a time.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmlib  # noqa: E402

PAGE_RE = re.compile(r"^\s*=+\s*page\s+(\d+)\s*=+\s*$", re.I)
NOTE_DEF_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
NOTE_REF_RE = re.compile(r"\[\^(\d+)\]")


def build(text, cfg):
    per_page = cfg.get("input", {}).get("per_page", 4)
    text = text.replace("\r\n", "\n").replace("\f", "\n=== page 0 ===\n")
    explicit = any(PAGE_RE.match(l) for l in text.split("\n"))

    sections, notes, pages = [], {}, []
    cur_sec, cur_page, page_no = None, None, 0

    def new_section(title):
        nonlocal cur_sec
        key = f"s{len(sections) + 1}"
        cur_sec = {"key": key, "name": title, "source_name": title, "first": None, "last": None}
        sections.append(cur_sec)
        notes[key] = {}

    def new_page(number=None):
        nonlocal cur_page, page_no
        page_no = number if number else page_no + 1
        if cur_sec is None:
            new_section(cfg.get("title", "Text"))
        cur_page = {"page": page_no, "section": cur_sec["key"], "blocks": []}
        pages.append(cur_page)
        cur_sec["first"] = cur_sec["first"] or page_no
        cur_sec["last"] = page_no

    def add(kind, src):
        if cur_page is not None and not cur_page["blocks"] and cur_page["section"] != cur_sec["key"]:
            # an empty page that a new chapter starts on: hand it to the new section
            cur_page["section"] = cur_sec["key"]
            cur_sec["first"] = cur_sec["last"] = cur_page["page"]
        if cur_page is None or (not explicit and len(cur_page["blocks"]) >= per_page) \
                or cur_page["section"] != cur_sec["key"]:
            new_page()
        n = len(cur_page["blocks"]) + 1
        blk = {"id": f"p{cur_page['page']}-{n}", "kind": kind, "src": src, "continues": False}
        if kind in ("para", "quote"):
            blk["notes"] = [int(x) for x in NOTE_REF_RE.findall(src)]
        cur_page["blocks"].append(blk)

    # split into chunks separated by blank lines, keeping page markers and headings as their own chunks
    chunks, buf = [], []
    for line in text.split("\n"):
        if not line.strip() or PAGE_RE.match(line) or line.startswith("#") or NOTE_DEF_RE.match(line):
            if buf:
                chunks.append(buf)
                buf = []
            if line.strip():
                chunks.append([line])
        else:
            buf.append(line)
    if buf:
        chunks.append(buf)

    for chunk in chunks:
        first = chunk[0]
        if m := PAGE_RE.match(first):
            num = int(m.group(1))
            new_page(num if num else None)
            continue
        if m := NOTE_DEF_RE.match(first):
            if cur_sec is None:
                new_section(cfg.get("title", "Text"))
            notes[cur_sec["key"]][m.group(1)] = m.group(2).strip()
            continue
        if m := re.match(r"^(#+)\s*(.*)$", first):
            level, title = len(m.group(1)), m.group(2).strip()
            if level == 1:
                new_section(title)
            add("heading", title)
            continue
        joined = " ".join(l.strip() for l in chunk)
        if all(l.lstrip().startswith(">") for l in chunk):
            add("quote", " ".join(l.lstrip()[1:].strip() for l in chunk))
        else:
            add("para", joined)

    pages = [p for p in pages if p["blocks"] or explicit]
    return {"title": cfg.get("source_title", cfg.get("title", "")), "sections": sections,
            "pages": pages, "notes": notes}


def run_project():
    """Import the active project's text file into source.json. Returns a one-line summary."""
    p, cfg = tmlib.paths(), tmlib.load_config()
    src = p.dir / cfg.get("input", {}).get("file", "source.txt")
    data = build(src.read_text(encoding="utf-8"), cfg)
    # keep section names in project.json in sync so the app shows them
    cfg["sections"] = data["sections"]
    tmlib.write_json(p.config, cfg)
    tmlib.write_json(p.source, data)
    return (f"wrote {p.source} — {len(data['pages'])} pages, "
            f"{sum(len(x['blocks']) for x in data['pages'])} blocks, {len(data['sections'])} sections")


def main():
    if len(sys.argv) > 1:
        tmlib.set_active(sys.argv[1])
    print(run_project())


if __name__ == "__main__":
    main()
