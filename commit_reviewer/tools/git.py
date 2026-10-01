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

from ..constants import MAX_LOG_ENTRIES
from ..gitcommits import RE_REV
from ..util import ToolError, run_git, check_pathspec
from . import tool


@tool(
    "git_show",
    "Show a commit with its diff (git show), or a "
    "file's contents at a given revision "
    "(rev:path).",
    {
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
)
def git_show(state, args):
    rev = args.get("rev")
    if not rev or not RE_REV.match(rev):
        raise ToolError("invalid rev: %r" % (rev,))
    file_ = args.get("file")
    if file_ is not None:
        check_pathspec(file_)
        out = run_git(["show", "%s:%s" % (rev, file_)])
        return "git show %s:%s\n%s" % (rev, file_, out)
    return run_git(["show", "--no-color", "--format=fuller", rev])


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
