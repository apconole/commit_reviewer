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

"""Provider base type, HTTP transport, shared key resolution, registry."""

import json
import os
import time
import urllib.error
import urllib.request

from ..constants import HTTP_RETRIES
from ..util import ToolError, warn


def http_post_json(url, headers, body, timeout):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


RETRIABLE_HTTP = (408, 429, 500, 502, 503, 504)


def http_post_with_retry(url, headers, body, timeout, verbose):
    last_err = None
    for attempt in range(HTTP_RETRIES):
        try:
            return http_post_json(url, headers, body, timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
            last_err = "HTTP %s: %s" % (e.code, detail or e.reason)
            if e.code not in RETRIABLE_HTTP:
                raise ToolError(last_err)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = "connection error: %s" % e
        if attempt < HTTP_RETRIES - 1:
            delay = 2 ** attempt
            if verbose:
                warn("request failed (%s); retrying in %ds"
                     % (last_err, delay))
            time.sleep(delay)
    raise ToolError("request failed after %d attempts: %s"
                    % (HTTP_RETRIES, last_err))


class Provider:
    """Common interface: chat() -> (text, tool_calls, usage_dict, thinking).

    thinking is a list of dicts with a "text" display field (plus any
    provider-specific extra fields needed to send the blocks back),
    carrying the model's reasoning when it produced any.
    """

    name = "base"

    def chat(self, system, messages, tools, verbose):
        raise NotImplementedError

    def describe(self):
        return self.name


REGISTRY = {}


def register(cls):
    """Class decorator: add a Provider subclass to the name registry."""
    REGISTRY[cls.name] = cls
    return cls


def resolve_key(args, provider):
    if args.api_key:
        return args.api_key
    if args.token:
        return args.token
    env_var = args.env_var or {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return None
