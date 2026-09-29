#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Review newly-created branches appearing on the configured remote.
#
# Configuration can be supplied through environment variables:
#
#   REPO=/home/me/git/for-review
#   REMOTE=origin
#   BASE_REF=origin/master
#   REVIEW_SCRIPT=/home/me/git/review-series.py
#   REVIEW_ARGS=--provider openai --model foo --max-turns 20
#   MAIL_TO=me@example.com
#   MAIL_FROM=me@example.com
#   STATE_FILE=/home/me/.cache/review-new-series/seen
#   BRANCH_PATTERN=series_[0-9]*
#
# BRANCH_PATTERN is a glob matched against remote branch names. When it is
# empty (the default), every branch on the remote is considered.
#
# The important one is BASE_REF: it should be the branch against which
# commit_review.py's "-X" count is supposed to be measured.

set -euo pipefail

###############################################################################
# Configuration
###############################################################################

REPO="${REPO:-$HOME/git/project}"
REMOTE="${REMOTE:-robot}"
BASE_REF="${BASE_REF:-${REMOTE}/main}"
REVIEW_SCRIPT="${REVIEW_SCRIPT:-$HOME/git/commit_review/commit_review.py}"

# Extra arguments passed to REVIEW_SCRIPT, e.g.
#     REVIEW_ARGS="--provider openai --model foo --max-turns 20"
#
# The commit count (a positional "-N" argument) is appended automatically,
# so do not include a positional commit spec here.  Arguments are split on
# whitespace, so values must not contain spaces.
REVIEW_ARGS="${REVIEW_ARGS:-}"

# Set these explicitly in your environment/crontab if your git-send-email
# configuration doesn't already provide them.
MAIL_TO="${MAIL_TO:-list@project.org}"
MAIL_FROM="${MAIL_FROM:-robot@project.org}"

STATE_FILE="${STATE_FILE:-$HOME/.cache/review-new-series/seen}"

# Put temporary worktrees outside the repository.
WORKTREE_ROOT="${WORKTREE_ROOT:-${TMPDIR:-/tmp}/review-new-series}"

# Glob matched against remote branch names (the part of the ref after
# refs/remotes/${REMOTE}/). An empty pattern matches every branch on the
# remote.
BRANCH_PATTERN="${BRANCH_PATTERN:-}"

# Use an explicit PATH because cron's PATH is often much smaller than the
# PATH you get from an interactive shell.
PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"
export PATH

###############################################################################
# Helpers
###############################################################################

log()
{
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

die()
{
    log "ERROR: $*"
    exit 1
}

cleanup()
{
    # Nothing here is allowed to make the script fail while handling an
    # already-existing failure.
    if [[ -n "${CURRENT_WORKTREE:-}" && -d "${CURRENT_WORKTREE:-}" ]]; then
        "$GIT" -C "$REPO" worktree remove --force "$CURRENT_WORKTREE" \
            >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT INT TERM

###############################################################################
# Locate git and establish the repository.
###############################################################################

GIT="$(command -v git)" || die "git not found in PATH"

cd "$REPO" || die "cannot cd to $REPO"

"$GIT" rev-parse --git-dir >/dev/null 2>&1 \
    || die "$REPO is not a git repository"

###############################################################################
# Basic configuration checks.
###############################################################################

[[ -x "$REVIEW_SCRIPT" ]] \
    || die "review script is not executable: $REVIEW_SCRIPT"

if [[ -z "$REVIEW_ARGS" ]]; then
    log "WARNING: REVIEW_ARGS is empty; $REVIEW_SCRIPT will be run " \
        "with no options beyond the commit count"
fi

"$GIT" remote get-url "$REMOTE" >/dev/null 2>&1 \
    || die "git remote does not exist: $REMOTE"

"$GIT" rev-parse --verify "$BASE_REF^{commit}" >/dev/null 2>&1 \
    || die "base ref does not exist: $BASE_REF"

if [[ -z "$MAIL_TO" ]]; then
    MAIL_TO="$("$GIT" config --get sendemail.to || true)"
fi

if [[ -z "$MAIL_FROM" ]]; then
    MAIL_FROM="$("$GIT" config --get sendemail.from || true)"
fi

[[ -n "$MAIL_TO" ]] \
    || die "MAIL_TO is not set and sendemail.to is not configured"

[[ -n "$MAIL_FROM" ]] \
    || die "MAIL_FROM is not set and sendemail.from is not configured"

###############################################################################
# Serialize cron invocations.
#
# flock is preferable to a PID file because it handles crashes without
# leaving a stale lock behind.
###############################################################################

LOCK_FILE="${STATE_FILE}.lock"

mkdir -p "$(dirname "$STATE_FILE")" "$WORKTREE_ROOT"

exec 9>"$LOCK_FILE"

if command -v flock >/dev/null 2>&1; then
    flock -n 9 || {
        log "another review-new-series invocation is already running"
        exit 0
    }
fi

###############################################################################
# Fetch.
#
# "git remote update" updates remote-tracking refs. It does not create local
# branches for the fetched branches, which is why we inspect refs/remotes
# below.
###############################################################################

log "updating remote $REMOTE"

"$GIT" remote update "$REMOTE"

###############################################################################
# Find remote-tracking branches matching BRANCH_PATTERN.
###############################################################################

if [[ -n "$BRANCH_PATTERN" ]]; then
    REF_PATTERN="refs/remotes/${REMOTE}/${BRANCH_PATTERN}"
    log "looking for branches matching $BRANCH_PATTERN"
else
    REF_PATTERN="refs/remotes/${REMOTE}/"
    log "looking for all branches on $REMOTE"
fi

mapfile -t REFS < <(
    "$GIT" for-each-ref \
        --format='%(refname)' \
        "$REF_PATTERN"
)

# refs/remotes/${REMOTE}/HEAD is a symbolic ref to the remote's default
# branch, not a branch itself. Never treat it as a review target.
SERIES_REFS=()
for ref in "${REFS[@]}"; do
    if [[ "$ref" != "refs/remotes/${REMOTE}/HEAD" ]]; then
        SERIES_REFS+=("${ref#refs/remotes/}")
    fi
done

if (( ${#SERIES_REFS[@]} == 0 )); then
    log "no branches found"
    exit 0
fi

###############################################################################
# Initialize state.
#
# The first run seeds the state file rather than treating every already-
# existing series branch as new. If you want existing branches reviewed,
# remove the state file before running the script.
#
# State format:
#
#     remote/branch<TAB>object-id
#
###############################################################################

if [[ ! -f "$STATE_FILE" ]]; then
    tmp_state="${STATE_FILE}.tmp.$$"

    : > "$tmp_state"

    for ref in "${SERIES_REFS[@]}"; do
        oid="$("$GIT" rev-parse "$ref^{commit}")"
        printf '%s\t%s\n' "$ref" "$oid" >> "$tmp_state"
    done

    mv "$tmp_state" "$STATE_FILE"

    log "initialized state with ${#SERIES_REFS[@]} existing branch(es)"
    log "no reviews sent on this first run"
    exit 0
fi

###############################################################################
# Process newly-created branches.
###############################################################################

read -r -a REVIEW_ARGS_ARRAY <<< "$REVIEW_ARGS"

for ref in "${SERIES_REFS[@]}"; do
    oid="$("$GIT" rev-parse "$ref^{commit}")"

    if grep -Fq "$(printf '%s\t' "$ref")" "$STATE_FILE"; then
        # The branch was seen before. We intentionally ignore subsequent
        # updates because the request is to review newly-created branches,
        # not every new version pushed to an existing branch.
        continue
    fi

    branch="${ref#${REMOTE}/}"

    log "new branch: $branch ($oid)"

    # Count commits reachable from the series tip but not from BASE_REF.
    #
    # git rev-list A..B means "commits reachable from B but not A".
    # --count returns just the number. See git-rev-list documentation.
    count="$("$GIT" rev-list --count "${BASE_REF}..${ref}")"

    if [[ ! "$count" =~ ^[0-9]+$ ]]; then
        die "unexpected commit count for $ref: $count"
    fi

    if (( count == 0 )); then
        log "WARNING: $ref has zero commits relative to $BASE_REF"
    fi

    CURRENT_WORKTREE="$WORKTREE_ROOT/$branch.$$"

    rm -rf "$CURRENT_WORKTREE"

    log "creating temporary worktree for $branch"

    "$GIT" worktree add \
        --detach \
        "$CURRENT_WORKTREE" \
        "$ref" \
        >/dev/null

    review_output="$(mktemp "${TMPDIR:-/tmp}/review-output.XXXXXX")"
    mail_file="$(mktemp "${TMPDIR:-/tmp}/review-mail.XXXXXX")"

    # Always remove these temporary files, including when review-series.py
    # exits nonzero.
    rm_review_output()
    {
        rm -f "$review_output" "$mail_file"
    }
    trap rm_review_output RETURN

    log "running $REVIEW_SCRIPT -$count for $branch"

    set +e
    (
        cd "$CURRENT_WORKTREE" || exit 1
        "$REVIEW_SCRIPT" ${REVIEW_ARGS_ARRAY[@]+"${REVIEW_ARGS_ARRAY[@]}"} "-$count"
    ) >"$review_output" 2>&1
    review_status=$?
    set -e

    if (( review_status == 0 )); then
        result="SUCCESS"
    else
        result="FAILED (exit status $review_status)"
    fi

    ###########################################################################
    # Construct an mbox/RFC-2822 message.
    #
    # git send-email accepts mbox-format messages as input.
    ###########################################################################

    {
        printf 'From: %s\n' "$MAIL_FROM"
        printf 'To: %s\n' "$MAIL_TO"
        printf 'Subject: review-series: %s (%s, %s commits)\n' \
            "$branch" "$result" "$count"
        printf 'Date: %s\n' "$(LC_ALL=C date -R)"
        printf '\n'

        printf 'review-series result\n'
        printf '====================\n\n'
        printf 'Branch:       %s\n' "$branch"
        printf 'Remote ref:   %s\n' "$ref"
        printf 'Commit:       %s\n' "$oid"
        printf 'Base:         %s\n' "$BASE_REF"
        printf 'Commit count: %s\n' "$count"
        printf 'Result:       %s\n' "$result"
        printf '\n'
        printf '%s\n' '-------------------- review-series.py output --------------------'
        cat "$review_output"
        printf '\n'
    } >"$mail_file"

    log "sending review result for $branch"

    "$GIT" send-email \
        --to="$MAIL_TO" \
        --from="$MAIL_FROM" \
        --confirm=never \
        --no-validate \
        --no-annotate \
        "$mail_file"

    ###########################################################################
    # Only record the branch after the email was successfully handed to
    # git-send-email. If sending fails, the branch remains "new" and the next
    # cron invocation will retry it.
    ###########################################################################

    printf '%s\t%s\n' "$ref" "$oid" >> "$STATE_FILE"

    log "completed $branch"

    "$GIT" worktree remove --force "$CURRENT_WORKTREE" >/dev/null
    CURRENT_WORKTREE=""

    rm_review_output
    trap - RETURN
done
