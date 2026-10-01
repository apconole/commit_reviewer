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

"""In-memory review todo list (the one non-read-only tool)."""

from ..util import ToolError, warn
from . import tool

TODO_STATUSES = ("pending", "in_progress", "completed")
MAX_TODO_ITEMS = 100


@tool(
    "TodoWrite",
    "Create or replace the todo list used to track "
    "progress through the review of the commit "
    "under analysis. Pass the complete list on "
    "every call and update statuses as you make "
    "progress. Only updates in-memory session "
    "state; never touches the repository.",
    {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string",
                                    "description": "What needs "
                                                   "to be done"},
                        "status": {"type": "string",
                                   "enum": list(TODO_STATUSES)},
                        "activeForm": {"type": "string",
                                       "description":
                                       "Present-tense label "
                                       "shown while the item "
                                       "is in progress"},
                    },
                    "required": ["content", "status",
                                 "activeForm"],
                },
            },
        },
        "required": ["todos"],
    },
)
def todo_write(state, args):
    """Replace the in-memory todo list for the commit under review.

    The list is session state only; nothing is written to the repository.
    """
    todos = args.get("todos")
    if not isinstance(todos, list):
        raise ToolError("todos must be an array")
    if len(todos) > MAX_TODO_ITEMS:
        raise ToolError("too many todos (max %d)" % MAX_TODO_ITEMS)
    out = []
    for i, item in enumerate(todos):
        if not isinstance(item, dict):
            raise ToolError("todo %d is not an object" % i)
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ToolError("todo %d has no content" % i)
        status = item.get("status") or "pending"
        if status not in TODO_STATUSES:
            raise ToolError("todo %d has invalid status: %r"
                            % (i, status))
        active = item.get("activeForm")
        if not isinstance(active, str) or not active.strip():
            active = content
        out.append({"content": content, "status": status,
                    "activeForm": active})
    state["todos"] = out
    done = sum(1 for t in out if t["status"] == "completed")
    if state.get("verbose"):
        marks = {"completed": "x", "in_progress": ">", "pending": " "}
        warn("todos: %d item(s), %d completed" % (len(out), done))
        for t in out:
            suffix = " (%s)" % t["activeForm"] \
                if t["status"] == "in_progress" else ""
            warn("  [%s] %s%s" % (marks[t["status"]], t["content"], suffix))
    return "Todo list updated: %d item(s), %d completed. Continue the " \
           "review and keep the list current." % (len(out), done)
