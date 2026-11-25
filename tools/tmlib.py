"""Shared storage helpers for the translation workbench.

Every text you translate is a *project*: a folder under projects/<name>/ holding

    project.json       title, languages, sections, input settings
    source.json        the source text split into blocks, grouped by page
    translation.json   translations, status, history, alternatives
    requests.json      requests made from the app (alternatives / retranslate)
    edits.jsonl        log of manual edits (Claude learns from these)
    decisions.md       living glossary and style decisions
    style-guide.md     translation instructions for Claude
    pages/pN.png       optional page images

The active project is chosen by, in order: the TM_PROJECT environment variable,
the file projects/.active, or the only project that exists.

Both the web server and the CLI (tools/tm.py) go through these helpers, which lock
the files and write atomically, so Claude and the browser can't clobber each other.
"""
import contextlib
import datetime as _dt
import fcntl
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
ACTIVE = PROJECTS / ".active"
TEMPLATES = ROOT / "templates"

EMPTY = {"blocks": {}, "pages": {}}


def now():
    return _dt.datetime.now().isoformat(timespec="seconds")


# ---- projects ----------------------------------------------------------------

def list_projects():
    if not PROJECTS.exists():
        return []
    return sorted(p.name for p in PROJECTS.iterdir() if (p / "project.json").exists())


def active_name():
    name = os.environ.get("TM_PROJECT")
    if not name and ACTIVE.exists():
        name = ACTIVE.read_text().strip()
    if not name or name not in list_projects():
        projects = list_projects()
        name = projects[0] if len(projects) == 1 else name
    if not name or name not in list_projects():
        raise SystemExit("No active project. Run: python3 tools/tm.py use <name>  "
                         f"(available: {', '.join(list_projects()) or 'none — create one with tm.py new'})")
    return name


def set_active(name):
    if name not in list_projects():
        raise SystemExit(f"unknown project {name!r}; available: {', '.join(list_projects())}")
    ACTIVE.write_text(name + "\n")


def paths(name=None):
    d = PROJECTS / (name or active_name())
    return SimpleNamespace(
        dir=d, config=d / "project.json", source=d / "source.json", trans=d / "translation.json",
        requests=d / "requests.json", edits=d / "edits.jsonl", seen=d / ".edits_seen",
        decisions=d / "decisions.md", style=d / "style-guide.md", pages=d / "pages", lock=d / ".lock",
        inbox=d / ".inbox",
    )


# ---- creating & configuring projects -----------------------------------------

LANGS = {
    "en": ("English", "ltr"), "he": ("עברית", "rtl"), "ar": ("العربية", "rtl"), "fr": ("Français", "ltr"),
    "de": ("Deutsch", "ltr"), "es": ("Español", "ltr"), "it": ("Italiano", "ltr"), "ru": ("Русский", "ltr"),
    "pt": ("Português", "ltr"), "nl": ("Nederlands", "ltr"), "pl": ("Polski", "ltr"), "yi": ("ייִדיש", "rtl"),
    "la": ("Latina", "ltr"), "el": ("Ελληνικά", "ltr"), "fa": ("فارسی", "rtl"), "zh": ("中文", "ltr"),
    "ja": ("日本語", "ltr"),
}
RTL = {"he", "ar", "yi", "fa", "ur"}


def lang(code, label=None):
    code = (code or "en").strip()
    known = LANGS.get(code)
    return {"code": code, "label": label or (known[0] if known else code),
            "dir": known[1] if known else ("rtl" if code in RTL else "ltr")}


def slugify(text):
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]
    if not s:
        n = 1
        while f"project-{n}" in list_projects():
            n += 1
        s = f"project-{n}"
    return s


def create_project(name, filename, data: bytes, title="", src="en", tgt="he",
                   first=None, last=None, page_offset=0, per_page=4):
    """Create projects/<name>/ from a PDF or text file, make it active and import it.
    Returns (name, import_message). Raises ValueError on bad input."""
    name = slugify(name or title)
    d = PROJECTS / name
    if d.exists():
        raise ValueError(f"project {name!r} already exists")
    suffix = Path(filename).suffix.lower() or ".txt"
    is_pdf = suffix == ".pdf"
    target = lang(tgt)
    cfg = {
        "title": title or name,
        "source_title": title or name,
        "source_lang": lang(src),
        "target_lang": target,
        "unit": "עמ'" if target["code"] == "he" else "p.",
        "sections": [],
    }
    if is_pdf:
        first, last = int(first or 1), int(last or first or 1)
        cfg["sections"] = [{"key": "s1", "name": "חלק 1" if target["code"] == "he" else "Part 1",
                            "source_name": "Part 1", "first": first, "last": last}]
        cfg["input"] = {"type": "pdf", "file": "source.pdf", "page_offset": int(page_offset or 0),
                        "render_pages": True, "header_regex": None, "footer_regex": r"\s*\[?\s*\d+\s*\]?\s*",
                        "indent": {"para": [2, 5], "quote": [2, 3], "epigraph": [6, 14], "skip_from": 15},
                        "headings": [], "fixes": {}}
    elif suffix in (".txt", ".md", ".markdown", ".text"):
        cfg["input"] = {"type": "text", "file": "source" + suffix, "per_page": int(per_page or 4)}
    else:
        raise ValueError("only .pdf, .txt and .md files are supported")
    PROJECTS.mkdir(exist_ok=True)
    d.mkdir()
    (d / ("source.pdf" if is_pdf else "source" + suffix)).write_bytes(data)
    write_json(d / "project.json", cfg)
    for f in ("style-guide.md", "decisions.md"):
        if (TEMPLATES / f).exists():
            (d / f).write_bytes((TEMPLATES / f).read_bytes())
    set_active(name)
    try:
        return name, run_import()
    except Exception as e:  # keep the project; the import settings can be fixed afterwards
        return name, f"created, but the import failed: {e}"


def run_import():
    """(Re)build the active project's source.json from its input file. Returns a summary line."""
    kind = load_config().get("input", {}).get("type")
    if kind == "pdf":
        import extract_pdf
        return extract_pdf.run_project()
    if kind == "text":
        import import_text
        return import_text.run_project()
    raise ValueError("project.json → input.type must be 'pdf' or 'text'")


def update_settings(changes: dict):
    """Update title / languages / page range of the active project. Re-imports when the source layout changes."""
    with locked():
        cfg = load_config()
        if "title" in changes:
            cfg["title"] = changes["title"].strip() or cfg.get("title")
        if "src" in changes:
            cfg["source_lang"] = lang(changes["src"])
        if "tgt" in changes:
            cfg["target_lang"] = lang(changes["tgt"])
            cfg["unit"] = "עמ'" if cfg["target_lang"]["code"] == "he" else "p."
        reimport = False
        inp = cfg.get("input", {})
        if inp.get("type") == "pdf":
            for k in ("first", "last"):
                if changes.get(k) and cfg["sections"] and int(changes[k]) != cfg["sections"][-1 if k == "last" else 0][k]:
                    cfg["sections"][-1 if k == "last" else 0][k] = int(changes[k])
                    reimport = True
            if changes.get("page_offset") not in (None, "") and int(changes["page_offset"]) != inp.get("page_offset"):
                inp["page_offset"] = int(changes["page_offset"])
                reimport = True
        elif inp.get("type") == "text" and changes.get("per_page") and int(changes["per_page"]) != inp.get("per_page"):
            inp["per_page"] = int(changes["per_page"])
            reimport = True
        write_json(paths().config, cfg)
    return run_import() if reimport else "saved"


@contextlib.contextmanager
def locked():
    with open(paths().lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path, default):
    if not path.exists():
        return json.loads(json.dumps(default))
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, obj):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    return _read(paths().config, {})


def load_source():
    return _read(paths().source, {"sections": [], "pages": [], "notes": {}})


def load_trans():
    return _read(paths().trans, EMPTY)


def load_requests():
    return _read(paths().requests, [])


def version():
    p = paths()
    m = [f.stat().st_mtime_ns for f in (p.trans, p.requests, p.source) if f.exists()]
    return f"{p.dir.name}:{max(m) if m else 0}"


def block_index(source):
    """id -> (page, block)"""
    return {b["id"]: (p, b) for p in source["pages"] for b in p["blocks"]}


def _rec(t, block_id):
    return t["blocks"].setdefault(block_id, {"tr": "", "status": "pending", "history": []})


# ---- mutations -------------------------------------------------------------

def set_block(block_id, text, by, status=None):
    """Set a block's translation and record history. Returns the block record."""
    with locked():
        t = load_trans()
        rec = _rec(t, block_id)
        before = rec.get("tr", "")
        if text != before:
            hist = rec.setdefault("history", [])
            last = hist[-1] if hist else None
            # coalesce rapid successive manual edits into one history entry
            if (by == "user" and last and last["by"] == "user" and
                    (_dt.datetime.now() - _dt.datetime.fromisoformat(last["at"])).total_seconds() < 600):
                last["tr"], last["at"] = text, now()
            else:
                hist.append({"tr": text, "by": by, "at": now()})
            rec["tr"] = text
            rec["by"] = by
            rec["updated"] = now()
            if by == "user":
                _log_edit(block_id, before, text)
        if status:
            rec["status"] = status
        elif by == "claude":
            rec["status"] = "draft"
        write_json(paths().trans, t)
        return rec


def set_status(block_ids, status):
    with locked():
        t = load_trans()
        for bid in block_ids:
            rec = t["blocks"].get(bid)
            if rec and rec.get("tr"):
                rec["status"] = status
        write_json(paths().trans, t)


def set_page_note(page, note):
    with locked():
        t = load_trans()
        t["pages"].setdefault(str(page), {})["notes"] = note
        write_json(paths().trans, t)


def add_alternatives(block_id, alt):
    """alt: {target, options:[{text, note}], why?}"""
    with locked():
        t = load_trans()
        _rec(t, block_id).setdefault("alternatives", []).append(alt)
        write_json(paths().trans, t)


def dismiss_alternative(block_id, index):
    with locked():
        t = load_trans()
        alts = t["blocks"].get(block_id, {}).get("alternatives", [])
        if 0 <= index < len(alts):
            alts.pop(index)
        write_json(paths().trans, t)


def add_request(req):
    with locked():
        reqs = load_requests()
        req = dict(req, id=(max([r["id"] for r in reqs], default=0) + 1), status="open", at=now())
        reqs.append(req)
        write_json(paths().requests, reqs)
        return req


def resolve_request(req_id, reply=""):
    with locked():
        reqs = load_requests()
        for r in reqs:
            if r["id"] == req_id:
                r.update(status="done", reply=reply, done_at=now())
        write_json(paths().requests, reqs)


def cancel_request(req_id):
    with locked():
        write_json(paths().requests, [r for r in load_requests() if r["id"] != req_id])


def _log_edit(block_id, before, after):
    with open(paths().edits, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": block_id, "at": now(), "before": before, "after": after},
                            ensure_ascii=False) + "\n")
