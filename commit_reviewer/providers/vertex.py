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

"""Google Vertex AI provider (Gemini generateContent or OpenAI-compatible)."""

import os
import re
import subprocess

from ..util import ToolError, die, warn, shutil_which
from .base import Provider, register, http_post_with_retry
from .openai import OpenAIProvider


def vertex_token(cli_token, env_var):
    if cli_token:
        return cli_token
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    for var in ("VERTEX_TOKEN", "GOOGLE_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    gcloud = shutil_which("gcloud")
    if gcloud:
        try:
            p = subprocess.run([gcloud, "auth", "print-access-token"],
                               capture_output=True, text=True, timeout=20)
            tok = p.stdout.strip()
            if p.returncode == 0 and tok:
                return tok
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        from google.auth import default as gcp_default
        from google.auth.transport import requests as ga_requests
        creds, _ = gcp_default()
        creds.refresh(ga_requests.Request())
        return creds.token
    except ImportError:
        pass
    except Exception as e:
        warn("google.auth default credentials failed: %s" % e)
    hint = ""
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        hint = (" GOOGLE_APPLICATION_CREDENTIALS is set but the "
                "'google-auth' python package is not installed.")
    raise ToolError(
        "no vertex token available.  Provide one via --token, an "
        "--env-var, VERTEX_TOKEN, or 'gcloud auth print-access-token', "
        "or install the 'google-auth' package with application default "
        "credentials.%s" % hint)


@register
class VertexProvider(Provider):
    name = "vertex"

    def __init__(self, model, token, project, location, max_tokens,
                 timeout, thinking=False):
        self.model = model
        self.token = token
        self.project = project
        self.location = location
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.thinking = thinking
        base = "https://%s-aiplatform.googleapis.com" % location
        if model.startswith("openai/"):
            self.url = (base + "/v1/projects/%s/locations/%s/"
                        "publishers/google/models/%s/chat/completions"
                        % (project, location, model))
            self.compat = True
        else:
            self.url = (base + "/v1/projects/%s/locations/%s/"
                        "publishers/google/models/%s:generateContent"
                        % (project, location, model))
            self.compat = False

    @classmethod
    def build(cls, args):
        project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            die("vertex requires a project.  Pass --project or set "
                "GOOGLE_CLOUD_PROJECT")
        token = vertex_token(args.token, args.env_var)
        thinking = bool(args.show_thinking or args.thinking_budget)
        return cls(args.model, token, project, args.location,
                   args.max_tokens, args.timeout, thinking)

    def describe(self):
        return ("vertex model=%s project=%s location=%s"
                % (self.model, self.project, self.location))

    def _headers(self):
        return {"Authorization": "Bearer " + self.token}

    def _tools(self, schemas):
        if self.compat:
            return OpenAIProvider._tools(schemas)
        return [{"functionDeclarations": [
            {"name": s["name"],
             "description": s["description"],
             "parameters": s["parameters"]} for s in schemas]}]

    def _contents(self, messages):
        contents = []
        for m in messages:
            role = m["role"]
            parts = []
            if role == "tool":
                resp = {"output": m["content"]}
                if m.get("is_error"):
                    resp["error"] = True
                parts.append({"functionResponse": {
                    "name": m.get("name", ""),
                    "response": resp,
                }})
                role = "user"
            elif role == "assistant":
                if m.get("content"):
                    parts.append({"text": m["content"]})
                for tc in m.get("tool_calls", []):
                    parts.append({"functionCall": {
                        "name": tc["name"], "args": tc["args"]}})
                role = "model"
            else:
                parts.append({"text": m["content"]})
                role = "user"
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        return contents

    def chat(self, system, messages, tools, verbose):
        if self.compat:
            body = {"model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": OpenAIProvider._messages(system,
                                                         messages),
                    "tools": OpenAIProvider._tools(tools)}
            data = http_post_with_retry(self.url, self._headers(), body,
                                        self.timeout, verbose)
            return OpenAIProvider.parse_response(data)
        body = {
            "contents": self._contents(messages),
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": self._tools(tools),
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        if self.thinking and re.search(r"gemini-(2\.5|3)", self.model):
            body["generationConfig"]["thinkingConfig"] = {
                "includeThoughts": True}
        data = http_post_with_retry(self.url, self._headers(), body,
                                    self.timeout, verbose)
        cands = data.get("candidates") or [{}]
        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = ""
        calls = []
        thinking = []
        for i, part in enumerate(parts):
            if part.get("thought"):
                thinking.append({"text": part.get("text", "")})
            elif "text" in part:
                text += part["text"]
            fc = part.get("functionCall")
            if fc:
                calls.append({"id": "call_%d" % i,
                              "name": fc.get("name", ""),
                              "args": fc.get("args") or {}})
        meta = data.get("usageMetadata", {}) or {}
        return text, calls, {
            "input": meta.get("promptTokenCount", 0),
            "output": meta.get("candidatesTokenCount", 0),
        }, thinking
