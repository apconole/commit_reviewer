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

"""Commit specification parsing and per-commit review context."""

import re

from .util import die, warn, run_git

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
    return ("Review commit %s\n"
            "File tools (read_file, grep, sed_read, awk_read, "
            "list_directory) are pinned to a checkout of this exact commit, "
            "so their contents and line numbers match the diff below.\n%s\n"
            "%s%s\n%s"
            % (sha, meta, stat, note, diff))
