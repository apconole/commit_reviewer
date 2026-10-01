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

"""Project-instruction and skill loading, and system-prompt assembly."""

import os

from .constants import SYSTEM_PROMPT
from .util import die, warn, run_git


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
