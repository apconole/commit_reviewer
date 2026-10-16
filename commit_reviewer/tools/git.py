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

"""Read-only git history tools (git show / git log)."""

from ..constants import MAX_LOG_ENTRIES, MAX_FILE_LINES, TOOL_PAGE_LIMIT
from ..gitcommits import RE_REV
from ..util import ToolError, run_git, check_pathspec, cap_text
from . import tool


@tool(
    "git_show",
    "Show a commit with its diff (git show), or a "
    "file's contents at a given revision "
    "(rev:path). File reads are line-numbered and "
    "support start_line/end_line to page through "
    "large files.",
    {
        "type": "object",
        "properties": {
            "rev": {"type": "string",
                    "description": "Commit hash or git rev"},
            "file": {"type": "string",
                     "description": "Optional path; when given, "
                                    "shows the file at that rev "
                                    "instead of the commit"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["rev"],
    },
)
def git_show(state, args):
    rev = args.get("rev")
    if not rev or not RE_REV.match(rev):
        raise ToolError("invalid rev: %r" % (rev,))
    file_ = args.get("file")
    if file_ is not None:
        check_pathspec(file_)
        blob = run_git(["show", "%s:%s" % (rev, file_)])
        lines = blob.split("\n")
        # A trailing newline yields a final empty element; drop it so the
        # reported total matches wc -l semantics.
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        start = args.get("start_line") or 1
        end = args.get("end_line")
        if not isinstance(start, int) or isinstance(start, bool) or start < 1:
            raise ToolError("invalid start_line")
        if end is not None and (not isinstance(end, int)
                                or isinstance(end, bool) or end < start):
            raise ToolError("invalid end_line")
        if end is None:
            end = min(start + MAX_FILE_LINES - 1, total)
        else:
            end = min(end, total)
        body = []
        for i in range(start, end + 1):
            body.append("%6d  %s" % (i, lines[i - 1]))
        shown = "\n".join(body) if body else \
            "(range is out of bounds; file has %d line(s))" % total
        header = "git show %s:%s [%d line(s) total, showing %d-%d]" % (
            rev, file_, total, start, min(end, total) if total else 0)
        hint = ("re-run git_show with start_line/end_line to page, or use "
                "grep/sed_read to locate the region you need")
        return cap_text("%s\n%s" % (header, shown), TOOL_PAGE_LIMIT, hint)
    diff = run_git(["show", "--no-color", "--format=fuller", rev])
    hint = ("use git_show with file= and start_line/end_line to read "
            "specific files in full")
    return cap_text(diff, state.get("diff_max") or TOOL_PAGE_LIMIT, hint)


@tool(
    "git_log",
    "Run git log over a rev/range, optionally "
    "limited to a file, with a bounded number of "
    "entries.",
    {
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
)
def git_log(state, args):
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
