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

"""Anthropic Messages API provider."""

from ..util import die, warn
from .base import Provider, register, http_post_with_retry, resolve_key


@register
class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model, api_key, base_url, max_tokens, timeout,
                 thinking=False, thinking_budget=0):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.thinking = thinking
        self.thinking_budget = thinking_budget

    @classmethod
    def build(cls, args):
        key = resolve_key(args, "anthropic")
        if not key:
            die("no anthropic credentials.  Set ANTHROPIC_API_KEY or "
                "pass --api-key/--token/--env-var")
        max_tokens = args.max_tokens
        thinking = bool(args.show_thinking or args.thinking_budget)
        thinking_budget = args.thinking_budget or 4096
        if thinking and thinking_budget >= args.max_tokens:
            warn("thinking budget %d is not below max_tokens %d; "
                 "clamping to %d" % (thinking_budget, max_tokens,
                                     max_tokens - 1))
            thinking_budget = max(1, max_tokens - 1)
        return cls(args.model, key, args.base_url, max_tokens, args.timeout,
                   thinking, thinking_budget)

    def describe(self):
        return "anthropic model=%s endpoint=%s/v1/messages" % (
            self.model, self.base_url)

    def _tools(self, schemas):
        return [{"name": s["name"],
                 "description": s["description"],
                 "input_schema": s["parameters"]} for s in schemas]

    def _messages(self, messages):
        out = []
        for m in messages:
            role = m["role"]
            if role == "assistant":
                blocks = []
                # Extended thinking blocks must be sent back unmodified,
                # with their signatures, for the conversation to stay
                # valid; they must also precede all other blocks.
                for tb in m.get("thinking", []):
                    if "signature" in tb:
                        blocks.append({"type": "thinking",
                                       "thinking": tb.get("thinking", ""),
                                       "signature": tb.get("signature", "")})
                    elif "data" in tb:
                        blocks.append({"type": "redacted_thinking",
                                       "data": tb.get("data", "")})
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": tc["id"],
                                   "name": tc["name"], "input": tc["args"]})
                if out and out[-1]["role"] == "assistant":
                    out[-1]["content"].extend(blocks)
                else:
                    out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                block = {"type": "tool_result",
                         "tool_use_id": m["tool_call_id"],
                         "content": m["content"]}
                if m.get("is_error"):
                    block["is_error"] = True
                if (out and out[-1]["role"] == "user"
                        and isinstance(out[-1]["content"], list)
                        and out[-1]["content"]
                        and out[-1]["content"][0].get("type")
                        == "tool_result"):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                if out and out[-1]["role"] == "user" and \
                        isinstance(out[-1]["content"], str):
                    out[-1]["content"] += "\n\n" + m["content"]
                else:
                    out.append({"role": "user", "content": m["content"]})
        return out

    @staticmethod
    def parse_response(data):
        text = ""
        calls = []
        thinking = []
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text += block.get("text", "")
            elif btype == "tool_use":
                calls.append({"id": block.get("id", "toolu_%d"
                                              % len(calls)),
                              "name": block.get("name", ""),
                              "args": block.get("input", {})})
            elif btype == "thinking":
                thinking.append({"text": block.get("thinking", ""),
                                 "thinking": block.get("thinking", ""),
                                 "signature": block.get("signature", "")})
            elif btype == "redacted_thinking":
                thinking.append({"text": "[redacted thinking]",
                                 "data": block.get("data", "")})
        usage = data.get("usage", {}) or {}
        return text, calls, {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        }, thinking

    def chat(self, system, messages, tools, verbose):
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": self._tools(tools),
            "messages": self._messages(messages),
        }
        headers = {"anthropic-version": "2023-06-01"}
        if self.thinking:
            body["thinking"] = {"type": "enabled",
                                "budget_tokens": self.thinking_budget}
            # Let the model reason between tool calls, not just before the
            # first one.
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        if self.api_key.startswith("sk-ant-"):
            headers["x-api-key"] = self.api_key
        else:
            # Treat as a bearer token (gateway / proxy auth).
            headers["Authorization"] = "Bearer " + self.api_key
        data = http_post_with_retry(
            self.base_url + "/v1/messages", headers, body,
            self.timeout, verbose)
        return AnthropicProvider.parse_response(data)
