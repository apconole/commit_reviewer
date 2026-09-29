# Patch / Commit Review

This project provides a small workflow for AI-assisted review of Git patches
and commit series.

It consists of two main utilities:

- **`commit_review.py`** : a Python script that reviews commits in a Git
  working tree using one of several LLM inference providers. It supports model
  selection, provider configuration, optional skills/instructions, read-only
  tool access, and machine-readable finding output.
- **`review-new-series.sh`** : a Bash helper that watches for new branches on
  a configured Git remote and starts a single `commit_review.py` run for all
  commits in the new series. Which branches are watched is controlled by the
  `BRANCH_PATTERN` environment variable; when it is unset, every branch on the
  remote is considered.

The goal is to make it easy to review individual commits, commit ranges, or
entire patch series without manually invoking the model for every commit.

---

## Overview

### `commit_review.py`

`commit_review.py` performs per-commit LLM analysis with read-only tool access.

It accepts Git commit specifications such as:

- a single revision: `HEAD`, `3fed0e7c2`
- a range: `HEAD..HEAD~6`, `origin/master..topic`
- a count: `-5` for the last 5 commits

It can be configured to use Anthropic, OpenAI-compatible, or Vertex-based
inference providers, and can optionally load repository-specific review
instructions from `AGENTS.md` and skill files.

### `review-new-series.sh`

`review-new-series.sh` is designed for automated series review.

It checks a configured Git remote for newly seen branches matching the
`BRANCH_PATTERN` glob (for example `series_<numbers>`). When `BRANCH_PATTERN`
is unset, every branch on the remote is considered.

When a new branch is found, it runs a single instance of the review
script against all commits introduced by that branch. The number of commits to
review is measured relative to a configured base ref, usually the main
integration branch such as `origin/master`.

A state file is used to remember which series branches have already been
processed.

---

## Requirements

Typical requirements are:

- `git`
- `python3`
- `bash`
- access to an LLM inference provider, such as:
  - Anthropic
  - OpenAI or an OpenAI-compatible endpoint
  - Google Vertex AI

You also need appropriate API credentials. Credentials should be supplied
through environment variables or CLI options; they should not be committed to
the repository.

---

## Quick Start

### Review the last 5 commits

```bash
commit_review.py --model claude-sonnet-4-5 -5
```

### Review a single commit

```bash
commit_review.py --model gpt-4o HEAD
```

### Review a range

```bash
commit_review.py --model gemini-2.5-pro origin/master..HEAD
```

### Dry run

Use `--dry-run` to see the resolved review plan without calling the API:

```bash
commit_review.py --model claude-sonnet-4-5 --dry-run -5
```

---

## Using `commit_review.py`

### Basic usage

```text
usage: commit_review.py [-h] --model MODEL [--provider {auto,anthropic,openai,vertex}]
                        [--base-url BASE_URL] [--api-key API_KEY] [--token TOKEN]
                        [--env-var ENV_VAR] [--project PROJECT] [--location LOCATION]
                        [--max-tokens MAX_TOKENS] [--max-turns MAX_TURNS]
                        [--timeout TIMEOUT] [--diff-max-kb DIFF_MAX_KB]
                        [--skill-dir SKILL_DIR] [--skill SKILL]
                        [--agents-file AGENTS_FILE] [--no-agents]
                        [--output-dir OUTPUT_DIR]
                        [--fail-on-severity {none,low,medium,high}]
                        [--dry-run] [--verbose] [--show-thinking]
                        commits [commits ...]
```

### Commit specifications

The positional `commits` argument accepts one or more commit specs.

Examples:

```bash
commit_review.py --model claude-sonnet-4-5 HEAD
commit_review.py --model claude-sonnet-4-5 3fed0e7c2
commit_review.py --model claude-sonnet-4-5 HEAD..HEAD~6
commit_review.py --model claude-sonnet-4-5 -5
```

---

## Provider Configuration

The provider can be selected explicitly, or auto-detected from the model name.

```bash
commit_review.py --provider anthropic --model claude-sonnet-4-5 -5
commit_review.py --provider openai --model gpt-4o -5
commit_review.py --provider vertex --model gemini-2.5-pro -5
```

### Using environment variables for credentials

You can store credentials in environment variables and reference them
with `--env-var`.

Example:

```bash
export ANTHROPIC_API_KEY="..."
commit_review.py \
  --provider anthropic \
  --model claude-sonnet-4-5 \
  --env-var ANTHROPIC_API_KEY \
  -5
```

Or, for an OpenAI-compatible endpoint:

```bash
export OPENAI_PROXY_TOKEN="..."
commit_review.py \
  --provider openai \
  --base-url https://example.com/v1 \
  --token "$OPENAI_PROXY_TOKEN" \
  --model gpt-4o \
  -5
```

For Vertex:

```bash
commit_review.py \
  --provider vertex \
  --project my-gcp-project \
  --location us-central1 \
  --model gemini-2.5-pro \
  --token "$VERTEX_TOKEN" \
  -5
```

---

## Important Options

### Model and provider

| Option | Description |
| --- | --- |
| `--model MODEL` | Model name, e.g. `claude-sonnet-4-5`, `gpt-4o`, `gemini-2.5-pro`. |
| `--provider {auto,anthropic,openai,vertex}` | LLM provider. Default is `auto`, which attempts to infer the provider from the model name. |
| `--base-url BASE_URL` | Override the API base URL for Anthropic or OpenAI-compatible providers. |
| `--api-key API_KEY` | API key for Anthropic or OpenAI. |
| `--token TOKEN` | Bearer token for proxies, OpenAI-compatible services, or Vertex. |
| `--env-var ENV_VAR` | Name of an environment variable containing the API key or token. |
| `--project PROJECT` | GCP project for Vertex. |
| `--location LOCATION` | GCP location for Vertex. Default is `us-central1`. |

### Review limits

| Option | Description |
| --- | --- |
| `--max-tokens MAX_TOKENS` | Maximum output tokens per request. Default: `8192`. |
| `--max-turns MAX_TURNS` | Maximum number of tool-calling turns per commit. Default: `30`. |
| `--timeout TIMEOUT` | HTTP timeout in seconds. Default: `300`. |
| `--diff-max-kb DIFF_MAX_KB` | Maximum diff size embedded per commit, in KB. Default: `120`. |

### Skills and instructions

| Option | Description |
| --- | --- |
| `--skill-dir SKILL_DIR` | Directory containing skill `.md` files. Repeatable. Defaults to `.skills` in the repository root. |
| `--skill SKILL` | Load a specific skill by name or file. Repeatable. |
| `--agents-file AGENTS_FILE` | Load an additional `AGENTS.md`-style instruction file. Repeatable. |
| `--no-agents` | Do not auto-load `AGENTS.md` from the repository root. |

By default, the review tool may load repository-level review guidance from
`AGENTS.md` and skill files from `.skills`.

### Output and gating

| Option | Description |
| --- | --- |
| `--output-dir OUTPUT_DIR` | Write review artifacts to this directory. Produces `<sha>.review.md` and `<sha>.findings.json`. |
| `--fail-on-severity {none,low,medium,high}` | Exit non-zero if any finding meets or exceeds the given severity. Default: `none`. |

Examples:

```bash
commit_review.py \
  --model claude-sonnet-4-5 \
  --output-dir /tmp/reviews \
  --fail-on-severity medium \
  -5
```

For CI:

```bash
commit_review.py \
  --provider anthropic \
  --model claude-sonnet-4-5 \
  --output-dir ./reviews \
  --fail-on-severity high \
  origin/master..HEAD
```

### Debugging

| Option | Description |
| --- | --- |
| `--dry-run` | Show the resolved plan without calling the API. |
| `--verbose` | Log turns and tool calls to stderr. |
| `--show-thinking` | Log thinking replies to stderr, if available. |

Example:

```bash
commit_review.py \
  --model claude-sonnet-4-5 \
  --verbose \
  --dry-run \
  -3
```

---

## Full `commit_review.py` Help

<details>
<summary>Show full help output</summary>

```text
usage: commit_review.py [-h] --model MODEL [--provider {auto,anthropic,openai,vertex}]
                        [--base-url BASE_URL] [--api-key API_KEY] [--token TOKEN]
                        [--env-var ENV_VAR] [--project PROJECT] [--location LOCATION]
                        [--max-tokens MAX_TOKENS] [--max-turns MAX_TURNS]
                        [--timeout TIMEOUT] [--diff-max-kb DIFF_MAX_KB]
                        [--skill-dir SKILL_DIR] [--skill SKILL]
                        [--agents-file AGENTS_FILE] [--no-agents]
                        [--output-dir OUTPUT_DIR]
                        [--fail-on-severity {none,low,medium,high}]
                        [--dry-run] [--verbose] [--show-thinking]
                        commits [commits ...]

Per-commit LLM analysis with read-only tool access.

positional arguments:
  commits               commit specs: rev, range (A..B), or -N

options:
  -h, --help            show this help message and exit
  --model MODEL         model name, e.g. claude-sonnet-4-5, gpt-4o, gemini-2.5-pro
  --provider {auto,anthropic,openai,vertex}
                        LLM provider (default: auto-detect from model name)
  --base-url BASE_URL   override API base URL (anthropic/openai)
  --api-key API_KEY     API key (anthropic/openai)
  --token TOKEN         bearer token (anthropic proxy, openai, vertex)
  --env-var ENV_VAR     environment variable holding the key/token
  --project PROJECT     GCP project (vertex)
  --location LOCATION   GCP location (vertex, default: us-central1)
  --max-tokens MAX_TOKENS
                        max output tokens per request (default: 8192)
  --max-turns MAX_TURNS
                        max tool-calling turns per commit (default: 30)
  --timeout TIMEOUT     HTTP timeout in seconds (default: 300)
  --diff-max-kb DIFF_MAX_KB
                        max diff bytes embedded per commit, in KB (default: 120)
  --skill-dir SKILL_DIR
                        directory of skill .md files (repeatable; default: .skills in the repo root)
  --skill SKILL         skill name or file to load fully (repeatable)
  --agents-file AGENTS_FILE
                        additional AGENTS.md-style instruction file (repeatable)
  --no-agents           do not auto-load AGENTS.md from the repo root
  --output-dir OUTPUT_DIR
                        write <sha>.review.md and <sha>.findings.json here
  --fail-on-severity {none,low,medium,high}
                        exit non-zero if any finding meets this severity (default: none)
  --dry-run             show the resolved plan without calling the API
  --verbose             log turns and tool calls to stderr
  --show-thinking       log all thinking replies to stderr

commit specs: a single rev (3fed0e7c2, HEAD), a range (HEAD..HEAD~6), or -N for the last N commits.
```

</details>

---

## Using `review-new-series.sh`

`review-new-series.sh` is meant to run periodically, for example from cron or a
systemd timer.

It reviews newly-created branches appearing on the configured remote. By
default (with `BRANCH_PATTERN` unset) every branch on the remote is
considered; set `BRANCH_PATTERN` to restrict which branch names are watched,
for example `BRANCH_PATTERN=series_[0-9]*`.

### Example configuration

Configuration is supplied through environment variables:

```bash
# Path to the Git repository to inspect.
REPO=/home/me/git/for-review

# Remote to watch for new branches.
REMOTE=origin

# Glob matched against remote branch names. When unset (the default),
# every branch on the remote is considered. For example, to only review
# series branches:
# BRANCH_PATTERN=series_[0-9]*

# Base ref against which the series is measured.
BASE_REF=origin/master

# Review script to invoke.
REVIEW_SCRIPT=/home/me/git/commit_review.py

# Extra arguments passed to the review script.
REVIEW_ARGS=--provider openai --model my-model --max-turns 20

# Optional mail notification addresses.
MAIL_TO=me@example.com
MAIL_FROM=me@example.com

# State file used to remember which series branches have already been processed.
STATE_FILE=/home/me/.cache/review-new-series/seen
```

### Important variable: `BASE_REF`

The most important setting is `BASE_REF`.

It should be the branch or ref against which the review count is measured. For
example, if a `series_123` branch is based on `origin/master`, then:

```bash
BASE_REF=origin/master
```

This allows `review-new-series.sh` to determine which commits are new in the
series and to run a single review pass over the entire series.

### Configuring review arguments: `REVIEW_ARGS`

`REVIEW_ARGS` is passed verbatim to the review script before the commit count
is appended. For example, with a 3-commit series and:

```bash
REVIEW_ARGS=--provider openai --model my-model --max-turns 20
```

the review script is invoked as:

```bash
commit_review.py --provider openai --model my-model --max-turns 20 -3
```

Notes:

- The positional commit count (`-N`) is always appended automatically, so
  do not include a commit spec in `REVIEW_ARGS`.
- `REVIEW_ARGS` is empty by default; the review script is then run with no
  options beyond the commit count.
- Arguments are split on whitespace, so argument values must not contain
  spaces.

### Example usage

Assuming the environment variables above are set:

```bash
review-new-series.sh
```

### Example cron entry

A simple cron entry might look like this:

```cron
*/5 * * * * \
  REPO=/home/me/git/for-review \
  REMOTE=origin \
  BRANCH_PATTERN=series_[0-9]* \
  BASE_REF=origin/master \
  REVIEW_SCRIPT=/home/me/git/commit_review.py \
  REVIEW_ARGS=--provider openai --model my-model --max-turns 20 \
  MAIL_TO=me@example.com \
  MAIL_FROM=me@example.com \
  STATE_FILE=/home/me/.cache/review-new-series/seen \
  /home/me/git/review-new-series.sh \
  >> /home/me/.cache/review-new-series.log 2>&1
```

You can also put these values in an environment file and source it before
invoking the script.

Example `review-new-series.env`:

```bash
REPO=/home/me/git/for-review
REMOTE=origin
BRANCH_PATTERN=series_[0-9]*
BASE_REF=origin/master
REVIEW_SCRIPT=/home/me/git/commit_review.py
REVIEW_ARGS=--provider openai --model my-model --max-turns 20
MAIL_TO=me@example.com
MAIL_FROM=me@example.com
STATE_FILE=/home/me/.cache/review-new-series/seen
```

Then:

```bash
. /home/me/git/review-new-series.env
review-new-series.sh
```

---

## Typical Workflow

1. A developer creates a series branch, for example:

   ```bash
   series_123
   ```

2. The branch is pushed to the configured remote:

   ```bash
   git push origin series_123
   ```

3. `review-new-series.sh` detects that `series_123` is new.

4. It records the branch in the state file so it is not reviewed again.

5. It invokes one instance of `commit_review.py` for all commits in the series
   relative to `BASE_REF`.

6. Review output may be written to disk, logged, mailed, or used by an
   external notification system.

---

## Example Output

If `--output-dir` is used, `commit_review.py` writes files such as:

```text
/tmp/reviews/3fed0e7c2.review.md
/tmp/reviews/3fed0e7c2.findings.json
```

The Markdown file contains the human-readable review.

The JSON file contains structured findings, which can be useful for:

- CI gating
- dashboards
- notification systems
- triage tooling
- storing historical review results

---

## CI / Gating Example

To fail a pipeline when the review finds medium-or-higher severity issues:

```bash
commit_review.py \
  --provider anthropic \
  --model claude-sonnet-4-5 \
  --output-dir ./artifacts/review \
  --fail-on-severity medium \
  origin/master..HEAD
```

To only gate on high severity:

```bash
commit_review.py \
  --provider anthropic \
  --model claude-sonnet-4-5 \
  --output-dir ./artifacts/review \
  --fail-on-severity high \
  origin/master..HEAD
```

---

## Security Notes

- Do not commit API keys or tokens.
- Prefer environment variables or secret managers for credentials.
- Use `--env-var` when possible to avoid putting secrets directly on the
  command line.
- The review tool is intended to have read-only tool access, but you should
  still treat model output as untrusted input.
- If you use `--show-thinking` or `--verbose`, be aware that logs may contain
  repository contents, prompts, or model reasoning.

---

## Repository Layout

A typical layout is:

```text
.
├── commit_review.py
├── review-new-series.sh
├── AGENTS.md
├── .skills/
│   ├── coding-style.md
│   ├── testing.md
│   └── security.md
└── README.md
```

`AGENTS.md` and `.skills/` are optional, but they can help the model apply
repository-specific review rules.

---

## Troubleshooting

### Provider auto-detection fails

If the provider is not obvious from the model name, set it explicitly:

```bash
commit_review.py --provider anthropic --model my-model-name -1
commit_review.py --provider openai --model my-model-name -1
commit_review.py --provider vertex --model my-model-name -1
```

### Authentication errors

Check that:

- the provider is correct
- the API key or token is present
- `--env-var` points to the correct variable
- `--base-url` is correct if using a proxy

Example:

```bash
commit_review.py \
  --provider openai \
  --base-url https://llm-proxy.example.com/v1 \
  --env-var LLM_PROXY_TOKEN \
  --model gpt-4o \
  -1
```

### A series is not being reviewed

Check:

- `REPO` points to the correct repository
- `REMOTE` is the correct remote name
- the branch name matches `BRANCH_PATTERN` (every branch when unset)
- `BASE_REF` exists and is up to date
- the state file has not already recorded the branch

You can inspect the state file:

```bash
cat /home/me/.cache/review-new-series/seen
```

If you want to force a branch to be reviewed again, remove its entry from the
state file.

### Review is taking too long

You can reduce work by:

- lowering `--max-turns`
- lowering `--diff-max-kb`
- reviewing fewer commits at once
- using a faster model

Example:

```bash
commit_review.py \
  --model claude-sonnet-4-5 \
  --max-turns 10 \
  --diff-max-kb 64 \
  -3
```

---

## Summary

This project gives you two levels of operation:

1. **Manual or scripted commit review** with `commit_review.py`.
2. **Automated series discovery and review** with `review-new-series.sh`.

Together, they make it straightforward to review individual commits, commit
ranges, or newly pushed branches on a configured remote using a configured
LLM provider and repository-specific review instructions.

# Licensing

Licensed under the open source Apache 2.0 terms.
