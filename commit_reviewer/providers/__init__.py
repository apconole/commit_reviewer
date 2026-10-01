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

"""Provider registry and construction.

Importing this package registers every provider; adding a new backend means
dropping a module that subclasses Provider and decorating it with @register,
then importing it here -- no dispatch edits elsewhere.
"""

import os

from ..util import die
from .base import REGISTRY, Provider, resolve_key  # noqa: F401
from .anthropic import AnthropicProvider  # noqa: F401
from .openai import OpenAIProvider  # noqa: F401
from .vertex import VertexProvider, vertex_token  # noqa: F401


def detect_provider(model, project):
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini") or project:
        return "vertex"
    return "openai"


def build_provider(args):
    provider_name = args.provider
    if provider_name == "auto":
        project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        provider_name = detect_provider(args.model, project)
    cls = REGISTRY.get(provider_name)
    if cls is None:
        die("unknown provider: %s" % provider_name)
    return cls.build(args)
