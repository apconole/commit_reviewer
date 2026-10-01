#!/usr/bin/env python3
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

"""Per-commit LLM analysis for git repositories.

Reviews one commit, a list of commits, or a range of commits using an LLM
with read-only tool access (file reading, directory listing, git show, git
log, grep, sed-based reads, awk-based reads).  TodoWrite additionally
maintains an in-memory task list; it never touches the repository.

Providers:
  anthropic  Anthropic Messages API (x-api-key or bearer token auth)
  openai     Any OpenAI-compatible /chat/completions endpoint
  vertex     Google Vertex AI (Gemini generateContent, or OpenAI-compatible
             models published on Vertex), using token/ADC authentication

Auth sources, in order of precedence:
  --token, --api-key, --env-var NAME, the provider default environment
  variable, and for vertex: gcloud auth print-access-token or google.auth
  application default credentials.

Context:
  AGENTS.md at the repository root is loaded automatically (disable with
  --no-agents); extra files may be added with --agents-file.  Skill files
  (markdown, optional YAML frontmatter) are discovered from --skill-dir
  (default: .skills) and can be loaded into context with --skill NAME or
  --skill PATH; the model can also fetch any discovered skill on demand via
  the load_skill tool.

Commit specifications:
  3fed0e7c2      a single commit (any rev git understands)
  HEAD..HEAD~6   a range (rev-list semantics)
  -4             shorthand for the last 4 commits (HEAD~4..HEAD)

The review process starts from a safe, read-only instruction set built into
the system prompt; all tools are read-only and confined to the repository,
with the single exception of TodoWrite, which only tracks the reviewer's
own progress in memory.

This is a thin launcher: the implementation lives in the commit_reviewer
package.  Keeping this file at the repository root preserves the historical
"run commit_review.py by path" invocation used by review-new-series.sh.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from commit_reviewer.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
