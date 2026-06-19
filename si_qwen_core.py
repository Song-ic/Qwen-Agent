#!/usr/bin/env python3
"""In-process coding agent used by si-qwen-mcp.

The MCP server owns the agent loop and talks to oMLX directly. This module has
no third-party dependencies so the stdio server remains easy to install.
"""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


DEFAULT_BASE = "http://127.0.0.1:18888/v1"
DEFAULT_MODEL = "Qwen3.6-35B-A3B-oQ6-fp16-mtp"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_MAX_TURNS = 24
EXTEND_TURNS = 8
MAX_TOTAL_TURNS = 32
OMLX_RETRY_ATTEMPTS = 3
OMLX_RETRY_BASE_DELAY = 6
WRITE_FILE_LINE_THRESHOLD = 40
CONTEXT_THRESHOLD_TOKENS = 28_000
CONTEXT_TARGET_TOKENS = 24_000
RECENT_ROUNDS_TO_KEEP = 4
TOOL_OUTPUT_LIMIT = 12_000

SYSTEM_PROMPT = """You are a precise, autonomous coding agent working inside a real software project.
Complete coding tasks end-to-end: explore, reason, edit with tools, and verify.

# Procedure
1. Locate code with search_symbol or grep. Read a file before editing it.
2. Plan the exact files and order briefly.
3. Apply minimal edits with edit_file, write_file, or batch_replace (for bulk literal replacements across many files).
4. Verify every change with read_file or run_bash.
5. When complete and verified, return a short final summary without more tool calls.

# Tool and context rules
- Tool output may be truncated. Narrow the query or use read_file line ranges when needed.
- An unchanged file may return a snapshot reference instead of duplicate content. The full
  snapshot is already in the retained context.
- Older rounds may be compacted. The original task, change summary, and recent rounds remain.
- Never invent paths. Never ask the user questions; make a reasonable choice and proceed.
- Keep edits surgical. Do not refactor unrelated code.
- If edit_file fails, re-read the relevant range and retry with exact text.
- NEVER use write_file on existing files. Always use edit_file for changes.
- Your first action must be a tool call.

# Change discipline — what you must NOT do
- Do NOT modify any function, method, or class not named in the task.
- Do NOT change formatting, comments, import order, or variable names outside the edit scope.
- Do NOT add error handling, logging, validation, or try-catch beyond what the task asks.
- Do NOT create new files unless the task explicitly requires new files.
- Do NOT add new imports or dependencies unless the edit requires them.
- Do NOT create abstractions (interfaces, wrappers, helpers) for single-use code.
- If you notice an unrelated issue, mention it in your final summary. Do not fix it.

# Verify-after-edit — mandatory
- After EVERY edit_file, immediately verify: read_file the changed lines OR run_bash to compile/test.
- Do NOT make a second edit_file before verifying the first one succeeded and is correct.
- If a task can be solved by changing 1 line, change 1 line. Do not rewrite the surrounding block.
"""

CODEGRAPH_PROMPT = """
# Code index
This project has .codegraph/codegraph.db. Prefer search_symbol for definitions and
find_references for dependency checks. Use grep for literals and patterns.
"""

TOOLS_BASE = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files and directories under a project-relative path.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": (
            "Read a project file. Use optional 1-based start_line/end_line for large files. "
            "Repeated unchanged snapshots are not duplicated while retained in context."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search a regex in a file or directory. Returns at most 60 matches.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact unique string in an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        }, "required": ["path", "old_string", "new_string"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create a file or fully replace an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "run_bash",
        "description": "Run a verification/build/test shell command from the project root.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]},
    }},
]

TOOLS_BASE.append({"type": "function", "function": {
    "name": "batch_replace", "description": "Apply literal find-and-replace across multiple files in one call. Each entry replaces ALL occurrences of 'find' with 'replace'. Optional 'import' field adds an import line after existing imports. Use for mechanical bulk replacements.",
    "parameters": {"type": "object", "properties": {"replacements": {"type": "array", "items": {"type": "object", "properties": {"file": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}, "import": {"type": "string"}}, "required": ["file", "find", "replace"]}}}, "required": ["replacements"]},
}})

TOOLS_CODEGRAPH = [
    {"type": "function", "function": {
        "name": "search_symbol",
        "description": "Search indexed code symbols by partial name.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "find_references",
        "description": "Find callers and/or callees of an indexed symbol.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["callers", "callees", "both"]},
        }, "required": ["symbol"]},
    }},
]


@dataclass(frozen=True)
class ProcessLockToken:
    path: str
    inode: int
    pid: int


def _lock_owner_pid(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def acquire_process_lock(
    path,
    wait_timeout=600,
    poll_interval=0.1,
    progress=None,
):
    """Atomically wait for the cross-client dispatch slot."""
    progress = progress or (lambda _message: None)
    started = time.monotonic()
    last_notice = 0.0
    pid = os.getpid()

    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            owner = _lock_owner_pid(path)
            if owner is not None and not _pid_is_alive(owner):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                continue

            elapsed = time.monotonic() - started
            if elapsed >= wait_timeout:
                raise TimeoutError(
                    f"Timed out after {wait_timeout}s waiting for dispatch lock "
                    f"(owner PID {owner or 'unknown'})."
                )
            if elapsed - last_notice >= 2.0:
                progress(
                    f"waiting for oMLX dispatch slot; active owner PID "
                    f"{owner or 'unknown'}, waited {elapsed:.1f}s"
                )
                last_notice = elapsed
            time.sleep(poll_interval)
            continue

        try:
            os.write(descriptor, str(pid).encode("ascii"))
            inode = os.fstat(descriptor).st_ino
        finally:
            os.close(descriptor)
        return ProcessLockToken(path=path, inode=inode, pid=pid)


def release_process_lock(token):
    """Release only the inode created by this owner."""
    try:
        stat = os.stat(token.path)
    except FileNotFoundError:
        return
    if stat.st_ino != token.inode:
        return
    if _lock_owner_pid(token.path) != token.pid:
        return
    try:
        os.unlink(token.path)
    except FileNotFoundError:
        pass


def truncate_output(text, limit=TOOL_OUTPUT_LIMIT):
    """Keep useful head/tail context while bounding every tool result."""
    text = str(text)
    if len(text) <= limit:
        return text
    head_size = max(1, int(limit * 0.62))
    tail_size = max(1, limit - head_size)
    omitted = len(text) - head_size - tail_size
    notice = f"\n... [truncated {omitted} chars; narrow the request] ...\n"
    return text[:head_size] + notice + text[-tail_size:]


def estimate_tokens(messages):
    """Conservative dependency-free estimate for mixed code/Chinese context."""
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return max(1, int(len(serialized) / 3.2))


def _history_blocks(messages):
    blocks = []
    current = []
    for message in messages[2:]:
        role = message.get("role")
        if role != "tool" and current:
            blocks.append(current)
            current = []
        current.append(message)
    if current:
        blocks.append(current)
    return blocks


def _compact_block(block, tool_limit=1600):
    compacted = []
    for message in block:
        item = dict(message)
        if item.get("role") == "tool":
            original = item.get("content", "")
            item["content"] = truncate_output(original, tool_limit)
            if len(original) > tool_limit and "[[FILE_SNAPSHOT " in item["content"]:
                item["content"] = item["content"].replace(
                    "[[FILE_SNAPSHOT ", "[[FILE_SUMMARY ", 1
                )
        elif isinstance(item.get("content"), str):
            item["content"] = truncate_output(item["content"], tool_limit)
        compacted.append(item)
    return compacted


def compact_messages(
    messages,
    task,
    change_summaries,
    threshold_tokens=CONTEXT_THRESHOLD_TOKENS,
    target_tokens=CONTEXT_TARGET_TOKENS,
    keep_recent_rounds=RECENT_ROUNDS_TO_KEEP,
):
    """Replace old rounds with deterministic memory before context becomes expensive."""
    if estimate_tokens(messages) <= threshold_tokens:
        return messages, False

    system = dict(messages[0])
    task_message = dict(messages[1])
    changes = change_summaries[-30:] or ["No successful file changes recorded yet."]
    memory = {
        "role": "user",
        "content": (
            "[CONTEXT COMPACTED]\n"
            f"Original task:\n{task}\n\n"
            "Successful changes retained:\n- " + "\n- ".join(changes) + "\n\n"
            "Continue from the recent rounds below. Re-read any file whose full snapshot "
            "is no longer present."
        ),
    }

    blocks = _history_blocks(messages)
    recent = blocks[-max(1, keep_recent_rounds):]
    candidate = [system, task_message, memory]
    for block in recent:
        candidate.extend(_compact_block(block))

    while estimate_tokens(candidate) > target_tokens and len(recent) > 1:
        recent.pop(0)
        candidate = [system, task_message, memory]
        for block in recent:
            candidate.extend(_compact_block(block))

    if estimate_tokens(candidate) > target_tokens:
        candidate = [system, task_message, memory]
        for block in recent:
            candidate.extend(_compact_block(block, tool_limit=700))

    return candidate, True


def _merge_tool_delta(tool_calls, fragment):
    index = int(fragment.get("index", len(tool_calls)))
    while len(tool_calls) <= index:
        tool_calls.append({
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        })
    target = tool_calls[index]
    if fragment.get("id"):
        target["id"] += fragment["id"]
    if fragment.get("type"):
        target["type"] = fragment["type"]
    function = fragment.get("function") or {}
    target["function"]["name"] += function.get("name") or ""
    target["function"]["arguments"] += function.get("arguments") or ""


def consume_sse(lines, on_delta=None):
    """Consume OpenAI-compatible SSE chunks and reconstruct one assistant message."""
    content = []
    reasoning = []
    tool_calls = []
    finish_reason = None

    for raw_line in lines:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        chunk = json.loads(payload)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("reasoning_content"):
            text = delta["reasoning_content"]
            reasoning.append(text)
            if on_delta:
                on_delta(text)
        if delta.get("content"):
            text = delta["content"]
            content.append(text)
            if on_delta:
                on_delta(text)
        for fragment in delta.get("tool_calls") or []:
            _merge_tool_delta(tool_calls, fragment)
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]

    return {
        "content": "".join(content),
        "reasoning_content": "".join(reasoning),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
    }


def stream_chat(
    messages,
    tools,
    base=DEFAULT_BASE,
    model=DEFAULT_MODEL,
    temperature=0.0,
    reasoning_effort=None,
    timeout=300,
    max_tokens=DEFAULT_MAX_TOKENS,
    on_delta=None,
):
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return consume_sse(response, on_delta)

def stream_chat_retry(*a, **kw):
    for i in range(OMLX_RETRY_ATTEMPTS):
        try: return stream_chat(*a, **kw)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if isinstance(e, urllib.error.HTTPError) or i >= OMLX_RETRY_ATTEMPTS - 1: raise
            time.sleep(OMLX_RETRY_BASE_DELAY * (2**i))

class AgentWorkspace:
    def __init__(self, root, allow=None):
        self.root = os.path.realpath(root)
        self.allow = self._parse_allow(allow)
        self.change_summaries = []
        codegraph = os.path.join(self.root, ".codegraph", "codegraph.db")
        self.codegraph_db = codegraph if os.path.isfile(codegraph) else None

    @staticmethod
    def _parse_allow(allow):
        if not allow:
            return None
        if isinstance(allow, str):
            values = allow.split(",")
        else:
            values = allow
        return [
            value.strip()[2:] if value.strip().startswith("./") else value.strip()
            for value in values if value.strip()
        ]

    def safe_path(self, path):
        full = os.path.realpath(os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ValueError(f"path escapes project root: {path}")
        return full

    def denied(self, path):
        if self.allow is None:
            return None
        relative = os.path.relpath(self.safe_path(path), self.root)
        if any(relative == pattern or fnmatch.fnmatch(relative, pattern) for pattern in self.allow):
            return None
        return (
            f"REFUSED: '{relative}' is outside the editable allowlist: "
            f"{', '.join(self.allow)}"
        )

    @staticmethod
    def _snapshot_is_retained(marker, messages):
        return any(marker in str(message.get("content") or "") for message in messages)

    def read_file(self, path, messages, start_line=None, end_line=None):
        full = self.safe_path(path)
        if not os.path.isfile(full):
            return f"ERROR: file not found: {path}"
        with open(full, encoding="utf-8") as handle:
            text = handle.read()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        line_suffix = ""
        if start_line is not None or end_line is not None:
            lines = text.splitlines()
            start = max(1, int(start_line or 1))
            end = min(len(lines), int(end_line or len(lines)))
            text = "\n".join(lines[start - 1:end])
            line_suffix = f" lines={start}-{end}"
        marker = f"[[FILE_SNAPSHOT {path} {digest}{line_suffix}]]"
        if self._snapshot_is_retained(marker, messages):
            return f"{marker}\n[unchanged; full snapshot already retained in context]"
        return f"{marker}\n{text}"

    def list_dir(self, path):
        full = self.safe_path(path)
        if not os.path.isdir(full):
            return f"ERROR: not a directory: {path}"
        entries = sorted(os.listdir(full))
        return "\n".join(entries) if entries else "(empty)"

    def grep(self, pattern, path="."):
        full = self.safe_path(path)
        rg = shutil.which("rg")
        command = (
            [rg, "-n", "--max-count=40", "--no-heading", "-e", pattern, full]
            if rg else
            ["grep", "-rn", "--max-count=40", "-e", pattern, full]
        )
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        output = (result.stdout or result.stderr).strip()
        if not output:
            return "(no matches)"
        prefix = self.root + os.sep
        lines = [line.replace(prefix, "") for line in output.splitlines()]
        suffix = f"\n... ({len(lines) - 60} more matches)" if len(lines) > 60 else ""
        return "\n".join(lines[:60]) + suffix

    def edit_file(self, path, old_string, new_string):
        refusal = self.denied(path)
        if refusal:
            return refusal
        full = self.safe_path(path)
        if not os.path.isfile(full):
            return f"ERROR: file not found: {path}"
        with open(full, encoding="utf-8") as handle:
            text = handle.read()
        count = text.count(old_string)
        if count == 0:
            return "ERROR: old_string not found. Re-read the relevant range and copy exact text."
        if count > 1:
            return f"ERROR: old_string is not unique ({count} matches). Add surrounding context."
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text.replace(old_string, new_string, 1))
        summary = f"edited {path}"
        self.change_summaries.append(summary)
        return f"OK: {summary}"

    def write_file(self, path, content):
        refusal = self.denied(path)
        if refusal:
            return refusal
        full = self.safe_path(path)
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as h:
                el = h.read().count("\n") + 1
            if el > WRITE_FILE_LINE_THRESHOLD:
                return f"ERROR: {path} has {el} lines. Use edit_file instead."
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
        summary = f"wrote {path}"
        self.change_summaries.append(summary)
        return f"OK: {summary}"

    def batch_replace(self, replacements):
        out = []
        for e in replacements:
            out.append(self._apply_replace(e["file"], e["find"], e["replace"]))
        return "\n".join(out) or "No replacements."
    def _apply_replace(self, path, find, repl):
        r = self.denied(path)
        if r: return r
        p = self.safe_path(path)
        if not os.path.isfile(p): return f"ERROR: {path} not found"
        return self._do_replace(p, path, find, repl)
    def _do_replace(self, full, path, find, repl):
        with open(full, encoding="utf-8") as f: t = f.read()
        n = t.count(find)
        if n == 0: return f"SKIP: {path} — not found"
        with open(full, "w", encoding="utf-8") as f: f.write(t.replace(find, repl))
        self.change_summaries.append(f"batch-replaced {path}")
        return f"OK: {path} — {n} replaced"
    def run_bash(self, command):
        result = subprocess.run(
            command,
            shell=True,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        return output if output else f"(exit {result.returncode}, no output)"

    def search_symbol(self, query, kind=None):
        if not self.codegraph_db:
            return "ERROR: codegraph not available."
        connection = sqlite3.connect(self.codegraph_db)
        connection.row_factory = sqlite3.Row
        try:
            sql = (
                "SELECT name, kind, file_path, start_line, end_line, signature "
                "FROM nodes WHERE lower(name) LIKE ? "
            )
            params = [f"%{query.lower()}%"]
            if kind:
                sql += "AND kind = ? "
                params.append(kind)
            sql += "ORDER BY CASE WHEN lower(name) = ? THEN 0 ELSE 1 END, name LIMIT 30"
            params.append(query.lower())
            rows = connection.execute(sql, params).fetchall()
        finally:
            connection.close()
        if not rows:
            return f"(no symbols matching '{query}')"
        return "\n".join(
            f"{row['kind']} {row['name']} {row['file_path']}:{row['start_line']}-{row['end_line']}"
            + (f" {row['signature']}" if row["signature"] else "")
            for row in rows
        )

    def find_references(self, symbol, direction="callers"):
        if not self.codegraph_db:
            return "ERROR: codegraph not available."
        connection = sqlite3.connect(self.codegraph_db)
        connection.row_factory = sqlite3.Row
        try:
            nodes = connection.execute(
                "SELECT id, name, kind, file_path, start_line FROM nodes "
                "WHERE lower(name) = ? LIMIT 5",
                [symbol.lower()],
            ).fetchall()
            output = []
            for node in nodes:
                output.append(
                    f"-- {node['kind']} {node['name']} @ "
                    f"{node['file_path']}:{node['start_line']}"
                )
                if direction in ("callers", "both"):
                    rows = connection.execute(
                        "SELECT n.name, n.kind, n.file_path, n.start_line, e.kind edge_kind "
                        "FROM edges e JOIN nodes n ON e.source=n.id WHERE e.target=? LIMIT 20",
                        [node["id"]],
                    ).fetchall()
                    output.extend(
                        f"caller [{row['edge_kind']}] {row['kind']} {row['name']} "
                        f"@ {row['file_path']}:{row['start_line']}" for row in rows
                    )
                if direction in ("callees", "both"):
                    rows = connection.execute(
                        "SELECT n.name, n.kind, n.file_path, n.start_line, e.kind edge_kind "
                        "FROM edges e JOIN nodes n ON e.target=n.id WHERE e.source=? LIMIT 20",
                        [node["id"]],
                    ).fetchall()
                    output.extend(
                        f"callee [{row['edge_kind']}] {row['kind']} {row['name']} "
                        f"@ {row['file_path']}:{row['start_line']}" for row in rows
                    )
        finally:
            connection.close()
        return "\n".join(output) if output else f"(no symbol '{symbol}' found)"

    def execute(self, name, arguments, messages):
        try:
            if name == "list_dir":
                result = self.list_dir(arguments.get("path", "."))
            elif name == "read_file":
                result = self.read_file(
                    arguments["path"],
                    messages,
                    arguments.get("start_line"),
                    arguments.get("end_line"),
                )
            elif name == "grep":
                result = self.grep(arguments["pattern"], arguments.get("path", "."))
            elif name == "edit_file":
                result = self.edit_file(
                    arguments["path"], arguments["old_string"], arguments["new_string"]
                )
            elif name == "write_file":
                result = self.write_file(arguments["path"], arguments["content"])
            elif name == "batch_replace":
                result = self.batch_replace(arguments.get("replacements", []))
            elif name == "run_bash":
                result = self.run_bash(arguments["command"])
            elif name == "search_symbol":
                result = self.search_symbol(arguments["query"], arguments.get("kind"))
            elif name == "find_references":
                result = self.find_references(
                    arguments["symbol"], arguments.get("direction", "callers")
                )
            else:
                result = f"ERROR: unknown tool '{name}'"
        except KeyError as error:
            result = f"ERROR: missing argument {error}"
        except Exception as error:
            result = f"ERROR: {type(error).__name__}: {error}"

        limits = {
            "read_file": 14_000,
            "grep": 8_000,
            "run_bash": 8_000,
        }
        return truncate_output(result, limits.get(name, 4_000))


class AgentRunner:
    def __init__(
        self,
        project,
        allow=None,
        base=DEFAULT_BASE,
        model=DEFAULT_MODEL,
        progress=None,
        max_turns=DEFAULT_MAX_TURNS,
        max_tokens=DEFAULT_MAX_TOKENS,
    ):
        self.workspace = AgentWorkspace(project, allow)
        self.base = base
        self.model = model
        self.progress = progress or (lambda _message: None)
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.compactions = 0

    def _emit(self, message):
        self.progress(str(message))

    def _tools(self):
        return TOOLS_BASE + (TOOLS_CODEGRAPH if self.workspace.codegraph_db else [])

    def run(self, task):
        prompt = SYSTEM_PROMPT + (CODEGRAPH_PROMPT if self.workspace.codegraph_db else "")
        if self.workspace.allow:
            task += (
                "\n\n[SCOPE ALLOWLIST] You may only create or edit: "
                + ", ".join(self.workspace.allow)
            )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": task},
        ]
        nudges = 0
        no_progress = 0
        effective_max = self.max_turns

        for turn in range(1, MAX_TOTAL_TURNS + 1):
            if turn > effective_max:
                break
            messages, compacted = compact_messages(
                messages,
                task,
                self.workspace.change_summaries,
            )
            if compacted:
                self.compactions += 1
                self._emit(
                    f"context compacted to about {estimate_tokens(messages)} tokens; "
                    "task, change summary, and recent rounds retained"
                )

            self._emit(f"turn {turn}: requesting {self.model}")
            try:
                response = stream_chat_retry(
                    messages,
                    self._tools(),
                    base=self.base,
                    model=self.model,
                    max_tokens=self.max_tokens,
                    on_delta=self._emit,
                )
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:600]
                return {"status": "http_error", "detail": f"{error.code} {detail}", "turns": turn}
            except Exception as error:
                return {
                    "status": "exception",
                    "detail": f"{type(error).__name__}: {error}",
                    "turns": turn,
                }

            content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []
            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tool_calls,
                })
                changed = False
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = function.get("name") or ""
                    raw_arguments = function.get("arguments") or "{}"
                    try:
                        arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        result = f"ERROR: arguments are not valid JSON: {raw_arguments[:300]}"
                    else:
                        self._emit(f"tool: {name}")
                        result = self.workspace.execute(name, arguments, messages)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call-{turn}",
                        "content": result,
                    })
                    if name in ("edit_file", "write_file") and result.startswith("OK:") or name == "batch_replace" and "OK:" in result:
                        changed = True
                    self._emit(f"{name}: {truncate_output(result, 500)}")

                no_progress = 0 if changed else no_progress + 1
                if changed and turn > effective_max - 4:
                    effective_max = min(effective_max + EXTEND_TURNS, MAX_TOTAL_TURNS)
                    self._emit(f"extending to {effective_max} turns")
                if no_progress >= 14:
                    return {
                        "status": "stuck",
                        "detail": "14 consecutive exploration rounds without edit_file/write_file.",
                        "turns": turn,
                    }
                continue

            stripped = content.strip()
            leaked = "to=functions." in stripped or stripped.startswith(("analysis", "commentary"))
            if stripped and not leaked:
                return {"status": "done", "detail": stripped, "turns": turn}

            if nudges < 5:
                nudges += 1
                messages.append({
                    "role": "user",
                    "content": (
                        "You ended without a tool call or final answer. Continue with the next "
                        "required tool, or return the final verified summary."
                    ),
                })
                self._emit(f"early stop; nudge {nudges}/5")
                continue
            return {"status": "early_stop", "detail": "stalled after 5 nudges", "turns": turn}

        return {
            "status": "max_turns",
            "detail": f"hit {effective_max} turns",
            "turns": effective_max,
        }


def check_backend(base=DEFAULT_BASE):
    with urllib.request.urlopen(f"{base}/models", timeout=10) as response:
        return [item.get("id", "?") for item in json.loads(response.read()).get("data", [])]


def _verify(command, project):
    return subprocess.run(
        command,
        shell=True,
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _diff_lines(project):
    result = subprocess.run(
        ["git", "-C", project, "diff", "--numstat"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    total = 0
    for line in result.stdout.splitlines():
        columns = line.split("\t")
        if len(columns) >= 2:
            total += int(columns[0]) if columns[0].isdigit() else 0
            total += int(columns[1]) if columns[1].isdigit() else 0
    return total


def run_dispatch(
    project,
    task,
    allow=None,
    verify=None,
    max_diff_lines=None,
    verify_retries=2,
    base=DEFAULT_BASE,
    progress=None,
):
    """Run one complete MCP dispatch without launching another agent process."""
    progress = progress or (lambda _message: None)
    started = time.time()
    runner = AgentRunner(project, allow=allow, base=base, progress=progress)
    outcome = runner.run(task)
    verification = None

    if verify and outcome["status"] == "done":
        for attempt in range(max(0, verify_retries) + 1):
            progress(f"verification {attempt + 1}/{max(0, verify_retries) + 1}: {verify}")
            result = _verify(verify, project)
            if result.returncode == 0:
                verification = "PASS"
                progress("verification PASS")
                break
            verification = "FAIL"
            error_text = truncate_output((result.stdout + result.stderr).strip(), 2500)
            progress(f"verification FAIL: {error_text}")
            if attempt >= max(0, verify_retries):
                outcome["status"] = "verify_failed"
                outcome["detail"] = error_text
                break
            fix_task = (
                f"The previous changes failed `{verify}`.\n\nFailure:\n{error_text}\n\n"
                "Fix only the root cause, preserve scope, and verify the result."
            )
            fix_runner = AgentRunner(project, allow=allow, base=base, progress=progress)
            fix_outcome = fix_runner.run(fix_task)
            runner.workspace.change_summaries.extend(fix_runner.workspace.change_summaries)
            runner.compactions += fix_runner.compactions
            if fix_outcome["status"] != "done":
                outcome = fix_outcome
                break

    diff_lines = None
    tripwire = False
    if max_diff_lines is not None and max_diff_lines >= 0:
        try:
            diff_lines = _diff_lines(project)
            tripwire = diff_lines > max_diff_lines
        except Exception as error:
            progress(f"diff check skipped: {error}")

    elapsed = time.time() - started
    return {
        **outcome,
        "seconds": elapsed,
        "verify": verification,
        "diff_lines": diff_lines,
        "tripwire": tripwire,
        "compactions": runner.compactions,
        "changes": runner.workspace.change_summaries,
    }


def format_dispatch_result(result):
    lines = [
        "exit_code: " + ("0" if result.get("status") == "done" else "1"),
        "",
        (
            f"[si-qwen] status={result.get('status')} turns={result.get('turns')} "
            f"{result.get('seconds', 0):.1f}s compactions={result.get('compactions', 0)}"
        ),
        "=" * 60,
        result.get("detail", ""),
    ]
    if result.get("verify"):
        lines.append(f"验证 {result['verify']}")
    if result.get("diff_lines") is not None:
        lines.append(f"diff 规模 {result['diff_lines']} 行")
    if result.get("tripwire"):
        lines.append("⚠ DIFF 超预期")
    if result.get("changes"):
        lines.append("changes: " + ", ".join(result["changes"]))
    return "\n".join(lines)
