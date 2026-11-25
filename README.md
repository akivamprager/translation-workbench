# Translation Workbench

A local, side-by-side workbench for translating books and essays page by page **with Claude**,
built for careful human-in-the-loop translation: Claude drafts, you review and polish, Claude learns
from your edits.

<div dir="rtl">

**שולחן תרגום** — כלי מקומי לתרגום ספרים ומאמרים עמוד אחר עמוד בעזרת Claude. Claude מתרגם טיוטה,
אתם קוראים, מאשרים ומתקנים לצד המקור, ו־Claude לומד מהתיקונים שלכם. ממשק המשתמש בעברית.

</div>

## What it does

- **Source and translation side by side**, paragraph by paragraph, with the original page image
  (for PDFs) one click away. Footnote markers in the source show the note text on click.
- **One click to translate** a page (or the next few) with Claude — it runs Claude Code in the
  background and the translation appears in the app as it's saved.
- **Edit in place.** Every change is saved automatically and kept in a version history with a
  word-level diff and one-click restore.
- **Ask for alternatives**: select a phrase, add a comment, and Claude proposes at least three
  rewordings with pros and cons; click one to swap it into the sentence.
- **Retranslate** a paragraph with a comment, or **ask Claude anything** about the text
  ("replace X with Y everywhere", "make page 3 more formal").
- **Claude learns from you.** Before each new page Claude reviews how you changed its drafts and
  records the lessons in the project's *decisions* file (glossary, register, phrasing), which it
  reads before every page. You can edit that file, and the project's translation guidelines, from the app.
- **Approve** paragraphs or whole pages, track progress, and **export** the translation as plain text.
- **Many texts, any language pair.** Each text is a project with its own languages, guidelines,
  decisions and translation. Right-to-left languages (Hebrew, Arabic, …) are handled on either side.

## Requirements

- macOS or Linux, **Python 3.9+** (standard library only — nothing to `pip install`)
- **[Claude Code](https://claude.com/claude-code)**, logged in — for the one-click translation.
  Without it the app still works: it shows the command to paste into a Claude Code chat instead.
- **poppler** for PDF sources: `brew install poppler` (macOS) or `apt install poppler-utils`

## Quick start

```bash
git clone https://github.com/akivamprager/translation-workbench.git
cd translation-workbench
python3 server.py
```

Open http://localhost:8765. A small public-domain example (J. S. Mill, *On Liberty*) is included.

**Add your own text:** click **＋ טקסט חדש** ("new text"), give it a name, pick the source and target
languages, and either upload a file (PDF, `.txt`, `.md`) or paste the text. For a PDF, give the
range of book pages you want and the page offset (how many PDF pages come before printed page 1).

Then, on each page: **תרגם עכשיו עם Claude** → review → edit or ask for alternatives → **אשר**.

### Working from Claude Code instead

Everything also works by talking to Claude Code in this folder (it reads [CLAUDE.md](CLAUDE.md)):

| You say | Claude does |
|---|---|
| `תרגם עמוד 5` / `next page` | reviews your recent edits, translates page 5 into the app |
| `תרגם עמודים 5–7` | translates a range |
| `טפל בבקשות` | answers the alternative/retranslate requests you made in the app |
| "I want to translate X" | creates a project and tunes the PDF extraction with you |

## How it fits together

```
app/index.html          the whole UI (vanilla JS, no build step)
server.py               tiny local HTTP server (127.0.0.1 only) + JSON API
tools/tmlib.py          project storage: locking, atomic writes, history, requests, edit log
tools/tm.py             CLI used by Claude (and you): show / put / alt / requests / edits / new / …
tools/claude_bridge.py  runs `claude -p` headless for the app's Claude buttons, one task at a time
tools/extract_pdf.py    PDF → blocks, driven by project.json (layout heuristics are configurable)
tools/import_text.py    text / Markdown → blocks
templates/              default style guide and decisions file for new projects
projects/<name>/        one folder per text (git-ignored, except the example)
```

A project folder:

```
project.json        title, languages, sections (page ranges), input settings
source.pdf|.md      the original
source.json         the source split into blocks (heading / epigraph / para / quote) per page
translation.json    translations, status, history, alternatives, page notes
decisions.md        living glossary & style decisions (Claude appends lessons here)
style-guide.md      translation guidelines for Claude
requests.json       alternative / retranslate requests from the app
edits.jsonl         log of your manual edits
pages/pN.png        page images (PDF projects)
```

The app and Claude never write the same file at the same time: all writes go through
`tools/tmlib.py`, which locks and writes atomically. Claude never overwrites a paragraph you
approved or edited unless you ask it to.

### Text and Markdown input

Paragraphs are separated by blank lines. Optional markup:

```
# Chapter title          starts a new section
## Subheading            heading block
> quoted text            block quote
...text[^3]              footnote reference;   [^3]: note text   defines it
=== page 12 ===          explicit page break
```

Without explicit page breaks the text is cut into "pages" of N paragraphs (default 4, set in Settings).

### PDF input

PDFs are read with `pdftotext -layout`, and paragraphs, block quotes, epigraphs, headings, running
headers and footnote markers are recognised from indentation and patterns set in `project.json` →
`input` (documented at the top of [tools/extract_pdf.py](tools/extract_pdf.py)). Every book is typeset
differently, so the defaults are a starting point: the easiest way to tune them is to ask Claude Code
("tune the extraction for project X"), which inspects the PDF, adjusts the settings and checks the result.
Set it up *before* you start translating — changing the page layout settings later can shift paragraph ids.

### Claude settings

- The app's Claude buttons use your `claude` CLI login (Claude subscription or API key) and count
  toward its usage; each finished task shows its result at the top of the page.
- Headless runs may only read files, edit files in this folder and run `python3 tools/tm.py`.
- To pin a model for a project, add `"claude_model": "<model id>"` to its `project.json`.

## Your texts stay private

`projects/` is in `.gitignore` (except the example), so source texts, page images and translations are
never committed. Keep it that way for copyrighted material.

## License

MIT — see [LICENSE](LICENSE).
