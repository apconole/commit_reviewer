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

"""Read-only sed/awk validators and the sed_read / awk_read tools."""

import os
import re

from ..constants import TOOL_PAGE_LIMIT
from ..util import ToolError, run_external, safe_abs_path, cap_text
from . import tool

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


@tool(
    "sed_read",
    "Read a file through a read-only sed script, "
    "as in 'sed -n -e SCRIPT FILE'. Only read-only "
    "sed commands are permitted (p, n, d, =, l, h, "
    "H, g, G, y, t, b, N, P, D, x, and s without "
    "the e flag).",
    {
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
)
def sed_read(state, args):
    root = state["root"]
    path = safe_abs_path(root, args.get("path"))
    if not os.path.isfile(path):
        raise ToolError("not a file: %s" % args.get("path"))
    expr = args.get("expression")
    if not expr or not isinstance(expr, str):
        raise ToolError("expression is required")
    if len(expr) > 4096:
        raise ToolError("expression too long (max 4096)")
    validate_sed(expr)
    hint = ("narrow the sed script (for example a tighter line range or "
            "address) to reduce output")
    return cap_text(run_external(["sed", "-n", "-e", expr, path]),
                    TOOL_PAGE_LIMIT, hint)


@tool(
    "awk_read",
    "Read a file through a read-only awk program, "
    "as in 'awk PROGRAM FILE'. The program must "
    "not use system(), getline, close(), ENVIRON, "
    "pipes, or print redirection.",
    {
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
)
def awk_read(state, args):
    root = state["root"]
    path = safe_abs_path(root, args.get("path"))
    if not os.path.isfile(path):
        raise ToolError("not a file: %s" % args.get("path"))
    program = args.get("program")
    if not program or not isinstance(program, str):
        raise ToolError("program is required")
    if len(program) > 8192:
        raise ToolError("program too long (max 8192)")
    validate_awk(program)
    hint = ("narrow the awk program (for example add a condition or line "
            "range) to reduce output")
    return cap_text(run_external(["awk", program, path]), TOOL_PAGE_LIMIT,
                    hint)
