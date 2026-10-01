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

"""Review agent loop: tool-calling turn loop, context compaction, findings."""

import json
import re

from .constants import SEVERITIES
from .gitcommits import commit_context
from .tools import tool_schemas, run_tool
from .util import ToolError, warn, clip

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
