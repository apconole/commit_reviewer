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
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOOL_OUTPUT_LIMIT = 60000
SUBPROCESS_TIMEOUT = 20
HTTP_RETRIES = 4
MAX_GREP_RESULTS = 500
MAX_LOG_ENTRIES = 100
MAX_LIST_ENTRIES = 500
MAX_FILE_LINES = 2000
MAX_FILE_BYTES = 200000

SEVERITIES = ("none", "low", "medium", "high")

OUTPUT_FORMAT_FINDINGS = """\
After writing the report as specified above, end your final message with
exactly one fenced JSON block of the form:
```json
{
  "findings": [
    {
      "type": "Memory Safety | Logic | Concurrency | API | Documentation",
      "title": "short summary",
      "description": "what is wrong",
      "reasoning": "how you verified it and why it matters",
      "severity": "high | medium | low",
      "severity_explanation": "consequence and triggering path",
      "locations": [
        {
          "file": "path",
          "function_or_symbol": "name or null",
          "line_range": "NN-NN",
          "why_this_location_matters": "..."
        }
      ],
      "preexisting": false
    }
  ]
}
```
If there are no issues, end with {"findings": []}.
This JSON block is required even when the report above already says PASS
with no concerns; it is parsed by tooling, not read by a person, so it must
be the last thing in your message and it must be valid JSON with no trailing
commentary after the closing fence.
"""

OUTPUT_FORMAT_INLINE = """\
Write all output as plain ascii text, without any special characters or
emojis.
Your report must be conversational and undramatic.
Your report must be factual, just technical observations.
Call issues as 'regressions' or 'concerns'.
Write out each concern as a reply to the hunk to which it belongs.
When writing the reply, the hunks need to be quoted inline with '> ' in front
of each line.  Preserve long lines.
If you have no concerns for a hunk, omit it.  At the bottom of the reply, you
should write 'Verdict: ' followed by one of PASS, FAIL, WARN
depending on whether there are major concerns.
A commit that has no concerns should be marked PASS.
Minor concerns should be WARN.
Major concerns should be FAIL.
"""

SYSTEM_PROMPT = f"""\
You are a meticulous, read-only code reviewer performing per-commit \
analysis in a git repository.

GUIDELINE (general):
Never: open with preamble or pleasantries; restate the question; add filler transitions; hedge with niceties; or repeat a point you've already made.
Always: keep essential steps, caveats, uncertainties, and specifics — never drop correctness or a needed warning for brevity. Keep the final answer lean. Use the least structure that conveys it (plain prose when short; lists or code only when they earn their place). If genuinely uncertain, say so and explain why — never omit uncertainty for the sake of brevity.
If something is ambiguous, ask a sharp question, don't guess.

SAFETY RULES (absolute):
1. You have READ-ONLY tools.  Never attempt to modify, create, or delete
   files, and never request actions that change repository or system state.
   If you find a bug, report it; do not attempt to fix it.  TodoWrite is
   the one exception: it only updates your in-memory task list.
2. Only inspect the repository working tree and git history via the tools
   provided.  Do not access the network or anything outside the repository.
3. If a credential or secret appears in the code, flag it as a finding but
   do not repeat the secret value in your report.
4. Report only findings you verified with tools.  If a concern cannot be
   verified, label it "unverified" instead of asserting it.
5. Stay in scope: review the commit under analysis.  Read surrounding code
   only to understand context, and do not report unrelated pre-existing
   issues unless the commit makes them worse.

REVIEW PROCEDURE (start here):
1. Read the commit message and the diff provided in the request.  State the
   changed files and the intended purpose of the change.
2. For each changed hunk, read the enclosing function in full (read_file,
   sed_read, or awk_read) and identify the invariants it relies on.
3. Use grep to locate callers and consumers of changed behavior, and
   git_log to understand how the invariants evolved when they are not
   obvious.
4. Focus on: correctness (bounds, off-by-one, overflow, signedness,
   endianness, protocol field handling), memory safety (leaks, double free,
   use-after-free), concurrency (locking, atomics, lock ordering), error
   handling, and API/ABI compatibility.
5. Distinguish problems introduced by this commit from pre-existing issues.

OUTPUT FORMAT:
{OUTPUT_FORMAT_INLINE}
{OUTPUT_FORMAT_FINDINGS}
"""


class ToolError(Exception):
    pass


def die(msg):
    sys.stderr.write("commit_review: %s\n" % msg)
    sys.exit(1)


def warn(msg):
    sys.stderr.write("commit_review: %s\n" % msg)


def clip(text, limit=TOOL_OUTPUT_LIMIT):
    text = text.replace("\r\n", "\n")
    if len(text) > limit:
        return text[:limit] + "\n[...truncated %d chars...]\n" % (
            len(text) - limit)
    return text


def run_git(args, timeout=SUBPROCESS_TIMEOUT):
    cmd = ["git", "--no-pager"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError("git %s timed out after %ds" %
                        (" ".join(args), timeout))
    except OSError as e:
        raise ToolError("failed to run git: %s" % e)
    out = p.stdout
    if p.returncode != 0:
        err = p.stderr.strip()
        raise ToolError("git %s failed (exit %d)%s" %
                        (" ".join(args[:2]), p.returncode,
                         (": " + err[:500]) if err else ""))
    return out


RE_REV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}:-]*$")
RE_NEG_COUNT = re.compile(r"^-([1-9][0-9]*)$")


def parse_commit_specs(specs):
    """Resolve commit specs to an ordered list of full SHAs (oldest first)."""
    shas = []
    seen = set()

    def add(shas_list):
        for s in shas_list:
            if s not in seen:
                seen.add(s)
                shas.append(s)

    for spec in specs:
        if ".." in spec:
            if not RE_REV.match(spec):
                die("invalid commit range: %r" % spec)
            out = run_git(["rev-list", "--reverse", spec])
            shas_list = [line.strip() for line in out.splitlines()
                         if line.strip()]
            if not shas_list:
                # An empty range is often a reversed one, e.g.
                # "HEAD..HEAD~6" meaning "the last 6 commits".  Try the
                # swapped endpoints before giving up.
                parts = spec.split("..")
                if len(parts) == 2:
                    swapped = parts[1] + ".." + parts[0]
                    out = run_git(["rev-list", "--reverse", swapped])
                    shas_list = [line.strip()
                                 for line in out.splitlines()
                                 if line.strip()]
                    if shas_list:
                        warn("range %r is empty; using reversed "
                             "range %r" % (spec, swapped))
            add(shas_list)
        else:
            m = RE_NEG_COUNT.match(spec)
            if m:
                n = int(m.group(1))
                out = run_git(["rev-list", "--reverse",
                               "HEAD~%d..HEAD" % n])
            else:
                if not RE_REV.match(spec):
                    die("invalid commit spec: %r" % spec)
                out = run_git(["rev-parse", "--verify",
                               spec + "^{commit}"])
            add([line.strip() for line in out.splitlines() if line.strip()])
    if not shas:
        die("no commits matched the given specifications")
    return shas


def commit_context(sha, max_diff_bytes):
    meta = run_git(["show", "-s",
                    "--format=commit %H%nAuthor: %an <%ae>%n"
                    "Date:   %ad%n%n%B", sha])
    stat = run_git(["show", "--stat", "--format=", sha])
    diff = run_git(["show", "--format=", "--no-color", sha])
    note = ""
    encoded = diff.encode("utf-8", "replace")
    if len(encoded) > max_diff_bytes:
        # Truncate on the byte length (max_diff_bytes is a byte count), then
        # decode back and cut again at the nearest preceding newline so we
        # don't split a multi-byte UTF-8 sequence or a line in half.
        truncated = encoded[:max_diff_bytes].decode("utf-8", "ignore")
        cut = truncated.rfind("\n")
        if cut < 0:
            cut = len(truncated)
        diff = (truncated[:cut]
                + "\n[... diff truncated; use git_show or read_file "
                  "for the full change ...]\n")
        note = " (diff truncated)"
    return ("Review commit %s\n%s\n%s%s\n%s"
            % (sha, meta, stat, note, diff))


def load_markdown_file(path, label, max_bytes=60000):
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except OSError as e:
        warn("could not read %s %s: %s" % (label, path, e))
        return None
    if len(text) > max_bytes:
        text = text[:max_bytes] + "\n[... truncated ...]"
    return text


def parse_frontmatter(text):
    """Parse a minimal YAML frontmatter block (scalar keys only)."""
    meta = {}
    if not text.startswith("---"):
        return meta, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return meta, text
    for line in lines[1:end]:
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip("\"'")
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def discover_skills(skill_dirs):
    """Return {name: {"path":..., "description":..., "text":...}}."""
    skills = {}
    for d in skill_dirs:
        if not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            path = os.path.join(d, entry)
            if not os.path.isfile(path) or not entry.endswith(".md"):
                continue
            raw = load_markdown_file(path, "skill")
            if raw is None:
                continue
            meta, body = parse_frontmatter(raw)
            name = meta.get("name") or os.path.splitext(entry)[0]
            if name not in skills:
                skills[name] = {
                    "path": path,
                    "description": meta.get("description", ""),
                    "text": body,
                }
    return skills


def build_system_prompt(root, agents_files, skills, selected_skills):
    parts = [SYSTEM_PROMPT]

    top = run_git(["rev-parse", "--show-toplevel"]).strip()
    branch = run_git(["branch", "--show-current"]).strip()
    parts.append("REPOSITORY\npath: %s\nbranch: %s"
                 % (top, branch if branch else "(detached HEAD)"))

    for label, text in agents_files:
        parts.append("PROJECT INSTRUCTIONS (%s)\nThe following project "
                     "instructions apply to this review:\n\n%s"
                     % (label, text))

    if selected_skills:
        for name, sk in selected_skills.items():
            parts.append("SKILL: %s\nLoaded skill file %s. Follow its "
                         "guidance where relevant.\n\n%s"
                         % (name, sk["path"], sk["text"]))
    elif skills:
        lines = ["AVAILABLE SKILLS",
                 "The following skill files are available.  If one looks "
                 "relevant to this review, call load_skill with its name "
                 "to fetch its full contents before relying on it:"]
        for name, sk in skills.items():
            lines.append("- %s: %s" %
                         (name, sk["description"] or sk["path"]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool implementations (all read-only, confined to the repository root;
# TodoWrite only tracks in-memory review progress)
# ---------------------------------------------------------------------------


def safe_abs_path(root, p):
    if p is None or p == "":
        return root
    if not isinstance(p, str) or "\x00" in p:
        raise ToolError("invalid path: %r" % (p,))
    full = os.path.abspath(os.path.join(root, p))
    root_real = os.path.realpath(root)
    full_real = os.path.realpath(full)
    if full_real != root_real and \
            not full_real.startswith(root_real + os.sep):
        raise ToolError("path escapes the repository: %s" % p)
    return full


def check_pathspec(file_):
    if not file_ or file_.startswith("/") or "\x00" in file_ or \
            ".." in file_.split("/"):
        raise ToolError("invalid file pathspec: %r" % file_)
    return file_


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


def run_external(cmd, timeout=SUBPROCESS_TIMEOUT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError("%s timed out after %ds" % (cmd[0], timeout))
    except OSError as e:
        raise ToolError("failed to run %s: %s" % (cmd[0], e))
    out = p.stdout
    if p.returncode != 0:
        err = p.stderr.strip()
        raise ToolError("%s failed (exit %d)%s" %
                        (cmd[0], p.returncode,
                         (": " + err[:500]) if err else ""))
    if not out.strip():
        return "(no output)"
    return out


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
# LLM provider clients
# ---------------------------------------------------------------------------


def http_post_json(url, headers, body, timeout):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


RETRIABLE_HTTP = (408, 429, 500, 502, 503, 504)


def http_post_with_retry(url, headers, body, timeout, verbose):
    last_err = None
    for attempt in range(HTTP_RETRIES):
        try:
            return http_post_json(url, headers, body, timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
            last_err = "HTTP %s: %s" % (e.code, detail or e.reason)
            if e.code not in RETRIABLE_HTTP:
                raise ToolError(last_err)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = "connection error: %s" % e
        if attempt < HTTP_RETRIES - 1:
            delay = 2 ** attempt
            if verbose:
                warn("request failed (%s); retrying in %ds"
                     % (last_err, delay))
            time.sleep(delay)
    raise ToolError("request failed after %d attempts: %s"
                    % (HTTP_RETRIES, last_err))


class Provider:
    """Common interface: chat() -> (text, tool_calls, usage_dict, thinking).

    thinking is a list of dicts with a "text" display field (plus any
    provider-specific extra fields needed to send the blocks back),
    carrying the model's reasoning when it produced any.
    """

    name = "base"

    def chat(self, system, messages, tools, verbose):
        raise NotImplementedError

    def describe(self):
        return self.name


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model, api_key, base_url, max_tokens, timeout,
                 thinking=False, thinking_budget=0):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.thinking = thinking
        self.thinking_budget = thinking_budget

    def describe(self):
        return "anthropic model=%s endpoint=%s/v1/messages" % (
            self.model, self.base_url)

    def _tools(self, schemas):
        return [{"name": s["name"],
                 "description": s["description"],
                 "input_schema": s["parameters"]} for s in schemas]

    def _messages(self, messages):
        out = []
        for m in messages:
            role = m["role"]
            if role == "assistant":
                blocks = []
                # Extended thinking blocks must be sent back unmodified,
                # with their signatures, for the conversation to stay
                # valid; they must also precede all other blocks.
                for tb in m.get("thinking", []):
                    if "signature" in tb:
                        blocks.append({"type": "thinking",
                                       "thinking": tb.get("thinking", ""),
                                       "signature": tb.get("signature", "")})
                    elif "data" in tb:
                        blocks.append({"type": "redacted_thinking",
                                       "data": tb.get("data", "")})
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": tc["id"],
                                   "name": tc["name"], "input": tc["args"]})
                if out and out[-1]["role"] == "assistant":
                    out[-1]["content"].extend(blocks)
                else:
                    out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                block = {"type": "tool_result",
                         "tool_use_id": m["tool_call_id"],
                         "content": m["content"]}
                if m.get("is_error"):
                    block["is_error"] = True
                if (out and out[-1]["role"] == "user"
                        and isinstance(out[-1]["content"], list)
                        and out[-1]["content"]
                        and out[-1]["content"][0].get("type")
                        == "tool_result"):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                if out and out[-1]["role"] == "user" and \
                        isinstance(out[-1]["content"], str):
                    out[-1]["content"] += "\n\n" + m["content"]
                else:
                    out.append({"role": "user", "content": m["content"]})
        return out

    @staticmethod
    def parse_response(data):
        text = ""
        calls = []
        thinking = []
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text += block.get("text", "")
            elif btype == "tool_use":
                calls.append({"id": block.get("id", "toolu_%d"
                                              % len(calls)),
                              "name": block.get("name", ""),
                              "args": block.get("input", {})})
            elif btype == "thinking":
                thinking.append({"text": block.get("thinking", ""),
                                 "thinking": block.get("thinking", ""),
                                 "signature": block.get("signature", "")})
            elif btype == "redacted_thinking":
                thinking.append({"text": "[redacted thinking]",
                                 "data": block.get("data", "")})
        usage = data.get("usage", {}) or {}
        return text, calls, {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        }, thinking

    def chat(self, system, messages, tools, verbose):
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": self._tools(tools),
            "messages": self._messages(messages),
        }
        headers = {"anthropic-version": "2023-06-01"}
        if self.thinking:
            body["thinking"] = {"type": "enabled",
                                "budget_tokens": self.thinking_budget}
            # Let the model reason between tool calls, not just before the
            # first one.
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        if self.api_key.startswith("sk-ant-"):
            headers["x-api-key"] = self.api_key
        else:
            # Treat as a bearer token (gateway / proxy auth).
            headers["Authorization"] = "Bearer " + self.api_key
        data = http_post_with_retry(
            self.base_url + "/v1/messages", headers, body,
            self.timeout, verbose)
        return AnthropicProvider.parse_response(data)


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model, api_key, base_url, max_tokens, timeout):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url
                         or "https://api.openai.com/v1").rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout

    def describe(self):
        return "openai-compatible model=%s endpoint=%s/chat/completions" \
            % (self.model, self.base_url)

    @staticmethod
    def _tools(schemas):
        return [{"type": "function",
                 "function": {"name": s["name"],
                              "description": s["description"],
                              "parameters": s["parameters"]}}
                for s in schemas]

    @staticmethod
    def _messages(system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]
            if role == "assistant":
                entry = {"role": "assistant"}
                entry["content"] = m.get("content") or ""
                if m.get("tool_calls"):
                    entry["tool_calls"] = [{
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments":
                                         json.dumps(tc["args"])},
                    } for tc in m["tool_calls"]]
                out.append(entry)
            elif role == "tool":
                out.append({"role": "tool",
                            "tool_call_id": m["tool_call_id"],
                            "content": m["content"]})
            else:
                if out and out[-1]["role"] == "user" and \
                        isinstance(out[-1].get("content"), str):
                    out[-1]["content"] += "\n\n" + m["content"]
                else:
                    out.append({"role": "user",
                                "content": m["content"]})
        return out

    @staticmethod
    def parse_response(data):
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                args = {"_raw_arguments": raw}
            calls.append({"id": tc.get("id") or "call_%d" % i,
                          "name": fn.get("name", ""),
                          "args": args if isinstance(args, dict)
                          else {"value": args}})
        usage = data.get("usage", {}) or {}
        thinking = []
        for key in ("reasoning", "reasoning_content"):
            val = msg.get(key)
            if isinstance(val, str) and val:
                thinking.append({"text": val})
        return text, calls, {
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
        }, thinking

    def chat(self, system, messages, tools, verbose):
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._messages(system, messages),
            "tools": self._tools(tools),
        }
        headers = {"Authorization": "Bearer " + self.api_key}
        data = http_post_with_retry(
            self.base_url + "/chat/completions", headers, body,
            self.timeout, verbose)
        return OpenAIProvider.parse_response(data)


class VertexProvider(Provider):
    name = "vertex"

    def __init__(self, model, token, project, location, max_tokens,
                 timeout, thinking=False):
        self.model = model
        self.token = token
        self.project = project
        self.location = location
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.thinking = thinking
        base = "https://%s-aiplatform.googleapis.com" % location
        if model.startswith("openai/"):
            self.url = (base + "/v1/projects/%s/locations/%s/"
                        "publishers/google/models/%s/chat/completions"
                        % (project, location, model))
            self.compat = True
        else:
            self.url = (base + "/v1/projects/%s/locations/%s/"
                        "publishers/google/models/%s:generateContent"
                        % (project, location, model))
            self.compat = False

    def describe(self):
        return ("vertex model=%s project=%s location=%s"
                % (self.model, self.project, self.location))

    def _headers(self):
        return {"Authorization": "Bearer " + self.token}

    def _tools(self, schemas):
        if self.compat:
            return OpenAIProvider._tools(schemas)
        return [{"functionDeclarations": [
            {"name": s["name"],
             "description": s["description"],
             "parameters": s["parameters"]} for s in schemas]}]

    def _contents(self, messages):
        contents = []
        for m in messages:
            role = m["role"]
            parts = []
            if role == "tool":
                resp = {"output": m["content"]}
                if m.get("is_error"):
                    resp["error"] = True
                parts.append({"functionResponse": {
                    "name": m.get("name", ""),
                    "response": resp,
                }})
                role = "user"
            elif role == "assistant":
                if m.get("content"):
                    parts.append({"text": m["content"]})
                for tc in m.get("tool_calls", []):
                    parts.append({"functionCall": {
                        "name": tc["name"], "args": tc["args"]}})
                role = "model"
            else:
                parts.append({"text": m["content"]})
                role = "user"
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        return contents

    def chat(self, system, messages, tools, verbose):
        if self.compat:
            body = {"model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": OpenAIProvider._messages(system,
                                                         messages),
                    "tools": OpenAIProvider._tools(tools)}
            data = http_post_with_retry(self.url, self._headers(), body,
                                        self.timeout, verbose)
            return OpenAIProvider.parse_response(data)
        body = {
            "contents": self._contents(messages),
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": self._tools(tools),
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        if self.thinking and re.search(r"gemini-(2\.5|3)", self.model):
            body["generationConfig"]["thinkingConfig"] = {
                "includeThoughts": True}
        data = http_post_with_retry(self.url, self._headers(), body,
                                    self.timeout, verbose)
        cands = data.get("candidates") or [{}]
        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = ""
        calls = []
        thinking = []
        for i, part in enumerate(parts):
            if part.get("thought"):
                thinking.append({"text": part.get("text", "")})
            elif "text" in part:
                text += part["text"]
            fc = part.get("functionCall")
            if fc:
                calls.append({"id": "call_%d" % i,
                              "name": fc.get("name", ""),
                              "args": fc.get("args") or {}})
        meta = data.get("usageMetadata", {}) or {}
        return text, calls, {
            "input": meta.get("promptTokenCount", 0),
            "output": meta.get("candidatesTokenCount", 0),
        }, thinking


def vertex_token(cli_token, env_var):
    if cli_token:
        return cli_token
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    for var in ("VERTEX_TOKEN", "GOOGLE_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    gcloud = shutil_which("gcloud")
    if gcloud:
        try:
            p = subprocess.run([gcloud, "auth", "print-access-token"],
                               capture_output=True, text=True, timeout=20)
            tok = p.stdout.strip()
            if p.returncode == 0 and tok:
                return tok
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        from google.auth import default as gcp_default
        from google.auth.transport import requests as ga_requests
        creds, _ = gcp_default()
        creds.refresh(ga_requests.Request())
        return creds.token
    except ImportError:
        pass
    except Exception as e:
        warn("google.auth default credentials failed: %s" % e)
    hint = ""
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        hint = (" GOOGLE_APPLICATION_CREDENTIALS is set but the "
                "'google-auth' python package is not installed.")
    raise ToolError(
        "no vertex token available.  Provide one via --token, an "
        "--env-var, VERTEX_TOKEN, or 'gcloud auth print-access-token', "
        "or install the 'google-auth' package with application default "
        "credentials.%s" % hint)


def shutil_which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


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


def detect_provider(model, project):
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini") or project:
        return "vertex"
    return "openai"


def resolve_key(args, provider):
    if args.api_key:
        return args.api_key
    if args.token:
        return args.token
    env_var = args.env_var or {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return None


def build_provider(args):
    provider_name = args.provider
    if provider_name == "auto":
        project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        provider_name = detect_provider(args.model, project)
    max_tokens = args.max_tokens
    timeout = args.timeout
    thinking = bool(args.show_thinking or args.thinking_budget)
    thinking_budget = args.thinking_budget or 4096
    if provider_name == "anthropic":
        key = resolve_key(args, "anthropic")
        if not key:
            die("no anthropic credentials.  Set ANTHROPIC_API_KEY or "
                "pass --api-key/--token/--env-var")
        if thinking and thinking_budget >= max_tokens:
            warn("thinking budget %d is not below max_tokens %d; "
                 "clamping to %d" % (thinking_budget, max_tokens,
                                     max_tokens - 1))
            thinking_budget = max(1, max_tokens - 1)
        return AnthropicProvider(args.model, key, args.base_url,
                                 max_tokens, timeout, thinking,
                                 thinking_budget)
    if provider_name == "openai":
        key = resolve_key(args, "openai")
        if not key:
            die("no openai credentials.  Set OPENAI_API_KEY or "
                "pass --api-key/--token/--env-var")
        return OpenAIProvider(args.model, key, args.base_url,
                              max_tokens, timeout)
    if provider_name == "vertex":
        project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            die("vertex requires a project.  Pass --project or set "
                "GOOGLE_CLOUD_PROJECT")
        token = vertex_token(args.token, args.env_var)
        return VertexProvider(args.model, token, project, args.location,
                              max_tokens, timeout, thinking)
    die("unknown provider: %s" % provider_name)


def load_agents_files(root, extra, no_agents):
    files = []
    default = os.path.join(root, "AGENTS.md")
    if not no_agents and os.path.isfile(default):
        text = load_markdown_file(default, "AGENTS.md")
        if text:
            files.append(("AGENTS.md", text))
    for spec in extra:
        path = os.path.abspath(spec)
        text = load_markdown_file(path, "agents file")
        if text is None:
            die("cannot read agents file: %s" % spec)
        files.append((os.path.basename(path), text))
    return files


def resolve_selected_skills(specs, skills, root):
    selected = {}
    for spec in specs:
        if spec in skills:
            selected[spec] = skills[spec]
            continue
        path = os.path.abspath(
            spec if os.path.isabs(spec) else os.path.join(root, spec))
        if os.path.isfile(path):
            raw = load_markdown_file(path, "skill")
            if raw is not None:
                meta, body = parse_frontmatter(raw)
                name = meta.get("name") or \
                    os.path.splitext(os.path.basename(path))[0]
                selected[name] = {"path": path,
                                  "description": meta.get(
                                      "description", ""),
                                  "text": body}
                continue
        die("skill not found: %s (available: %s)"
            % (spec, ", ".join(sorted(skills)) or "none"))
    return selected


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

