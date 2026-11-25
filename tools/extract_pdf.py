#!/usr/bin/env python3
"""Extract a PDF into the active project's source.json, driven by project.json.

    python3 tools/extract_pdf.py            # active project
    python3 tools/extract_pdf.py NAME       # a specific project

Needs poppler (pdftotext, pdftoppm):  brew install poppler

Each book page holds the blocks (paragraphs, headings, quotes) that START on it; a
paragraph that runs onto the next page is kept whole on the page where it begins.

How text is recognised (all set in project.json → "input"):
  page_offset     book page = PDF page - page_offset
  header_regex    the first non-empty line of a page matching this is a running header → dropped
  footer_regex    the last non-empty line matching this (full match) is a page number → dropped
  indent          leading-space counts in `pdftotext -layout` output:
                    para      [min, max]  first line of a new paragraph
                    quote     [min, max]  every line of a block quote (with a blank line before it)
                    epigraph  [min, max]  epigraph lines (a line starting with "—" is its attribution)
                    skip_from N           lines indented this much or more are ignored (chapter labels)
  headings        exact heading texts (may wrap onto two lines in the PDF)
  fixes           literal search → replace, for small-caps or OCR artefacts
  notes           endnotes: {"pdf_pages": [first, last], "sections": {key: [start_regex, end_regex]}}

Typesetting differs between books, so for a new book run this, look at the output
(python3 tools/tm.py show N for a few pages) and tune the numbers.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmlib  # noqa: E402

KEEP_HYPHEN = {"self", "well", "long", "non", "anti", "mid", "co", "pre", "post", "half", "ill", "so",
               "free", "book", "high", "ever", "never", "far", "small", "big", "first", "second", "third",
               "late", "early", "pro", "neo", "ultra", "quasi", "semi", "counter", "laissez", "short"}
NOTE_RE = re.compile(r"(?<=[A-Za-z\.\,;\?!”’\)\]])(\d{1,3})(?=[\s—–]|$)")


class Extractor:
    def __init__(self, project_dir: Path, cfg: dict):
        self.cfg = cfg
        self.inp = cfg["input"]
        self.pdf = project_dir / self.inp.get("file", "source.pdf")
        ind = self.inp.get("indent", {})
        self.para = ind.get("para", [2, 5])
        self.quote = ind.get("quote", [2, 3])
        self.epi = ind.get("epigraph", [6, 14])
        self.skip = ind.get("skip_from", 15)
        self.headings = [self.norm(h) for h in self.inp.get("headings", [])]
        self.fixes = self.inp.get("fixes", {})
        self.header_re = self.inp.get("header_regex")
        self.footer_re = self.inp.get("footer_regex")
        self._corpus = None

    # ---- text helpers --------------------------------------------------------
    @staticmethod
    def norm(s):
        return re.sub(r"\s+", " ", s).strip()

    def page_text(self, pdf_page):
        return subprocess.run(
            ["pdftotext", "-q", "-layout", "-f", str(pdf_page), "-l", str(pdf_page), str(self.pdf), "-"],
            capture_output=True, text=True, check=True).stdout

    def corpus(self):
        """Every word in the whole PDF, to tell a line-break hyphen (mid-dle) from a real one (self-interest)."""
        if self._corpus is None:
            txt = subprocess.run(["pdftotext", "-q", "-layout", str(self.pdf), "-"],
                                 capture_output=True, text=True).stdout
            self._corpus = set(re.findall(r"[a-z]+", txt.lower()))
        return self._corpus

    def join_lines(self, lines):
        out = ""
        for ln in lines:
            ln = ln.strip()
            if not out:
                out = ln
            elif out.endswith("­") or (out.endswith("-") and not out.endswith("--")):
                prev = re.split(r"[\s—–“(]", out[:-1])[-1].lower()
                nxt = re.split(r"[\s,.;:?!”’)]", ln)[0].lower()
                keep = (prev in KEEP_HYPHEN or prev.endswith(("teenth", "tieth", "ieth"))) and \
                    (prev + nxt) not in self.corpus()
                out = out[:-1] + ("-" if keep else "") + ln
            else:
                out += " " + ln
        out = out.replace("­ ", "-").replace("­", "-")
        for a, b in self.fixes.items():
            out = out.replace(a, b)
        return self.norm(out)

    @staticmethod
    def mark_notes(text):
        nums = []

        def sub(m):
            nums.append(int(m.group(1)))
            return f"[^{m.group(1)}]"

        return NOTE_RE.sub(sub, text), nums

    def strip_header(self, lines):
        out = list(lines)
        if self.header_re:
            for i, ln in enumerate(out):
                if ln.strip():
                    if re.search(self.header_re, ln):
                        out[i] = ""
                    break
        if self.footer_re:
            for i in range(len(out) - 1, -1, -1):
                if out[i].strip():
                    if re.fullmatch(self.footer_re, out[i]):
                        out[i] = ""
                    break
        return out

    # ---- page → blocks ---------------------------------------------------------
    @staticmethod
    def indent_of(ln):
        return len(ln) - len(ln.lstrip(" ")) if ln.strip() else -1

    def classify(self, lines):
        blocks, cur, i = [], None, 0
        inr = lambda v, r: r[0] <= v <= r[1]  # noqa: E731
        while i < len(lines):
            ln = lines[i].rstrip("\n")
            s = ln.strip()
            if not s:
                i += 1
                if cur and cur[0] in ("heading", "epigraph"):
                    cur = None
                continue
            indent = self.indent_of(ln)
            two = self.norm(s + " " + lines[i + 1].strip()) if i + 1 < len(lines) else s
            if self.norm(s) in self.headings or two in self.headings:
                one = self.norm(s) in self.headings
                blocks.append(["heading", [self.norm(s) if one else two]])
                cur = None
                i += 1 if one else 2
                continue
            if inr(indent, self.epi) or s.startswith("—"):
                if cur and cur[0] == "epigraph":
                    cur[1].append(s)
                else:
                    cur = ["epigraph", [s]]
                    blocks.append(cur)
                i += 1
                continue
            if indent >= self.skip:
                i += 1
                continue
            blank_before = i > 0 and not lines[i - 1].strip()
            nxt_indent = self.indent_of(lines[i + 1]) if i + 1 < len(lines) else -1
            in_quote = cur and cur[0] == "quote" and not blank_before
            if inr(indent, self.quote) and (in_quote or (blank_before and inr(nxt_indent, self.quote)) or
                                            (inr(nxt_indent, self.quote) and cur and cur[0] == "quote")):
                if in_quote:
                    cur[1].append(s)
                else:
                    cur = ["quote", [s]]
                    blocks.append(cur)
            elif inr(indent, self.para):
                cur = ["para", [s]]
                blocks.append(cur)
            elif cur and cur[0] == "para":
                cur[1].append(s)
            else:
                cur = ["para", [s]]
                blocks.append(cur)
            i += 1
        return blocks

    def starts_new(self, lines):
        for ln in lines:
            if ln.strip():
                return self.indent_of(ln) >= self.para[0]
        return True

    # ---- main ----------------------------------------------------------------
    def run(self):
        offset = self.inp.get("page_offset", 0)
        pages = []
        for sec in self.cfg["sections"]:
            carry = None
            for bp in range(sec["first"], sec["last"] + 1):
                lines = self.strip_header(self.page_text(bp + offset).split("\n"))
                raw = self.classify(lines)
                page_blocks = []
                for idx, (kind, lns) in enumerate(raw):
                    if idx == 0 and carry is not None and kind == carry["kind"] and (
                            kind == "quote" or not self.starts_new(lines)):
                        carry["_lines"].extend(lns)
                        carry["continues"] = True
                        continue
                    blk = {"kind": kind, "_lines": lns}
                    page_blocks.append(blk)
                    if kind in ("para", "quote"):
                        carry = blk
                if raw and raw[-1][0] not in ("para", "quote"):
                    carry = None
                pages.append({"page": bp, "section": sec["key"], "blocks": page_blocks})

        for pg in pages:
            for n, blk in enumerate(pg["blocks"], 1):
                blk["id"] = f"p{pg['page']}-{n}"
                lns = blk.pop("_lines")
                if blk["kind"] == "epigraph":
                    quote = [l for l in lns if not l.startswith("—")]
                    attr = [l for l in lns if l.startswith("—")]
                    text = self.join_lines(quote) + ("\n" + self.join_lines(attr) if attr else "")
                else:
                    text = self.join_lines(lns)
                if blk["kind"] in ("para", "quote"):
                    text, nums = self.mark_notes(text)
                    blk["notes"] = nums
                blk["src"] = text
                blk.setdefault("continues", False)
        return pages

    def notes(self):
        ncfg = self.inp.get("notes")
        out = {s["key"]: {} for s in self.cfg["sections"]}
        if not ncfg:
            return out
        first, last = ncfg["pdf_pages"]
        text = "".join("\n".join(self.strip_header(self.page_text(p).split("\n"))) + "\n"
                       for p in range(first, last + 1))
        text = text.replace("­\n", "").replace("­", "-")
        for key, (start, end) in ncfg.get("sections", {}).items():
            m = re.search(f"{start}(.*?){end}", text, re.S)
            if not m:
                print(f"warn: notes for {key} not found", file=sys.stderr)
                continue
            parts = re.split(r"(?m)^\s{0,6}(\d{1,3})\.\s", m.group(1))
            for j in range(1, len(parts) - 1, 2):
                out.setdefault(key, {})[parts[j]] = self.join_lines(parts[j + 1].split("\n"))
        return out

    def render_pages(self, pages_dir: Path):
        """Render page images that don't exist yet. Returns how many were rendered."""
        pages_dir.mkdir(exist_ok=True)
        offset = self.inp.get("page_offset", 0)
        n = 0
        for sec in self.cfg["sections"]:
            for bp in range(sec["first"], sec["last"] + 1):
                if (pages_dir / f"p{bp}.png").exists():
                    continue
                subprocess.run(["pdftoppm", "-q", "-f", str(bp + offset), "-l", str(bp + offset), "-r", "110",
                                "-png", "-singlefile", str(self.pdf), str(pages_dir / f"p{bp}")], check=True)
                n += 1
        return n


def run_project():
    """Extract the active project's PDF into source.json. Returns a one-line summary."""
    p = tmlib.paths()
    cfg = tmlib.load_config()
    ex = Extractor(p.dir, cfg)
    pages = ex.run()
    data = {
        "title": cfg.get("source_title", cfg.get("title", "")),
        "sections": cfg["sections"],
        "pages": pages,
        "notes": ex.notes(),
    }
    tmlib.write_json(p.source, data)
    msg = f"wrote {p.source} — {len(pages)} pages, {sum(len(x['blocks']) for x in pages)} blocks"
    if cfg["input"].get("render_pages"):
        n = ex.render_pages(p.pages)
        if n:
            msg += f"; rendered {n} page images"
    return msg


def main():
    if len(sys.argv) > 1:
        tmlib.set_active(sys.argv[1])
    print(run_project())


if __name__ == "__main__":
    main()
