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

"""OpenAI-compatible /chat/completions provider."""

import json

from ..util import die
from .base import Provider, register, http_post_with_retry, resolve_key


@register
class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model, api_key, base_url, max_tokens, timeout):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url
                         or "https://api.openai.com/v1").rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout

    @classmethod
    def build(cls, args):
        key = resolve_key(args, "openai")
        if not key:
            die("no openai credentials.  Set OPENAI_API_KEY or "
                "pass --api-key/--token/--env-var")
        return cls(args.model, key, args.base_url, args.max_tokens,
                   args.timeout)

    def describe(self):
        return "openai-compatible model=%s endpoint=%s/chat/completions" \
            % (self.model, self.base_url)

    @staticmethod
    def _tools(schemas):
        return [{"type": "function",
                 "function": {"name": s["name"],
                              "description": s["description"],
                              "parameters": s["parameters"]}}
                for s in schemas]

    @staticmethod
    def _messages(system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]
            if role == "assistant":
                entry = {"role": "assistant"}
                entry["content"] = m.get("content") or ""
                if m.get("tool_calls"):
                    entry["tool_calls"] = [{
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments":
                                         json.dumps(tc["args"])},
                    } for tc in m["tool_calls"]]
                out.append(entry)
            elif role == "tool":
                out.append({"role": "tool",
                            "tool_call_id": m["tool_call_id"],
                            "content": m["content"]})
            else:
                if out and out[-1]["role"] == "user" and \
                        isinstance(out[-1].get("content"), str):
                    out[-1]["content"] += "\n\n" + m["content"]
                else:
                    out.append({"role": "user",
                                "content": m["content"]})
        return out

    @staticmethod
    def parse_response(data):
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                args = {"_raw_arguments": raw}
            calls.append({"id": tc.get("id") or "call_%d" % i,
                          "name": fn.get("name", ""),
                          "args": args if isinstance(args, dict)
                          else {"value": args}})
        usage = data.get("usage", {}) or {}
        thinking = []
        for key in ("reasoning", "reasoning_content"):
            val = msg.get(key)
            if isinstance(val, str) and val:
                thinking.append({"text": val})
        return text, calls, {
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
        }, thinking

    def chat(self, system, messages, tools, verbose):
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._messages(system, messages),
            "tools": self._tools(tools),
        }
        headers = {"Authorization": "Bearer " + self.api_key}
        data = http_post_with_retry(
            self.base_url + "/chat/completions", headers, body,
            self.timeout, verbose)
        return OpenAIProvider.parse_response(data)
