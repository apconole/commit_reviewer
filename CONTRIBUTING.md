# Contributing to commit_reviewer

`commit_reviewer` is an agentic harness purpose-built for reviewing commits.
Contributions are welcome and should be submitted via GitHub pull requests.

This document describes the expected process and the rules for acceptable
contributions.

## How to contribute

1. Fork the repository on GitHub.
2. Create a feature or fix branch.
3. Make your changes in one or more well-scoped commits.
4. Ensure each commit follows the commit message rules below.
5. Push the branch and open a pull request against the main branch.

All contributions must be submitted as GitHub pull requests. Direct pushes to
the main repository are not the expected contribution path.

## DCO sign-off

All contributions must be signed off under the Developer Certificate of
Origin.

Every commit in a pull request must include a proper `Signed-off-by:` tag in
the commit message:

```text
Signed-off-by: Your Name <your.email@example.com>
```

A common way to add this automatically is:

```bash
git commit -s
```

You can also configure Git to always add the sign-off:

```bash
git config --global format.signOff true
```

A `Signed-off-by:` line indicates that you have the right to submit the
work under the project license and that you agree to the DCO terms.

## LLM and specialized tool assistance

If a contribution is created or substantially assisted by LLMs or very
special development tools, such as Coccinelle, the commit message must
include an `Assisted-by:` tag describing what was used.

Examples:

```text
Assisted-by: LLM
```

```text
Assisted-by: LLM Coccinelle
```

```text
Assisted-by: Coccinelle
```

If multiple tools were used, include one `Assisted-by:` line per tool or
assistance category.

The `Assisted-by:` tag does not replace the required human `Signed-off-by:`
tag. Human authors remain responsible for the correctness and licensing of
their contributions.

## Multiple human authors

If more than one person authored a particular patch, every additional
co-author must have:

1. A `Co-Authored-By:` tag.
2. A `Signed-off-by:` tag.

Example:

```text
Co-Authored-By: Alice Example <alice@example.com>
Signed-off-by: Alice Example <alice@example.com>
```

The primary author should still include their own `Signed-off-by:` tag.

Example for a patch with two human authors:

```text
Signed-off-by: Bob Example <bob@example.com>
Co-Authored-By: Alice Example <alice@example.com>
Signed-off-by: Alice Example <alice@example.com>
```

Use `Co-Authored-By:` for human co-authors. Use `Assisted-by:` for LLMs,
Coccinelle, or other specialized tools.

## Commit message guidelines

### Explain the purpose, not just the diff

Every commit message should describe the purpose behind the change.

The code diff shows what changed. The commit message should explain why the
change was made.

Good commit messages answer questions such as:

- What problem is this solving?
- Why is this needed now?
- What behavior is being improved, fixed, or introduced?
- What would break or regress if this change were reverted?

Avoid commit messages that merely list the mechanical changes. For example,
this is not enough:

```text
Update script and fix indentation
```

This is much better:

```text
Validate commit metadata before invoking the reviewer

The reviewer was previously invoked even when required commit metadata was
missing. This caused noisy failure output and made it harder for users to
diagnose invalid commit messages.

Adding validation before the review step gives a clear, actionable error
message and keeps the reviewer focused on commits that are actually eligible
for review.
```

### One logical change per commit

Each commit should be a single logical change.

This is important to keep the history clean and to make the project easier to
maintain. It also helps with:

- `git revert`
- `git bisect`
- code review
- debugging regressions
- understanding the evolution of the project

Avoid combining unrelated changes into one commit. For example, do not combine:

- bug fixes and refactoring
- formatting changes and behavior changes
- documentation updates and code changes
- license header changes and functional changes
- multiple independent bug fixes

If a change requires multiple logical steps, split it into multiple commits.

### Bug fixes and the Fixes tag

If a commit fixes a bug introduced by a specific previous commit, the commit
message should include a `Fixes:` tag containing the first 12 characters of
the commit SHA that introduced the bug.

Example:

```text
Fixes: 0123abcd4567
```

If a change fixes multiple specific commits, include multiple `Fixes:` lines:

```text
Fixes: 0123abcd4567
Fixes: 89abcdef0123
```

This makes it easier to identify the source of regressions and improves the
value of `git bisect` and automated debugging workflows.

## Example commit message

```text
Reject commits with missing Signed-off-by lines before review

The reviewer should only analyze commits that are valid contributions to the
project. Previously, commits without a Signed-off-by tag were still passed to
the review stage, producing confusing errors later in the pipeline.

Failing early makes the invalid commit message easier to diagnose and keeps
the reviewer from performing unnecessary work.

Fixes: 0123abcd4567
Assisted-by: Coccinelle
Signed-off-by: Alice Example <alice@example.com>
Co-Authored-By: Bob Example <bob@example.com>
Signed-off-by: Bob Example <bob@example.com>
```

## Pull request guidelines

When opening a pull request, please include:

1. A clear title describing the overall change.
2. A description of the purpose of the pull request.
3. An explanation of any behavioral changes.
4. Any relevant testing notes.
5. A reference to the issue or discussion being addressed, if applicable.

The pull request description should also focus on the why, not just a verbose
summary of the diff.

A good pull request description may explain:

- The problem being solved.
- The approach chosen.
- Alternatives considered, if useful.
- How the change was tested.
- Any follow-up work that was intentionally left out.

## Licensing

All contributed code must be licensed under a suitable Free and Open Source
license.

Accepted licenses include:

- Apache-2.0
- BSD
- Another suitable Free and Open Source license

Do not contribute code under proprietary, non-free, or restrictive licenses.

If a file uses a license other than the main project license, the file should
clearly identify its license and its provenance where appropriate.

Newly added files should make their licensing status obvious, for example
through a license header or by being covered by the repository-level license
policy.  This should include appropriate SPDX identifier.

## Pull request checklist

Before submitting a pull request, please confirm that:

- [ ] Every commit includes appropriate `Signed-off-by:` tags.
- [ ] Any LLM or specialized tool assistance is disclosed with an
      `Assisted-by:` tag.
- [ ] All human co-authors are listed with both `Co-Authored-By:` and
      `Signed-off-by:` tags.
- [ ] Each commit explains the purpose behind the change.
- [ ] Each commit is a single logical change.
- [ ] Each commit does not regress or fail any tests.
- [ ] Bug fixes that revert a known bad commit include a `Fixes:` tag with the
      first 12 characters of the relevant commit SHA.
- [ ] All code is licensed under Apache-2.0, BSD, or another suitable Free and
      Open Source license.
