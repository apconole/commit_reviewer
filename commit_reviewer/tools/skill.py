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

"""On-demand loading of discovered skill files."""

from ..util import ToolError
from . import tool


@tool(
    "load_skill",
    "Load the full contents of a skill file by "
    "name from the list of available skills.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
        },
        "required": ["name"],
    },
)
def load_skill(state, args):
    skills = state["skills"]
    name = args.get("name")
    if not name or not isinstance(name, str):
        raise ToolError("name is required")
    if name not in skills:
        return "unknown skill %r.  Available skills: %s" % (
            name, ", ".join(sorted(skills)) or "(none)")
    sk = skills[name]
    return "skill %s (%s):\n%s" % (name, sk["path"], sk["text"])
