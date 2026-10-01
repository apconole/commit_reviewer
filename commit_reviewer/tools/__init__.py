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

"""Tool registry: a single decorator registers each tool's schema and handler.

Handlers share one signature ``handler(state, args)`` where ``state`` carries
the repository root, verbosity, discovered skills, and the in-memory todo
list.  Adding a tool is now a single decorated function in a submodule plus
one import below -- no dispatch edits.
"""

from ..util import ToolError

REGISTRY = []


def tool(name, description, parameters):
    """Decorator registering a read-only tool handler and its schema."""
    def deco(func):
        REGISTRY.append({"name": name, "description": description,
                         "parameters": parameters, "func": func})
        return func
    return deco


def tool_schemas():
    return [{"name": e["name"], "description": e["description"],
             "parameters": e["parameters"]} for e in REGISTRY]


def run_tool(name, args, state):
    for entry in REGISTRY:
        if entry["name"] == name:
            return entry["func"](state, args)
    raise ToolError("unknown tool: %s" % name)


# Import submodules last so their @tool decorators can register; this import
# order fixes the order tools are advertised to the model (read_file,
# list_directory, git_show, git_log, grep, sed_read, awk_read, load_skill,
# TodoWrite).  Keep it stable.
from . import fs  # noqa: E402
from . import git  # noqa: E402
from . import search  # noqa: E402
from . import sedawk  # noqa: E402
from . import skill  # noqa: E402
from . import todo  # noqa: E402
