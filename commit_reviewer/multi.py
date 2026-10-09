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

"""Multiple-backend support: spec parsing and work planning.

A backend is described by a comma-separated ``key=value`` specification that
overrides a subset of the global CLI options (provider, model, endpoint and
credentials).  The pure helpers here turn those specifications into provider
namespaces and compute which commits each backend should review; all thread
orchestration lives in the CLI.
"""

import copy

from .util import die

# Keys accepted inside a --backend spec, mapped to the argument attribute
# they override on the provider namespace.
SPEC_KEYS = {
    "provider": "provider",
    "model": "model",
    "base-url": "base_url",
    "api-key": "api_key",
    "token": "token",
    "env-var": "env_var",
    "project": "project",
    "location": "location",
    "label": "label",
}


def parse_backend_spec(spec):
    """Parse a ``key=value,key=value`` backend spec into an attr->value dict.

    Raises (via die) on an empty spec, a pair without '=', or an unknown key.
    """
    if spec is None or not spec.strip():
        die("empty --backend specification")
    out = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            die("invalid --backend entry %r: expected key=value" % pair)
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        attr = SPEC_KEYS.get(key)
        if attr is None:
            die("unknown --backend key %r (known keys: %s)"
                % (key, ", ".join(sorted(SPEC_KEYS))))
        out[attr] = value
    if not out:
        die("empty --backend specification")
    return out


def sanitize_label(text):
    """Reduce a display name to characters safe for use in filenames."""
    text = (text or "").strip()
    kept = [c if c.isalnum() or c in "-_." else "-" for c in text]
    label = "".join(kept).strip("-._")
    return label or "backend"


def backend_label(spec, index):
    """Choose a display/filename label for a parsed backend spec."""
    if spec.get("label"):
        return sanitize_label(spec["label"])
    if spec.get("model"):
        return sanitize_label(spec["model"])
    return "backend%d" % (index + 1)


def make_backend_namespace(base_args, spec):
    """Overlay a parsed backend spec onto a shallow copy of the global args.

    The copy carries over the shared options (max_tokens, timeout, thinking,
    ...) while the spec overrides provider selection and credentials.
    """
    ns = copy.copy(base_args)
    for attr, value in spec.items():
        setattr(ns, attr, value)
    return ns


def plan_backends(shas, nbackends, strategy):
    """Return one ordered sha list per backend.

    ``separate`` gives every backend the full commit list so each report can
    be cross-checked against the others; ``combine`` partitions the commits
    round-robin across the backends to minimize wall-clock time.  A single
    backend always receives the whole list regardless of strategy.
    """
    if nbackends < 1:
        return []
    if nbackends == 1 or strategy != "separate":
        buckets = [[] for _ in range(nbackends)]
        for i, sha in enumerate(shas):
            buckets[i % nbackends].append(sha)
        return buckets
    return [list(shas) for _ in range(nbackends)]
