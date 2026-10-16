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

"""Command-line interface: argument parsing and the review driver."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor

from .agent import (review_commit, parse_findings, worst_severity, SEV_RANK)
from .constants import SEVERITIES
from .context import (discover_skills, build_system_prompt, load_agents_files,
                      resolve_selected_skills)
from .gitcommits import parse_commit_specs
from .multi import (parse_backend_spec, make_backend_namespace, backend_label,
                    plan_backends)
from .providers import build_provider
from .util import (ToolError, die, warn, run_git, create_review_worktree,
                   remove_review_worktree)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="commit_review.py",
        description="Per-commit LLM analysis with read-only tool access.",
        epilog="commit specs: a single rev (3fed0e7c2, HEAD), a range "
               "(HEAD..HEAD~6), or -N for the last N commits.")
    p.add_argument("commits", nargs="+",
                   help="commit specs: rev, range (A..B), or -N")
    p.add_argument("--model",
                   help="model name, e.g. claude-sonnet-4-5, gpt-4o, "
                        "gemini-2.5-pro; required unless every backend sets "
                        "its own model via --backend")
    p.add_argument("--provider",
                   choices=["auto", "anthropic", "openai", "vertex"],
                   default="auto",
                    help="LLM provider (default: auto-detect from model "
                         "name)")
    p.add_argument("--backend", action="append", default=[],
                   metavar="KEY=VALUE,...",
                   help="add a review backend; a comma-separated list of "
                        "key=value overrides (provider, model, base-url, "
                        "api-key, token, env-var, project, location, label). "
                        "Repeatable to run several backends together; when "
                        "omitted the top-level provider options are used.")
    p.add_argument("--backend-strategy", choices=["separate", "combine"],
                   default="separate",
                   help="how to distribute commits across backends: "
                        "'separate' reviews every commit with every backend "
                        "for cross-checking (default); 'combine' partitions "
                        "commits across backends for throughput")
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
    p.add_argument("--worktree-dir",
                   help="parent directory for the temporary detached "
                        "worktrees used to pin file tools to each reviewed "
                        "commit (default: system temp dir). Must live "
                        "outside the repository; point it at a fast/local "
                        "filesystem for large repos.")
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
    args = p.parse_args(argv)
    if not args.model and not args.backend:
        p.error("--model is required unless --backend is used")
    return args


def build_backends(args):
    """Resolve the ordered list of (label, provider) backends to review with.

    With no --backend options this is exactly a single unlabeled provider from
    the top-level options.  Each --backend spec overrides a subset of those
    options so several endpoints can run side by side.
    """
    if not args.backend:
        try:
            return [("", build_provider(args))]
        except ToolError as e:
            die(str(e))
    backends = []
    for i, spec_str in enumerate(args.backend):
        spec = parse_backend_spec(spec_str)
        ns = make_backend_namespace(args, spec)
        if not getattr(ns, "model", None):
            die("backend %d (%s): no model set (add model=... to --backend "
                "or a top-level --model)"
                % (i + 1, backend_label(spec, i)))
        try:
            provider = build_provider(ns)
        except ToolError as e:
            die("backend %d (%s): %s"
                % (i + 1, backend_label(spec, i), e))
        backends.append((backend_label(spec, i), provider))
    return backends


def review_backend(bindex, label, provider, shas, system, base_state, args):
    """Review one backend's assigned commits, in order.

    Runs inside its own thread.  Every commit gets a fresh copy of the shared
    state so the per-commit todo list never races with other backends; the
    read-only entries (root, skills, ...) are safely shared by reference.
    """
    name = label or "backend"
    results = []
    for sha in shas:
        state = dict(base_state)
        try:
            worktree = create_review_worktree(sha, args.worktree_dir)
        except ToolError as e:
            warn("could not pin %s on %s: %s" % (sha[:10], name, e))
            results.append({"bindex": bindex, "label": label, "sha": sha,
                            "error": str(e)})
            continue
        state["root"] = worktree
        try:
            text, stats, thinking_log = review_commit(
                sha, provider, system, state, args.max_turns)
        except ToolError as e:
            warn("review failed for %s on %s: %s" % (sha[:10], name, e))
            results.append({"bindex": bindex, "label": label, "sha": sha,
                            "error": str(e)})
            continue
        finally:
            remove_review_worktree(worktree)
        findings = parse_findings(text)
        if findings is None:
            warn("no findings JSON block found in final output (%s)" % name)
            findings = []
        results.append({"bindex": bindex, "label": label, "sha": sha,
                        "text": text, "findings": findings,
                        "sev": worst_severity(findings), "stats": stats,
                        "thinking_log": thinking_log,
                        "todos": state.get("todos", [])})
    return results


def _output_stem(sha, res, multi):
    if multi and res["label"]:
        return "%s.%s" % (sha[:10], res["label"])
    return sha[:10]


def write_outputs(res, subject, multi, args):
    """Persist one result's review/findings (and thinking/todos) files."""
    stem = _output_stem(res["sha"], res, multi)
    labeled = multi and res["label"]
    review_path = os.path.join(args.output_dir, "%s.review.md" % stem)
    findings_path = os.path.join(args.output_dir,
                                 "%s.findings.json" % stem)
    with open(review_path, "w") as f:
        if labeled:
            f.write("# Review of %s\n\nBackend: %s\n\n%s\n\n%s\n"
                    % (res["sha"], res["label"], subject, res["text"]))
        else:
            f.write("# Review of %s\n\n%s\n\n%s\n"
                    % (res["sha"], subject, res["text"]))
    payload = {"sha": res["sha"], "subject": subject,
               "findings": res["findings"]}
    if labeled:
        payload["backend"] = res["label"]
    with open(findings_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    written = [review_path, findings_path]
    if args.show_thinking and res["thinking_log"]:
        thinking_path = os.path.join(args.output_dir,
                                     "%s.thinking.md" % stem)
        with open(thinking_path, "w") as f:
            f.write("# Thinking for %s (%s)\n\n"
                    % (res["sha"][:10], subject))
            for tlabel, entry in res["thinking_log"]:
                f.write("## %s\n\n%s\n\n" % (tlabel, entry))
        written.append(thinking_path)
    if args.verbose:
        todos_path = os.path.join(args.output_dir, "%s.todos.json" % stem)
        todos_payload = {"sha": res["sha"], "subject": subject,
                         "todos": res["todos"]}
        if labeled:
            todos_payload["backend"] = res["label"]
        with open(todos_path, "w") as f:
            json.dump(todos_payload, f, indent=2)
            f.write("\n")
        written.append(todos_path)
    warn("wrote %s" % ", ".join(written))


def emit_results(shas, subjects, backends, results_by_sha, multi, args):
    """Print every commit's report(s) and write outputs; return aggregates."""
    overall_rank = 0
    failures = 0
    for i, sha in enumerate(shas, 1):
        print("=" * 72)
        print("[%d/%d] commit %s  %s"
              % (i, len(shas), sha[:10], subjects[sha]))
        print("=" * 72)
        for res in sorted(results_by_sha.get(sha, []),
                          key=lambda r: r["bindex"]):
            label = res["label"]
            if multi:
                print()
                print("### backend: %s (%s)"
                      % (label, backends[res["bindex"]][1].describe()))
            if res.get("error"):
                failures += 1
                print("(review failed: %s)" % res["error"])
                continue
            print()
            print(res["text"])
            findings = res["findings"]
            sev = res["sev"]
            prefix = ("backend %s | " % label) if multi else ""
            print()
            print("---")
            print("%sfindings: %d  worst severity: %s  tokens: %d in / %d out"
                  % (prefix, len(findings), sev,
                     res["stats"]["input_tokens"],
                     res["stats"]["output_tokens"]))
            overall_rank = max(overall_rank, SEV_RANK[sev])
            if args.output_dir:
                write_outputs(res, subjects[sha], multi, args)
    return overall_rank, failures


def main(argv=None):
    args = parse_args(argv)
    try:
        root = run_git(["rev-parse", "--show-toplevel"]).strip()
    except ToolError as e:
        die("not in a git repository: %s" % e)

    if args.worktree_dir:
        args.worktree_dir = os.path.abspath(args.worktree_dir)

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

    backends = build_backends(args)
    multi = len(backends) > 1

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
        if multi:
            print("  strategy:  %s" % args.backend_strategy)
            print("  backends:  %d" % len(backends))
            for label, provider in backends:
                print("    - %-16s %s" % (label, provider.describe()))
        else:
            print("  provider:  %s" % backends[0][1].describe())
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
        print("  worktrees: %s"
              % (args.worktree_dir or "(system temp dir)"))
        if args.output_dir:
            print("  output:    %s" % os.path.abspath(args.output_dir))
        return 0

    state = {"root": root, "verbose": args.verbose, "skills": skills,
             "diff_max": args.diff_max_kb * 1024,
             "show_thinking": args.show_thinking}

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
    if args.worktree_dir:
        try:
            os.makedirs(args.worktree_dir, exist_ok=True)
        except OSError as e:
            die("cannot create --worktree-dir %s: %s"
                % (args.worktree_dir, e))

    plan = plan_backends(shas, len(backends), args.backend_strategy)

    results_by_sha = {sha: [] for sha in shas}
    with ThreadPoolExecutor(max_workers=len(backends)) as pool:
        futures = [
            pool.submit(review_backend, bindex, label, provider,
                        assigned, system, state, args)
            for bindex, ((label, provider), assigned)
            in enumerate(zip(backends, plan))
        ]
        for fut in futures:
            for res in fut.result():
                results_by_sha[res["sha"]].append(res)

    overall_rank, failures = emit_results(shas, subjects, backends,
                                          results_by_sha, multi, args)

    if failures:
        return 1
    threshold = SEV_RANK[args.fail_on_severity]
    if overall_rank >= threshold and threshold > 0:
        return 2
    return 0
