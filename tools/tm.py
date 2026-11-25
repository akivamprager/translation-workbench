#!/usr/bin/env python3
"""Translation workbench CLI — used by Claude (and by you) to manage projects and translations.

Projects
  tm.py projects                       list projects (* = active)
  tm.py use NAME                       switch the active project (the app follows)
  tm.py new NAME --pdf FILE | --text FILE  [--title T] [--from en] [--to he]
             [--first N --last M --offset K]   (PDF: book pages to take; book page = PDF page - offset)
                                       create a project from a PDF or a text/Markdown file
  tm.py import                         (re)build source.json from the project's input file
  tm.py paths                          print the active project's file paths

Translating
  tm.py status                         progress, next page, open requests, unseen edits
  tm.py next                           the next page that still needs translating
  tm.py show N                         source blocks of page N (+ footnotes, + preceding translation)
  tm.py put FILE.json|- [--force]        write translations: {"page":N,"blocks":{id:text},"notes":"..."}
  tm.py alt FILE.json                  add alternatives: [{"block","target","why","options":[{"text","note"}],"request"}]
  tm.py requests                       open requests made from the app
  tm.py resolve ID [reply]             mark a request done
  tm.py edits [--mark]                 manual edits not yet reviewed (Claude's draft → user's version)
  tm.py export                         write <project>/output/translation.txt
"""
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmlib  # noqa: E402

# ---- projects ----------------------------------------------------------------

def cmd_projects():
    try:
        active = tmlib.active_name()
    except SystemExit:
        active = None
    for name in tmlib.list_projects():
        cfg = json.loads((tmlib.PROJECTS / name / "project.json").read_text(encoding="utf-8"))
        print(f"{'*' if name == active else ' '} {name:20} {cfg.get('title', '')}")


def cmd_use(name):
    tmlib.set_active(name)
    print(f"active project: {name}")


def _opt(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def cmd_new(name):
    src = _opt("--pdf") or _opt("--text")
    if not src:
        sys.exit("give --pdf FILE or --text FILE")
    path = Path(src).expanduser()
    if not path.exists():
        sys.exit(f"no such file: {path}")
    try:
        name, msg = tmlib.create_project(
            name, path.name, path.read_bytes(), title=_opt("--title", ""), src=_opt("--from", "en"),
            tgt=_opt("--to", "he"), first=_opt("--first"), last=_opt("--last"), page_offset=_opt("--offset", 0))
    except ValueError as e:
        sys.exit(str(e))
    print(f"created projects/{name} (now active)\n{msg}")
    if path.suffix.lower() == ".pdf":
        print("Check the result with tm.py show N and tune project.json → input (see tools/extract_pdf.py).")


def cmd_import():
    print(tmlib.run_import())


def cmd_paths():
    p = tmlib.paths()
    for k in ("dir", "config", "style", "decisions", "source", "trans", "pages", "inbox"):
        print(f"{k:10} {getattr(p, k)}")


# ---- translating ---------------------------------------------------------------

def tr(t, bid):
    return t["blocks"].get(bid, {}).get("tr", "")


def pages_with_blocks(src):
    return [p for p in src["pages"] if p["blocks"]]


def next_page(src, t):
    for p in pages_with_blocks(src):
        if any(not tr(t, b["id"]) for b in p["blocks"]):
            return p["page"]
    return None


def cmd_status():
    src, t = tmlib.load_source(), tmlib.load_trans()
    blocks = [b for p in src["pages"] for b in p["blocks"]]
    done = sum(1 for b in blocks if tr(t, b["id"]))
    approved = sum(1 for b in blocks if t["blocks"].get(b["id"], {}).get("status") == "approved")
    print(f"project: {tmlib.active_name()} — {tmlib.load_config().get('title', '')}")
    print(f"blocks: {len(blocks)}  translated: {done}  approved: {approved}")
    print(f"next page to translate: {next_page(src, t) or '— all done'}")
    print(f"open requests: {sum(1 for r in tmlib.load_requests() if r['status'] == 'open')}")
    print(f"unseen manual edits: {len(unseen_edits())}")


def cmd_next():
    print(next_page(tmlib.load_source(), tmlib.load_trans()) or "done")


def cmd_show(n):
    n = int(n)
    src, t = tmlib.load_source(), tmlib.load_trans()
    page = next((p for p in src["pages"] if p["page"] == n), None)
    if not page:
        sys.exit(f"no page {n}")
    sec = page["section"]
    prev = [(b["id"], tr(t, b["id"])) for p in src["pages"] if p["page"] < n for b in p["blocks"] if tr(t, b["id"])]
    if prev:
        print("=== CONTEXT: preceding translation (for continuity) ===")
        for bid, text in prev[-2:]:
            print(f"[{bid}] {text}\n")
    img = tmlib.paths().pages / f"p{n}.png"
    print(f"=== PAGE {n} ({sec})" + (f" — image: {img}" if img.exists() else "") + " ===")
    if not page["blocks"]:
        print("(no block starts on this page — its text belongs to the previous page's last paragraph)")
    for b in page["blocks"]:
        rec = t["blocks"].get(b["id"], {})
        flag = " [continues onto next page]" if b.get("continues") else ""
        print(f"\n--- {b['id']}  ({b['kind']}){flag}  status={rec.get('status', 'pending')}")
        print(b["src"])
        for num in b.get("notes", []):
            print(f"    note {num}: {src.get('notes', {}).get(sec, {}).get(str(num), '?')}")
        if rec.get("tr"):
            print(f"  TRANSLATION (current, by {rec.get('by')}): {rec['tr']}")
    pnote = t["pages"].get(str(n), {}).get("notes")
    if pnote:
        print(f"\n=== page notes ===\n{pnote}")


def read_json_arg(path):
    return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8"))


def consume(path):
    """Files Claude drops in a project's .inbox/ are deleted once imported."""
    if path != "-" and Path(path).resolve().parent.name == ".inbox":
        Path(path).unlink(missing_ok=True)


def cmd_put(path):
    data = read_json_arg(path)
    force = "--force" in sys.argv
    src, t = tmlib.load_source(), tmlib.load_trans()
    idx = tmlib.block_index(src)
    for bid, text in data.get("blocks", {}).items():
        if bid not in idx:
            sys.exit(f"unknown block id {bid}")
        rec = t["blocks"].get(bid, {})
        if not force and (rec.get("status") == "approved" or rec.get("by") == "user"):
            print(f"skip {bid}: approved or edited by the user (use --force to overwrite)")
            continue
        tmlib.set_block(bid, text.strip(), "claude")
        print(f"wrote {bid}")
    if "notes" in data and "page" in data:
        tmlib.set_page_note(data["page"], data["notes"].strip())
        print(f"page {data['page']} notes saved")
    consume(path)


def cmd_alt(path):
    data = read_json_arg(path)
    for a in (data if isinstance(data, list) else [data]):
        alt = {"target": a["target"], "options": a["options"]}
        if a.get("why"):
            alt["why"] = a["why"]
        tmlib.add_alternatives(a["block"], alt)
        print(f"alternatives added to {a['block']} for «{a['target']}»")
        if a.get("request"):
            tmlib.resolve_request(int(a["request"]), "alternatives added")
            print(f"request {a['request']} resolved")
    consume(path)


def cmd_requests():
    src, t = tmlib.load_source(), tmlib.load_trans()
    idx = tmlib.block_index(src)
    reqs = [r for r in tmlib.load_requests() if r["status"] == "open"]
    if not reqs:
        print("no open requests")
    for r in reqs:
        _, b = idx.get(r["block"], (None, {"src": "?"}))
        print(f"\n### request {r['id']}  type={r['type']}  block={r['block']}")
        if r.get("selection"):
            print(f"selected: «{r['selection']}»")
        if r.get("comment"):
            print(f"user comment: {r['comment']}")
        print(f"SOURCE: {b['src']}")
        print(f"TRANSLATION: {tr(t, r['block'])}")


def cmd_resolve(rid, reply=""):
    tmlib.resolve_request(int(rid), reply)
    print(f"request {rid} resolved")


def unseen_edits():
    p = tmlib.paths()
    if not p.edits.exists():
        return []
    lines = p.edits.read_text(encoding="utf-8").splitlines()
    seen = int(p.seen.read_text()) if p.seen.exists() else 0
    return [json.loads(l) for l in lines[seen:] if l.strip()]


def cmd_edits():
    t = tmlib.load_trans()
    by_block = {}
    for e in unseen_edits():
        by_block.setdefault(e["id"], []).append(e)
    if not by_block:
        print("no unseen manual edits")
    for bid, es in by_block.items():
        hist = t["blocks"].get(bid, {}).get("history", [])
        claude = next((h["tr"] for h in reversed(hist) if h["by"] == "claude"), es[0]["before"])
        user = tr(t, bid) or es[-1]["after"]
        print(f"\n### {bid}")
        a, b = claude.split(), user.split()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if op != "equal":
                print(f"  …{' '.join(a[max(0, i1 - 3):i1])} «{' '.join(a[i1:i2])}» → «{' '.join(b[j1:j2])}»")
        print(f"  USER FULL: {user}")
    p = tmlib.paths()
    if "--mark" in sys.argv and p.edits.exists():
        p.seen.write_text(str(len(p.edits.read_text(encoding="utf-8").splitlines())))
        print("\n(marked as seen)")


def cmd_export():
    src, t = tmlib.load_source(), tmlib.load_trans()
    out_dir = tmlib.paths().dir / "output"
    out_dir.mkdir(exist_ok=True)
    parts = []
    for sec in src["sections"]:
        blocks = [tr(t, b["id"]) for p in src["pages"] if p["section"] == sec["key"] for b in p["blocks"]]
        blocks = [x for x in blocks if x]
        if blocks:
            parts.append(f"===== {sec['name']} =====\n\n" + "\n\n".join(blocks))
    path = out_dir / "translation.txt"
    path.write_text("\n\n\n".join(parts) + "\n", encoding="utf-8")
    print(f"wrote {path}")


COMMANDS = {
    "projects": cmd_projects, "use": cmd_use, "new": cmd_new, "import": cmd_import, "paths": cmd_paths,
    "status": cmd_status, "next": cmd_next, "show": cmd_show, "put": cmd_put, "alt": cmd_alt,
    "requests": cmd_requests, "resolve": cmd_resolve, "edits": cmd_edits, "export": cmd_export,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return
    cmd = sys.argv[1]
    args, skip = [], False
    for a in sys.argv[2:]:
        if skip:
            skip = False
        elif a in ("--pdf", "--text", "--title", "--from", "--to", "--first", "--last", "--offset"):
            skip = True
        elif a == "-" or not a.startswith("--"):
            args.append(a)
    COMMANDS[cmd](*args)


if __name__ == "__main__":
    main()
