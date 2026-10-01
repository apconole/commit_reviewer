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

"""Working-tree file and directory reads."""

import os

from ..constants import MAX_FILE_LINES, MAX_LIST_ENTRIES
from ..util import ToolError, warn, safe_abs_path
from . import tool


@tool(
    "read_file",
    "Read a text file from the repository working "
    "tree, optionally a line range. Output includes "
    "line numbers.",
    {
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
)
def read_file(state, args):
    root = state["root"]
    path = safe_abs_path(root, args.get("path"))
    if not os.path.isfile(path):
        raise ToolError("not a file: %s" % args.get("path"))
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


@tool(
    "list_directory",
    "List entries of a directory in the repository "
    "working tree.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Directory path (default: "
                                    "repo root)"},
        },
    },
)
def list_directory(state, args):
    root = state["root"]
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
