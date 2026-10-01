#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026, Aaron Conole <aconole@bytheb.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Per-commit LLM analysis for git repositories.

Reviews one commit, a list of commits, or a range of commits using an LLM
with read-only tool access (file reading, directory listing, git show, git
log, grep, sed-based reads, awk-based reads).  TodoWrite additionally
maintains an in-memory task list; it never touches the repository.

Providers:
  anthropic  Anthropic Messages API (x-api-key or bearer token auth)
  openai     Any OpenAI-compatible /chat/completions endpoint
  vertex     Google Vertex AI (Gemini generateContent, or OpenAI-compatible
             models published on Vertex), using token/ADC authentication

Auth sources, in order of precedence:
  --token, --api-key, --env-var NAME, the provider default environment
  variable, and for vertex: gcloud auth print-access-token or google.auth
  application default credentials.

Context:
  AGENTS.md at the repository root is loaded automatically (disable with
  --no-agents); extra files may be added with --agents-file.  Skill files
  (markdown, optional YAML frontmatter) are discovered from --skill-dir
  (default: .skills) and can be loaded into context with --skill NAME or
  --skill PATH; the model can also fetch any discovered skill on demand via
  the load_skill tool.

Commit specifications:
  3fed0e7c2      a single commit (any rev git understands)
  HEAD..HEAD~6   a range (rev-list semantics)
  -4             shorthand for the last 4 commits (HEAD~4..HEAD)

The review process starts from a safe, read-only instruction set built into
the system prompt; all tools are read-only and confined to the repository,
with the single exception of TodoWrite, which only tracks the reviewer's
own progress in memory.
"""

import argparse
import fnmatch
import json
import os
import re
import sys

from commit_reviewer.constants import (
    TOOL_OUTPUT_LIMIT, SUBPROCESS_TIMEOUT, HTTP_RETRIES, MAX_GREP_RESULTS,
    MAX_LOG_ENTRIES, MAX_LIST_ENTRIES, MAX_FILE_LINES, MAX_FILE_BYTES,
    SEVERITIES, SYSTEM_PROMPT)
from commit_reviewer.util import (
    ToolError, die, warn, clip, run_git, run_external,
    safe_abs_path, check_pathspec)
from commit_reviewer.gitcommits import (
    RE_REV, parse_commit_specs, commit_context)
from commit_reviewer.context import (
    load_markdown_file, parse_frontmatter, discover_skills,
    build_system_prompt, load_agents_files, resolve_selected_skills)
from commit_reviewer.providers import build_provider


# ---------------------------------------------------------------------------
# Tool implementations (all read-only, confined to the repository root;
# TodoWrite only tracks in-memory review progress)
# ---------------------------------------------------------------------------


def tool_read_file(root, args, verbose):
    path = safe_abs_path(root, args.get("path"))
    if not os.path.isfile(path):
        raise ToolError("not a file: %s" % args.get("path"))
    st = os.stat(path)
    start = args.get("start_line") or 1
    end = args.get("end_line")
    if end is None:
        end = start + MAX_FILE_LINES - 1
    if not isinstance(start, int) or not isinstance(end, int) or \
            isinstance(start, bool) or isinstance(end, bool) or \
            start < 1 or end < start:
        raise ToolError("invalid start_line/end_line")
    if end - start + 1 > MAX_FILE_LINES:
        warn(f"read_file: {path} has large range.  Truncating [{start}:{end}/{MAX_FILE_LINES}]")
        end = start + MAX_FILE_LINES - 1
    with open(path, "r", errors="replace") as f:
        lines = []
        for i, line in enumerate(f, 1):
            if i > end:
                break
            if i >= start:
                lines.append("%6d  %s" % (i, line.rstrip("\n")))
    if not lines:
        return "(file is empty or range is out of bounds)"
    return "%s\n[%d line(s) shown, range %d-%d]" % (
        "\n".join(lines), len(lines), start, end)


def tool_list_directory(root, args, verbose):
    path = safe_abs_path(root, args.get("path"))
    if not os.path.isdir(path):
        raise ToolError("not a directory: %s" % args.get("path"))
    entries = sorted(os.listdir(path))
    lines = []
    for e in entries[:MAX_LIST_ENTRIES]:
        full = os.path.join(path, e)
        if os.path.isdir(full) and not os.path.islink(full):
            lines.append("%s/" % e)
        elif os.path.islink(full):
            lines.append("%s -> %s" % (e, os.readlink(full)))
        else:
            lines.append(e)
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append("[... %d more entries ...]"
                     % (len(entries) - MAX_LIST_ENTRIES))
    return "listing of %s:\n%s" % (path, "\n".join(lines) or "(empty)")


def tool_git_show(root, args, verbose):
    rev = args.get("rev")
    if not rev or not RE_REV.match(rev):
        raise ToolError("invalid rev: %r" % (rev,))
    file_ = args.get("file")
    if file_ is not None:
        check_pathspec(file_)
        out = run_git(["show", "%s:%s" % (rev, file_)])
        return "git show %s:%s\n%s" % (rev, file_, out)
    return run_git(["show", "--no-color", "--format=fuller", rev])


def tool_git_log(root, args, verbose):
    rev = args.get("rev") or "HEAD"
    if not RE_REV.match(rev):
        raise ToolError("invalid rev: %r" % (rev,))
    count = args.get("max_count") or 20
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ToolError("invalid max_count")
    count = min(count, MAX_LOG_ENTRIES)
    oneline = args.get("oneline", True)
    file_ = args.get("file")
    cmd = ["log", "--no-color", "--max-count", str(count)]
    if oneline:
        cmd.append("--oneline")
    else:
        cmd += ["--format=%h %ad %an %s", "--date=short"]
    cmd.append(rev)
    if file_ is not None:
        check_pathspec(file_)
        cmd += ["--", file_]
    out = run_git(cmd)
    if not out.strip():
        return "no commits found for: git log %s" % " ".join(cmd[1:])
    return "git log %s\n%s" % (" ".join(cmd[1:]), out)


def tool_grep(root, args, verbose):
    pattern = args.get("pattern")
    if not pattern or not isinstance(pattern, str):
        raise ToolError("pattern is required")
    base = safe_abs_path(root, args.get("path"))
    if not os.path.exists(base):
        raise ToolError("no such path: %s" % args.get("path"))
    include = args.get("include")
    try:
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        regex = re.compile(re.escape(pattern)
                           if args.get("fixed_strings") else pattern,
                           flags)
    except re.error as e:
        raise ToolError("invalid pattern: %s" % e)
    try:
        max_results = int(args.get("max_results") or 100)
    except (TypeError, ValueError):
        raise ToolError("invalid max_results")
    max_results = max(1, min(max_results, MAX_GREP_RESULTS))
    matches = []

    def grep_file(full):
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if b"\x00" in data[:8192]:
            return None
        text = data.decode("utf-8", "replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = os.path.relpath(full, root)
                matches.append("%s:%d: %s"
                               % (rel, lineno, line.strip()))
                if len(matches) >= max_results:
                    return

    def walk(d):
        if len(matches) >= max_results:
            return
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        for e in entries:
            if len(matches) >= max_results:
                return
            full = os.path.join(d, e)
            if e == ".git":
                continue
            if os.path.isdir(full) and not os.path.islink(full):
                walk(full)
            elif os.path.isfile(full):
                if include and not fnmatch.fnmatch(e, include):
                    continue
                grep_file(full)

    if os.path.isdir(base):
        walk(base)
    elif os.path.isfile(base):
        grep_file(base)

    if not matches:
        return "no matches for %r under %s%s" % (
            pattern, base,
            (" (include: %s)" % include) if include else "")
    out = "\n".join(matches)
    if len(matches) >= max_results:
        out += "\n[... %d matches shown, limit reached ...]" % max_results
    return out


def tool_load_skill(root, args, verbose, skills):
    name = args.get("name")
    if not name or not isinstance(name, str):
        raise ToolError("name is required")
    if name not in skills:
        return "unknown skill %r.  Available skills: %s" % (
            name, ", ".join(sorted(skills)) or "(none)")
    sk = skills[name]
    return "skill %s (%s):\n%s" % (name, sk["path"], sk["text"])


# -- sed / awk read-only validators and runners -----------------------------

SED_ALLOWED_CMDS = set("pnd=lhsHgytbNPDx")
SED_S_FLAGS = set("gInp0123456789")


def split_sed_commands(script):
    cmds = []
    cur = []
    in_q = False
    in_re = False
    qch = ""
    i = 0
    while i < len(script):
        ch = script[i]
        if in_q:
            cur.append(ch)
            if ch == qch:
                in_q = False
            i += 1
            continue
        if in_re:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(script):
                cur.append(script[i + 1])
                i += 1
            elif ch == "/":
                in_re = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_q = True
            qch = ch
            cur.append(ch)
        elif ch == "/":
            in_re = True
            cur.append(ch)
        elif ch in (";", "\n"):
            cmds.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    cmds.append("".join(cur))
    return [c.strip() for c in cmds if c.strip()]


def sed_cmd_index(cmd):
    """Return the index of the command letter, skipping any address."""
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c in "0123456789$!+-~":
            i += 1
        elif c == "/":
            i += 1
            while i < n and cmd[i] != "/":
                if cmd[i] == "\\" and i + 1 < n:
                    i += 1
                i += 1
            i += 1
        elif c in (",", " "):
            i += 1
        else:
            break
    return i if i < n else None


def validate_sed(script):
    """Reject sed constructs that can write files or execute commands."""
    for cmd in split_sed_commands(script):
        j = sed_cmd_index(cmd)
        if j is None:
            continue
        letter = cmd[j]
        if letter not in SED_ALLOWED_CMDS:
            raise ToolError(
                "sed_read: command %r is not allowed (read-only commands "
                "only: p n d = l h H g G y t b N P D x s)" % letter)
        if letter == "s":
            if j + 1 >= len(cmd):
                raise ToolError("sed_read: malformed s command")
            d = cmd[j + 1]
            # d is delimiter 1; find delimiter 2 and 3 (unescaped).
            k = j + 2
            seen = 1
            while k < len(cmd) and seen < 3:
                if cmd[k] == "\\" and k + 1 < len(cmd):
                    k += 2
                    continue
                if cmd[k] == d:
                    seen += 1
                k += 1
            if seen < 3:
                raise ToolError("sed_read: unterminated s command")
            for f in cmd[k:]:
                if f not in SED_S_FLAGS:
                    raise ToolError(
                        "sed_read: s-command flag %r is not allowed "
                        "(no 'e' execute flag, no writes)" % f)
    return True


AWK_BLOCKED_CHECKS = [
    (re.compile(r"\bsystem\s*\(", re.I), "system()"),
    (re.compile(r"\bgetline\b", re.I), "getline"),
    (re.compile(r"\bclose\s*\(", re.I), "close()"),
    (re.compile(r"\bENVIRON\b", re.I), "ENVIRON"),
    (re.compile(r"\bPROCINFO\b", re.I), "PROCINFO"),
    (re.compile(r"\bnextfile\b", re.I), "nextfile"),
    (re.compile(r"@load|@include"), "@load/@include extension"),
    (re.compile(r"`"), "backtick command execution"),
    # A single '|' is awk's command pipe (print | "cmd", "cmd" | getline);
    # '||' is plain logical-or and must stay usable in conditions.
    (re.compile(r"(?<!\|)\|(?!\|)"), "pipe (command execution)"),
    (re.compile(r"\b(print|printf)\b[^|\n]*>", re.I),
     "print redirection (file write)"),
]


def validate_awk(program):
    """Reject awk constructs that can execute commands or write files."""
    for rx, label in AWK_BLOCKED_CHECKS:
        if rx.search(program):
            raise ToolError("awk_read: %s is not allowed "
                            "(read-only programs only)" % label)
    return True


def tool_sed_read(root, args, verbose):
    path = safe_abs_path(root, args.get("path"))
    if not os.path.isfile(path):
        raise ToolError("not a file: %s" % args.get("path"))
    expr = args.get("expression")
    if not expr or not isinstance(expr, str):
        raise ToolError("expression is required")
    if len(expr) > 4096:
        raise ToolError("expression too long (max 4096)")
    validate_sed(expr)
    return run_external(["sed", "-n", "-e", expr, path])


def tool_awk_read(root, args, verbose):
    path = safe_abs_path(root, args.get("path"))
    if not os.path.isfile(path):
        raise ToolError("not a file: %s" % args.get("path"))
    program = args.get("program")
    if not program or not isinstance(program, str):
        raise ToolError("program is required")
    if len(program) > 8192:
        raise ToolError("program too long (max 8192)")
    validate_awk(program)
    return run_external(["awk", program, path])


TODO_STATUSES = ("pending", "in_progress", "completed")
MAX_TODO_ITEMS = 100


def tool_todo_write(state, args):
    """Replace the in-memory todo list for the commit under review.

    The list is session state only; nothing is written to the repository.
    """
    todos = args.get("todos")
    if not isinstance(todos, list):
        raise ToolError("todos must be an array")
    if len(todos) > MAX_TODO_ITEMS:
        raise ToolError("too many todos (max %d)" % MAX_TODO_ITEMS)
    out = []
    for i, item in enumerate(todos):
        if not isinstance(item, dict):
            raise ToolError("todo %d is not an object" % i)
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ToolError("todo %d has no content" % i)
        status = item.get("status") or "pending"
        if status not in TODO_STATUSES:
            raise ToolError("todo %d has invalid status: %r"
                            % (i, status))
        active = item.get("activeForm")
        if not isinstance(active, str) or not active.strip():
            active = content
        out.append({"content": content, "status": status,
                    "activeForm": active})
    state["todos"] = out
    done = sum(1 for t in out if t["status"] == "completed")
    if state.get("verbose"):
        marks = {"completed": "x", "in_progress": ">", "pending": " "}
        warn("todos: %d item(s), %d completed" % (len(out), done))
        for t in out:
            suffix = " (%s)" % t["activeForm"] \
                if t["status"] == "in_progress" else ""
            warn("  [%s] %s%s" % (marks[t["status"]], t["content"], suffix))
    return "Todo list updated: %d item(s), %d completed. Continue the " \
           "review and keep the list current." % (len(out), done)


def tool_schemas():
    return [
        {
            "name": "read_file",
            "description": "Read a text file from the repository working "
                           "tree, optionally a line range. Output includes "
                           "line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "File path relative to the "
                                            "repo root"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
        },
        {
            "name": "list_directory",
            "description": "List entries of a directory in the repository "
                           "working tree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Directory path (default: "
                                            "repo root)"},
                },
            },
        },
        {
            "name": "git_show",
            "description": "Show a commit with its diff (git show), or a "
                           "file's contents at a given revision "
                           "(rev:path).",
            "parameters": {
                "type": "object",
                "properties": {
                    "rev": {"type": "string",
                            "description": "Commit hash or git rev"},
                    "file": {"type": "string",
                             "description": "Optional path; when given, "
                                            "shows the file at that rev "
                                            "instead of the commit"},
                },
                "required": ["rev"],
            },
        },
        {
            "name": "git_log",
            "description": "Run git log over a rev/range, optionally "
                           "limited to a file, with a bounded number of "
                           "entries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rev": {"type": "string",
                            "description": "Rev or range (default HEAD)"},
                    "file": {"type": "string",
                             "description": "Optional path to limit "
                                            "history to"},
                    "max_count": {"type": "integer", "minimum": 1,
                                  "maximum": MAX_LOG_ENTRIES},
                    "oneline": {"type": "boolean"},
                },
            },
        },
        {
            "name": "grep",
            "description": "Search file contents in the repository with a "
                           "regex (extended regex, like grep -E). Skips "
                           "binary files and .git. Results are "
                           "path:line: text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string",
                             "description": "Directory or file to search "
                                            "(default: repo root)"},
                    "include": {"type": "string",
                                "description": "Glob filter on file name, "
                                               "e.g. '*.c'"},
                    "ignore_case": {"type": "boolean"},
                    "fixed_strings": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1,
                                    "maximum": MAX_GREP_RESULTS},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "sed_read",
            "description": "Read a file through a read-only sed script, "
                           "as in 'sed -n -e SCRIPT FILE'. Only read-only "
                           "sed commands are permitted (p, n, d, =, l, h, "
                           "H, g, G, y, t, b, N, P, D, x, and s without "
                           "the e flag).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expression": {"type": "string",
                                   "description": "sed script, e.g. "
                                                  "'10,40p' or "
                                                  "'/^ovs_/,/^}/p'"},
                },
                "required": ["path", "expression"],
            },
        },
        {
            "name": "awk_read",
            "description": "Read a file through a read-only awk program, "
                           "as in 'awk PROGRAM FILE'. The program must "
                           "not use system(), getline, close(), ENVIRON, "
                           "pipes, or print redirection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "program": {"type": "string",
                                "description": "awk program, e.g. "
                                               "'length > 120 "
                                               "{print NR}'"},
                },
                "required": ["path", "program"],
            },
        },
        {
            "name": "load_skill",
            "description": "Load the full contents of a skill file by "
                           "name from the list of available skills.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "TodoWrite",
            "description": "Create or replace the todo list used to track "
                           "progress through the review of the commit "
                           "under analysis. Pass the complete list on "
                           "every call and update statuses as you make "
                           "progress. Only updates in-memory session "
                           "state; never touches the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string",
                                            "description": "What needs "
                                                           "to be done"},
                                "status": {"type": "string",
                                           "enum": list(TODO_STATUSES)},
                                "activeForm": {"type": "string",
                                                "description":
                                                "Present-tense label "
                                                "shown while the item "
                                                "is in progress"},
                            },
                            "required": ["content", "status",
                                         "activeForm"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    ]


def run_tool(name, args, state):
    root = state["root"]
    if name == "read_file":
        return tool_read_file(root, args, state["verbose"])
    if name == "list_directory":
        return tool_list_directory(root, args, state["verbose"])
    if name == "git_show":
        return tool_git_show(root, args, state["verbose"])
    if name == "git_log":
        return tool_git_log(root, args, state["verbose"])
    if name == "grep":
        return tool_grep(root, args, state["verbose"])
    if name == "sed_read":
        return tool_sed_read(root, args, state["verbose"])
    if name == "awk_read":
        return tool_awk_read(root, args, state["verbose"])
    if name == "load_skill":
        return tool_load_skill(root, args, state["verbose"],
                               state["skills"])
    if name == "TodoWrite":
        return tool_todo_write(state, args)
    raise ToolError("unknown tool: %s" % name)


# ---------------------------------------------------------------------------
# Review agent loop
# ---------------------------------------------------------------------------

FINDINGS_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_findings(text):
    """Extract the findings list from the final JSON block, if present."""
    for m in reversed(list(FINDINGS_BLOCK.finditer(text))):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and "findings" in data:
            findings = data["findings"]
            return findings if isinstance(findings, list) else []
    return None


CONTEXT_LENGTH_MARKERS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length",
    "exceeds the model's",
)

COMPACTED_TOOL_NOTICE = "[compacted: earlier tool output omitted to fit " \
                        "the model's context window]"

# Successive compaction passes: each is the number of most-recent tool
# results to leave untouched, replacing everything older with a short
# placeholder.  Shrinks further each retry until nothing is left to trim.
COMPACTION_LEVELS = (6, 3, 1, 0)


def is_context_length_error(err):
    msg = str(err).lower()
    return any(marker in msg for marker in CONTEXT_LENGTH_MARKERS)


def compact_messages(messages, keep_recent_tool_turns):
    """Replace older tool-result content with a short placeholder to shrink
    the conversation.  Leaves the most recent `keep_recent_tool_turns` tool
    results untouched.  Returns True if anything was actually changed."""
    tool_indices = [i for i, m in enumerate(messages)
                    if m["role"] == "tool"
                    and m["content"] != COMPACTED_TOOL_NOTICE]
    if keep_recent_tool_turns > 0:
        tool_indices = tool_indices[:-keep_recent_tool_turns]
    changed = False
    for i in tool_indices:
        messages[i]["content"] = COMPACTED_TOOL_NOTICE
        changed = True
    return changed


def chat_with_compaction(provider, system, messages, schemas, verbose):
    """Call provider.chat(), and if the server reports the prompt/context
    was too long, progressively compact earlier tool output and retry
    instead of failing the whole review."""
    for keep in COMPACTION_LEVELS:
        try:
            return provider.chat(system, messages, schemas, verbose)
        except ToolError as e:
            if not is_context_length_error(e):
                raise
            if not compact_messages(messages, keep):
                raise
            warn("context length exceeded; compacted tool history "
                 "(keeping %d most recent result(s)) and retrying" % keep)
    return provider.chat(system, messages, schemas, verbose)


def review_commit(sha, provider, system, state, max_turns):
    """Run the tool-calling loop for one commit.

    Returns (text, stats, thinking_log), where thinking_log is a list of
    (label, thinking_text) entries for every model reply that carried
    reasoning.
    """
    verbose = state["verbose"]
    show_thinking = state["show_thinking"]
    state["todos"] = []
    schemas = tool_schemas()
    messages = [{"role": "user",
                 "content": commit_context(sha, state["diff_max"])}]
    total_in = 0
    total_out = 0
    final_text = None
    thinking_log = []
    text = ""

    def log_thinking(label, thinking):
        parts = [b.get("text", "") for b in thinking or [] if b.get("text")]
        if not parts:
            return
        entry = "\n\n".join(parts)
        thinking_log.append((label, entry))
        if show_thinking:
            warn("=== %s thinking ===\n%s" % (label, entry))

    for turn in range(1, max_turns + 1):
        text, calls, usage, thinking = chat_with_compaction(provider, system,
                                                            messages, schemas,
                                                            verbose)
        total_in += usage.get("input", 0)
        total_out += usage.get("output", 0)
        if verbose:
            warn("turn %d: %d tool call(s), %d in / %d out tokens"
                 % (turn, len(calls), usage.get("input", 0),
                    usage.get("output", 0)))
        log_thinking("turn %d" % turn, thinking)
        if not calls:
            final_text = text
            break
        messages.append({"role": "assistant",
                         "content": text or None,
                         "tool_calls": calls,
                         "thinking": thinking})
        for tc in calls:
            args = tc["args"] if isinstance(tc["args"], dict) else {}
            if verbose:
                warn("  tool %s %s" % (tc["name"],
                                       json.dumps(args)[:200]))
            try:
                out = run_tool(tc["name"], args, state)
                is_error = False
            except ToolError as e:
                out = "[tool error] %s" % e
                is_error = True
            if verbose:
                warn("  -> %d char(s)%s" % (len(out),
                                            " (error)" if is_error else ""))
            messages.append({"role": "tool",
                             "tool_call_id": tc["id"],
                             "name": tc["name"],
                             "content": clip(out),
                             "is_error": is_error})
    if final_text is None:
        # Tool budget exhausted mid-loop: ask for the final answer once.
        messages.append({"role": "user",
                         "content": "The tool budget for this review is "
                                    "exhausted. End your review now with "
                                    "the summary and the final JSON "
                                    "findings block."})
        try:
            text, calls, usage, thinking = chat_with_compaction(
                provider, system, messages, schemas, verbose)
            total_in += usage.get("input", 0)
            total_out += usage.get("output", 0)
            log_thinking("final answer", thinking)
            final_text = text
        except ToolError as e:
            final_text = "(failed to finish review: %s)" % e
        final_text += "\n\n[note: tool call budget exhausted]"
    if final_text is None or not final_text.strip():
        final_text = "(model returned no final text)"
    return final_text, {"input_tokens": total_in,
                        "output_tokens": total_out}, thinking_log


SEV_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def worst_severity(findings):
    rank = 0
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        sev = f.get("severity")
        if sev in SEV_RANK:
            rank = max(rank, SEV_RANK[sev])
    return SEVERITIES[rank]


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="commit_review.py",
        description="Per-commit LLM analysis with read-only tool access.",
        epilog="commit specs: a single rev (3fed0e7c2, HEAD), a range "
               "(HEAD..HEAD~6), or -N for the last N commits.")
    p.add_argument("commits", nargs="+",
                   help="commit specs: rev, range (A..B), or -N")
    p.add_argument("--model", required=True,
                   help="model name, e.g. claude-sonnet-4-5, gpt-4o, "
                        "gemini-2.5-pro")
    p.add_argument("--provider",
                   choices=["auto", "anthropic", "openai", "vertex"],
                   default="auto",
                   help="LLM provider (default: auto-detect from model "
                        "name)")
    p.add_argument("--base-url",
                   help="override API base URL (anthropic/openai)")
    p.add_argument("--api-key", help="API key (anthropic/openai)")
    p.add_argument("--token",
                   help="bearer token (anthropic proxy, openai, vertex)")
    p.add_argument("--env-var",
                   help="environment variable holding the key/token")
    p.add_argument("--project", help="GCP project (vertex)")
    p.add_argument("--location", default="us-central1",
                   help="GCP location (vertex, default: us-central1)")
    p.add_argument("--max-tokens", type=int, default=8192,
                   help="max output tokens per request (default: 8192)")
    p.add_argument("--max-turns", type=int, default=30,
                   help="max tool-calling turns per commit (default: 30)")
    p.add_argument("--timeout", type=int, default=300,
                   help="HTTP timeout in seconds (default: 300)")
    p.add_argument("--diff-max-kb", type=int, default=120,
                   help="max diff bytes embedded per commit, in KB "
                        "(default: 120)")
    p.add_argument("--skill-dir", action="append", default=[],
                   help="directory of skill .md files (repeatable; "
                        "default: .skills in the repo root)")
    p.add_argument("--skill", action="append", default=[],
                   help="skill name or file to load fully (repeatable)")
    p.add_argument("--agents-file", action="append", default=[],
                   help="additional AGENTS.md-style instruction file "
                        "(repeatable)")
    p.add_argument("--no-agents", action="store_true",
                   help="do not auto-load AGENTS.md from the repo root")
    p.add_argument("--output-dir",
                   help="write <sha>.review.md and <sha>.findings.json "
                        "here")
    p.add_argument("--fail-on-severity",
                   choices=list(SEVERITIES), default="none",
                   help="exit non-zero if any finding meets this "
                        "severity (default: none)")
    p.add_argument("--dry-run", action="store_true",
                   help="show the resolved plan without calling the API")
    p.add_argument("--verbose", action="store_true",
                   help="log turns and tool calls to stderr")
    p.add_argument("--show-thinking", action="store_true",
                   help="enable model thinking and log the reasoning to "
                        "stderr (and to <sha>.thinking.md with "
                        "--output-dir)")
    p.add_argument("--thinking-budget", type=int, default=0,
                   help="thinking token budget for Anthropic extended "
                        "thinking (default: 4096 when thinking is "
                        "enabled); setting it also enables thinking")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        root = run_git(["rev-parse", "--show-toplevel"]).strip()
    except ToolError as e:
        die("not in a git repository: %s" % e)

    try:
        shas = parse_commit_specs(args.commits)
    except ToolError as e:
        die("could not resolve commit specs: %s" % e)

    skill_dirs = []
    default_skills = os.path.join(root, ".skills")
    if os.path.isdir(default_skills):
        skill_dirs.append(default_skills)
    for d in args.skill_dir:
        skill_dirs.append(os.path.abspath(
            d if os.path.isabs(d) else os.path.join(root, d)))
    skills = discover_skills(skill_dirs)
    selected_skills = resolve_selected_skills(args.skill, skills, root)
    agents_files = load_agents_files(root, args.agents_file,
                                     args.no_agents)

    try:
        provider = build_provider(args)
    except ToolError as e:
        die(str(e))

    try:
        system = build_system_prompt(root, agents_files, skills,
                                     selected_skills)

        subjects = {}
        for sha in shas:
            subjects[sha] = run_git(
                ["log", "-1", "--format=%s", sha]).strip()
    except ToolError as e:
        die("git error while preparing the review: %s" % e)

    if args.dry_run:
        print("commit_review dry run")
        print("  repo:      %s" % root)
        print("  provider:  %s" % provider.describe())
        print("  commits:   %d" % len(shas))
        for sha in shas:
            print("    - %s %s" % (sha[:10], subjects[sha]))
        print("  agents:    %s"
              % (", ".join(label for label, _ in agents_files) or "(none)"))
        print("  skills:    %s"
              % (", ".join(sorted(skills)) or "(none)"))
        if selected_skills:
            print("  selected:  %s"
                  % ", ".join(sorted(selected_skills)))
        print("  limits:    max_turns=%d max_tokens=%d diff_max=%dKB"
              % (args.max_turns, args.max_tokens, args.diff_max_kb))
        if args.output_dir:
            print("  output:    %s" % os.path.abspath(args.output_dir))
        return 0

    state = {"root": root, "verbose": args.verbose, "skills": skills,
             "diff_max": args.diff_max_kb * 1024,
             "show_thinking": args.show_thinking}

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)

    overall_rank = 0
    failures = 0
    for i, sha in enumerate(shas, 1):
        print("=" * 72)
        print("[%d/%d] commit %s  %s"
              % (i, len(shas), sha[:10], subjects[sha]))
        print("=" * 72)
        try:
            text, stats, thinking_log = review_commit(sha, provider, system,
                                                      state, args.max_turns)
        except ToolError as e:
            failures += 1
            warn("review failed for %s: %s" % (sha[:10], e))
            print("(review failed: %s)" % e)
            continue
        print()
        print(text)
        findings = parse_findings(text)
        if findings is None:
            warn("no findings JSON block found in final output")
            findings = []
        sev = worst_severity(findings)
        print()
        print("---")
        print("findings: %d  worst severity: %s  tokens: %d in / %d out"
              % (len(findings), sev, stats["input_tokens"],
                 stats["output_tokens"]))
        overall_rank = max(overall_rank, SEV_RANK[sev])
        if args.output_dir:
            review_path = os.path.join(
                args.output_dir, "%s.review.md" % sha[:10])
            findings_path = os.path.join(
                args.output_dir, "%s.findings.json" % sha[:10])
            with open(review_path, "w") as f:
                f.write("# Review of %s\n\n%s\n\n%s\n"
                        % (sha, subjects[sha], text))
            with open(findings_path, "w") as f:
                json.dump({"sha": sha, "subject": subjects[sha],
                           "findings": findings}, f, indent=2)
                f.write("\n")
            written = [review_path, findings_path]
            if args.show_thinking and thinking_log:
                thinking_path = os.path.join(
                    args.output_dir, "%s.thinking.md" % sha[:10])
                with open(thinking_path, "w") as f:
                    f.write("# Thinking for %s (%s)\n\n"
                            % (sha[:10], subjects[sha]))
                    for label, entry in thinking_log:
                        f.write("## %s\n\n%s\n\n" % (label, entry))
                written.append(thinking_path)
            if args.verbose:
                todos_path = os.path.join(
                    args.output_dir, "%s.todos.json" % sha[:10])
                with open(todos_path, "w") as f:
                    json.dump({"sha": sha, "subject": subjects[sha],
                               "todos": state.get("todos", [])},
                              f, indent=2)
                    f.write("\n")
                written.append(todos_path)
            warn("wrote %s" % ", ".join(written))

    if failures:
        return 1
    threshold = SEV_RANK[args.fail_on_severity]
    if overall_rank >= threshold and threshold > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

