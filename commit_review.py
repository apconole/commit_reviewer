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
import json
import os
import re
import sys

from commit_reviewer.constants import SEVERITIES
from commit_reviewer.util import ToolError, die, warn, clip, run_git
from commit_reviewer.gitcommits import parse_commit_specs, commit_context
from commit_reviewer.context import (
    discover_skills, build_system_prompt, load_agents_files,
    resolve_selected_skills)
from commit_reviewer.providers import build_provider
from commit_reviewer.tools import tool_schemas, run_tool


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

