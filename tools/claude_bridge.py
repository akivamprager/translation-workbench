"""Run Claude Code headless from the workbench app.

The server hands tasks ("translate page 5", "handle requests", a free-form message) to the
Claude Code CLI in non-interactive mode (`claude -p`), in this repository, so Claude follows
CLAUDE.md and writes through tools/tm.py exactly as in an interactive session. The app picks up
the results through its normal live refresh.

Uses whatever account the `claude` CLI is logged in with (a Claude subscription or an API key).
Tasks run one at a time, in order. Claude is limited to reading files, writing/editing files in the
repository (decisions.md, and JSON hand-off files in projects/<name>/.inbox/) and running `python3 tools/tm.py …`.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_TOOLS = ["Read", "Edit", "Write", "Bash(python3 tools/tm.py:*)"]

SYSTEM = """You are running headless, started from the translation workbench app; no human is watching
this session and you cannot ask questions. The active project is "{project}" (TM_PROJECT is set).
Follow CLAUDE.md exactly. To save translations or alternatives, write the JSON with the Write tool to
projects/{project}/.inbox/<name>.json and then run `python3 tools/tm.py put <that path>` (or `alt`);
tm.py deletes the file after importing it. Don't create files anywhere else, and don't pipe JSON through the
shell. If something is ambiguous, make the best choice and mention it in the page notes. Finish with a
summary of 1–3 short lines in the project's target language."""


def find_claude():
    for p in (shutil.which("claude"), Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude",
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if p and Path(p).is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def describe(task):
    kind = task["kind"]
    if kind == "translate":
        pages = task["pages"]
        return f"מתרגם את עמוד {pages[0]}" if len(pages) == 1 else f"מתרגם את עמודים {pages[0]}–{pages[-1]}"
    if kind == "requests":
        return "מטפל בבקשות"
    return "עונה להודעה"


def prompt_for(task):
    kind = task["kind"]
    if kind == "translate":
        pages = task["pages"]
        return f"תרגם עמוד {pages[0]}" if len(pages) == 1 else f"תרגם עמודים {pages[0]}–{pages[-1]}"
    if kind == "requests":
        return "טפל בבקשות"
    return task["text"]


def _tool_summary(name, inp):
    if name == "Bash":
        cmd = inp.get("command", "")
        for key, label in (("tm.py show", "קורא את טקסט המקור"), ("tm.py put", "כותב את התרגום"),
                           ("tm.py alt", "כותב חלופות"), ("tm.py edits", "בודק את התיקונים הידניים"),
                           ("tm.py requests", "קורא את הבקשות"), ("tm.py status", "בודק מצב")):
            if key in cmd:
                return label
        return "מריץ פקודה"
    if name == "Read":
        f = Path(inp.get("file_path", "")).name
        return {"style-guide.md": "קורא את הנחיות התרגום", "decisions.md": "קורא את ההחלטות"}.get(f, f"קורא {f}")
    if name == "Edit":
        return f"מעדכן {Path(inp.get('file_path', '')).name}"
    return name


class Bridge:
    def __init__(self):
        self.claude = find_claude()
        self.lock = threading.Lock()
        self.jobs = []          # newest last; kept short
        self.proc = None
        self.next_id = 1
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- public --------------------------------------------------------------
    def status(self):
        with self.lock:
            return {
                "available": bool(self.claude),
                "jobs": [self._public(j) for j in self.jobs[-8:]],
            }

    def submit(self, project, task):
        if not self.claude:
            raise ValueError("Claude Code CLI not found (install it and log in: https://claude.com/claude-code)")
        with self.lock:
            # don't queue the same thing twice
            for j in self.jobs:
                if j["status"] in ("queued", "running") and j["project"] == project and j["task"] == task \
                        and task["kind"] != "message":
                    return self._public(j)
            job = {"id": self.next_id, "project": project, "task": task, "label": describe(task),
                   "status": "queued", "progress": "", "result": "", "cost": None,
                   "queued_at": time.time(), "started": None, "finished": None}
            self.next_id += 1
            self.jobs.append(job)
            self.jobs = self.jobs[-30:]
        self.wake.set()
        return self._public(job)

    def cancel(self, job_id):
        with self.lock:
            for j in self.jobs:
                if j["id"] == job_id:
                    if j["status"] == "queued":
                        j["status"] = "cancelled"
                    elif j["status"] == "running" and self.proc:
                        j["status"] = "cancelled"
                        self.proc.terminate()

    # ---- internals -----------------------------------------------------------
    @staticmethod
    def _public(j):
        return {k: j[k] for k in ("id", "project", "task", "label", "status", "progress", "result", "cost", "finished")}

    def _worker(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            while True:
                with self.lock:
                    job = next((j for j in self.jobs if j["status"] == "queued"), None)
                    if job:
                        job["status"], job["started"] = "running", time.time()
                if not job:
                    break
                self._run(job)

    def _run(self, job):
        from tmlib import PROJECTS  # local import: tools/ is on sys.path in the server
        cfg = json.loads((PROJECTS / job["project"] / "project.json").read_text(encoding="utf-8"))
        cmd = [self.claude, "-p", prompt_for(job["task"]),
               "--output-format", "stream-json", "--verbose",
               "--permission-mode", "acceptEdits",
               "--allowedTools", *ALLOWED_TOOLS,
               "--disallowedTools", "NotebookEdit",
               "--append-system-prompt", SYSTEM.format(project=job["project"])]
        if cfg.get("claude_model"):
            cmd += ["--model", cfg["claude_model"]]
        env = dict(os.environ, TM_PROJECT=job["project"])
        result, cost, failed = "", None, False
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True)
            with self.lock:
                self.proc = proc
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "assistant":
                    for part in ev.get("message", {}).get("content", []):
                        if part.get("type") == "tool_use":
                            with self.lock:
                                job["progress"] = _tool_summary(part.get("name", ""), part.get("input", {}))
                elif ev.get("type") == "result":
                    result = ev.get("result") or ""
                    cost = ev.get("total_cost_usd")
                    failed = bool(ev.get("is_error"))
            proc.wait()
            if proc.returncode and not result:
                failed = True
                result = (proc.stderr.read() or f"claude exited with code {proc.returncode}")[-800:]
        except OSError as e:
            failed, result = True, str(e)
        with self.lock:
            self.proc = None
            if job["status"] != "cancelled":
                job["status"] = "failed" if failed else "done"
            job.update(result=result.strip(), cost=cost, finished=time.time(), progress="")
