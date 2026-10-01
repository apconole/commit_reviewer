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

"""Content search over the working tree."""

import fnmatch
import os
import re

from ..constants import MAX_GREP_RESULTS
from ..util import ToolError, safe_abs_path
from . import tool


@tool(
    "grep",
    "Search file contents in the repository with a "
    "regex (extended regex, like grep -E). Skips "
    "binary files and .git. Results are "
    "path:line: text.",
    {
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
)
def grep(state, args):
    root = state["root"]
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
