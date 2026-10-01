# Security Policy

`commit_reviewer` is an agentic, LLM-powered harness for reviewing Git commits
and patch series. This document describes the project's threat model, the
assumptions under which it is meant to be run, and how to report a
vulnerability.

The project consists of two utilities:

- **`commit_review.py`** — a Python CLI that reviews commits (or a commit
  range) in a Git working tree using an LLM provider, with read-only tool
  access and optional machine-readable findings. It can gate CI via
  `--fail-on-severity`.
- **`review-new-series.sh`** — a Bash helper (intended for cron or a
  systemd-timer) that watches a configured Git remote for new branches, checks
  out each series in a temporary worktree, runs the reviewer, and emails the
  full result via `git send-email`.

## Supported versions

The latest `main` branch. The project does not currently cut release tags.

## Reporting a vulnerability

To report a security issue, open an issue on the upstream repository labeled
**`security`**:

- <https://github.com/apconole/commit_reviewer/issues>

Please label the issue `security`. As a courtesy, avoid attaching a working
exploit or proof-of-concept to the public issue until the problem has been
fixed; the maintainer may ask that you hold those details back. If you prefer
not to use the issue tracker, you may instead email the maintainer directly:

- Aaron Conole — <aconole@bytheb.org>

What to include: a description of the issue, the affected version (commit),
and, where possible, the steps or input that trigger it.

The maintainer will acknowledge a valid report and work on a fix on a
best-effort basis. <!-- TBD: state an explicit acknowledgment/fix window once
established, e.g. "acknowledged within 5 business days". -->

---

## Threat model

### Trust boundaries and where the tool should run

`commit_reviewer` is a **client only**. It opens no network listeners. Its
outbound traffic is limited to:

- the LLM inference provider (HTTPS),
- the watched Git remote (`git remote update` / `git fetch`), and
- SMTP via `git send-email` (the `review-new-series.sh` helper).

The tool is meant to be run **by a trusted operator, on a trusted host**,
against repositories the operator has chosen to review. The trust split is:

- **Operator-trusted (configuration):** the host, the credentials and how
  they are supplied, the `MAIL_TO` recipient(s), `--base-url`, `REVIEW_ARGS`,
  and the environment.
- **Untrusted by design (input):** every repository, branch, commit, commit
  message, diff, and file the tool is pointed at. Reviewing *other people's
  code* is the point of the tool, so **each reviewed repository is an active
  attack surface**, not a benign input.

Deployment guidance:

- Run on a trusted host and keep credentials out of the process command line
  (see "Process (`ps`) exposure" and "Credential and secret leakage" below).
  Prefer `--env-var`, environment variables, or a secret manager.
- Keep secrets out of the reviewed working tree and out of `.git/config`
  (for example, tokens embedded in remote URLs such as
  `https://user:token@host/...`). Anything in the tree or in `.git/` is
  readable by the model's tools.
- Consider running the reviewer inside a sandbox or container with a
  restricted filesystem and a minimal environment, so that an injection cannot
  exfiltrate host-level secrets even if it reflects in-repo content.
- Treat the `MAIL_TO` mailing list as a **third party** that receives model
  output. Anything the model can read in the repository can, in the worst
  case, end up in the emailed report.
- The `review-new-series.sh` helper runs as a service account. When
  `BRANCH_PATTERN` is unset it reviews *every* branch on the remote, so a
  malicious contributor can trigger a review (and an outbound email) by simply
  pushing a branch.

### Threats and mitigations

The tool is deliberately read-only, but the LLM is fed untrusted content, so
the threat model is dominated by **prompt injection** and the small number of
paths by which injected instructions could reach a real interpreter or leak
secrets.

#### 1. Prompt injection (primary threat)

Untrusted content reaches the model verbatim, with no sanitization. Vectors
include:

- commit messages (full body), author/date metadata, and diffs
  (`gitcommits.py` → `commit_context`),
- any working-tree file or Git blob reachable via the `read_file`,
  `list_directory`, `git_show`, `git_log`, `grep`, `sed_read`, and `awk_read`
  tools, and
- **`AGENTS.md` and `.skills/*.md`**, which are embedded directly into the
  **system prompt** (`context.py`). Content in these files is therefore treated
  with system-level authority and can be loaded on demand via `load_skill`. A
  malicious repository can plant instructions there.

What the model **cannot** do, by design:

- There is no command-execution tool and no network tool.
- All file access is confined to the repository root by `safe_abs_path`
  (`util.py`), so the model cannot read files outside the reviewed repository.
- The model has no access to process tables, `/proc`, or the host environment,
  so it cannot enumerate processes or read the operator's environment
  variables.

Residual risk (what an injection **can** still achieve):

- **Secret exfiltration through the output channel.** The model can read
  in-repo secrets (for example a `.env` file or `.git/config` with embedded
  tokens) and reflect them into the review text, which is written to
  `--output-dir` and, by the helper, emailed to `MAIL_TO`. `read_file` and
  `list_directory` do **not** exclude `.git/` (unlike `grep`, which skips it),
  so `.git/config`, `.git/hooks/*`, and reflogs are readable.
- **Cost / availability.** A crafted commit can exhaust the per-commit budget
  (default `--max-turns` of 30 turns, up to the configured output tokens per
  turn, plus HTTP retries) before producing a result.

Mitigations in place:

- A read-only tool set and the path sandbox (enforcement-level).
- System-prompt **SAFETY RULES** (`constants.py`), including the rule to flag
  but **not repeat** the value of any secret that appears in the code.
- Output clipping and per-commit turn/token budgets.

**Caveat:** the SAFETY RULES are prompt-level, not enforcement-level. They
reduce the likelihood of a secret being echoed but cannot guarantee it.

#### 2. Command execution via the `sed_read` / `awk_read` tools (RCE if a
validator is bypassed)

`sed_read` and `awk_read` invoke the real `sed` and `awk` interpreters on a
path inside the repository. They are guarded by validators (`tools/sedawk.py`):

- `validate_sed` — a **whitelist** of read-only sed commands, rejecting the
  `e` (execute) flag and write flags on `s///`.
- `validate_awk` — a **regex blacklist** of dangerous awk constructs
  (`system(`, `getline`, `close(`, `ENVIRON`, `PROCINFO`, command pipes,
  backticks, `print >` redirections, etc.).

The model's tool calls — which a prompt injection can steer — are the input to
these validators. A whitelist is strong, but a regex blacklist fronting a real
interpreter is inherently bypass-prone. **Any validator bypass would yield
arbitrary command execution on the host running the review.** This is the most
security-critical code in the project and a priority audit target.

#### 3. Credential and secret leakage

- **Process (`ps`) exposure.** `--api-key` / `--token` passed on the command
  line are visible in the process list (`ps`) and in shell history to other
  users on a shared host. The tool itself never prints `ps` output and gives
  the model no way to read process listings, so this exposure is the operator's
  own invocation, not something the tool leaks. Use `--env-var` / environment
  variables / a secret manager instead.
- **`--base-url` scheme is not validated.** A plain `http://` endpoint is
  accepted, which would transmit the credential in cleartext and hand the full
  key to a malicious or compromised endpoint.
- **Redirect credential leak.** Outbound requests use `urllib`, which follows
  HTTP redirects and re-sends the `Authorization` header cross-host. A
  malicious endpoint can issue a redirect to a second, attacker-controlled
  host and capture the key.
- **Error-body echo.** On HTTP errors, up to ~800 characters of the response
  body are embedded in the error message and can appear in logs or the emailed
  report.
- **Model-readable in-repo secrets.** Any secret in the working tree or in
  `.git/` (including token-bearing remote URLs in `.git/config`) is readable
  by the model's tools and can be reflected into review output, `--output-dir`
  files, and email.
- **Verbose / thinking logs.** `--verbose` and `--show-thinking` write tool
  arguments and full model reasoning to stderr; the helper captures both
  stdout and stderr into the mbox it sends, and the CLI also writes
  `<sha>.thinking.md`. Reasoning text may quote in-repo secrets.
- There is **no automatic redaction** of credentials anywhere in the pipeline.

#### 4. Information disclosure via output channels

The review text, the machine-readable findings JSON, and (when enabled)
thinking/verbose logs are written to `--output-dir` and, by the helper, placed
in an mbox and sent to `MAIL_TO` via `git send-email`. Because the content is
model-generated from untrusted input, these outputs should be treated as
potentially containing repository data or leaked secrets, and the recipients
as third parties.

#### 5. Process (`ps`) output

The tool does not invoke or display `ps`, `/proc`, or process listings, and it
exposes no tool that reads them. The only `ps`-related exposure is the
operator's own command-line arguments (`--api-key` / `--token`), covered in
section 3.

#### 6. Availability and cost

Per commit, the agent is bounded by `--max-turns` (default 30), the configured
output tokens per turn, a 20-second subprocess timeout, and 4 HTTP retries.
These bounds limit, but do not eliminate, the ability of a crafted commit to
maximize compute and API cost before a review completes.

#### 7. Minor and hardening-relevant notes

- **Worktree paths derived from remote branch names.** The helper builds
  `"$WORKTREE_ROOT/$branch.$$"` and later `rm -rf "$CURRENT_WORKTREE"`. Remote
  branch names are attacker-controllable, but are constrained by Git refname
  rules (no spaces, no `..`, no leading `-`, etc.).
- **`REVIEW_ARGS` word-splitting.** The helper reads `REVIEW_ARGS` into an
  array; it is operator-controlled and documented as must-not-contain-spaces.
- **Inconsistent `.git` exclusion.** `grep` skips `.git/`, but `read_file` and
  `list_directory` do not, so `.git/` internals are reachable through those
  tools.

### Accepted risk / known limitations

Operators should assume that **reviewed content is hostile**. The read-only
tool set and the path sandbox are enforcement-level, but the defenses against
prompt injection (system-prompt SAFETY RULES, output clipping, turn budgets)
are best-effort and do not guarantee that an injected instruction will not
reflect in-repo secrets into the review output, the `--output-dir` artifacts,
or the emailed report. The `sed`/`awk` validators are the single most
consequential control against remote command execution and are the priority
for audit and review. Running the tool in a restricted sandbox with secrets
kept out of the working tree and `.git/` is the recommended way to contain the
residual risk.
