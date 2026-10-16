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

"""Shared primitives: error type, logging, output clipping, subprocess and
path-safety helpers."""

import os
import shutil
import subprocess
import sys
import tempfile
import uuid

from .constants import TOOL_OUTPUT_LIMIT, TOOL_PAGE_LIMIT, SUBPROCESS_TIMEOUT


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


def cap_text(text, limit=TOOL_PAGE_LIMIT, hint=""):
    """Cap tool output to ``limit`` characters, cutting on a line boundary so
    a partial line is never sent.  Unlike clip() (the hard last-resort net),
    this appends an actionable note telling the model how to narrow the query
    (offsets, ranges, grep) instead of silently dropping the tail."""
    text = text.replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    note = "\n[... output capped: %d of %d chars shown ...]" % (
        cut, len(text))
    if hint:
        note += "\n" + hint
    return text[:cut] + note



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


def shutil_which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


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


def create_review_worktree(sha, base_dir=None):
    """Create a temporary detached worktree pinned to ``sha``.

    The working-tree file tools read from the repository root, so pointing
    them at a checkout of the exact commit under review keeps file contents
    and line numbers aligned with the diff being reviewed.  Returns the
    absolute path of the new worktree; callers must pass it to
    remove_review_worktree when done.

    ``base_dir`` selects where the worktree is created (default: the system
    temp dir).  It should live outside the repository so the checkout does
    not pollute grep/listing.  The base directory itself is never removed --
    only the unique per-commit subdirectory we create under it.
    """
    base = os.path.abspath(base_dir) if base_dir else tempfile.gettempdir()
    path = os.path.join(
        base, "commit-review-%s-%s" % (sha[:8], uuid.uuid4().hex))
    try:
        run_git(["worktree", "add", "--detach", path, sha])
    except ToolError as e:
        raise ToolError("could not create review worktree for %s: %s"
                        % (sha[:10], e))
    return path


def remove_review_worktree(path):
    """Remove a worktree created by create_review_worktree and prune stale
    administrative entries.  Best-effort and idempotent: never raises, and
    leaves the parent base directory in place."""
    if not path:
        return
    try:
        run_git(["worktree", "remove", "--force", path])
    except ToolError:
        shutil.rmtree(path, ignore_errors=True)
    finally:
        try:
            run_git(["worktree", "prune"])
        except ToolError:
            pass
