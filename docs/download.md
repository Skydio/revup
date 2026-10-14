# NAME

revup download - Cherry-pick someone else's pull request as a managed commit.

# SYNOPSIS

`revup [--verbose]`
: `download [--help] [--base-branch=<base>] <PR-URL | PR-number | branch>`

# DESCRIPTION

Squash the given pull request or branch into a single commit, like
`revup cherry-pick`, and cherry-pick it to the current HEAD. This is meant for
keeping a copy of another person's changes locally, not for uploading them.

The pull request can be given as a URL, or as a number in the repository that
the remote points at. Its head and base branches are looked up on the forge.
The pull request must be in the repository that the remote points at.
A branch name is used as is, and its base branch is auto-detected unless
`--base-branch` is given.

The commit message is the first commit message of the branch with all revup
tags (such as `Topic:`) and any previous `Revup-Download:` line removed, so the
commit is never treated as one of your topics. A warning is added saying the
commit is managed by `revup download`, along with a `Revup-Download: <branch>`
line naming the source branch. If the stack already has a commit with that line
for the branch, the command fails with an error.

If there is a conflict, the git repository will contain the conflicting files
for the user to resolve.

# OPTIONS

**`<PR-URL | PR-number | branch>`**
: The GitHub pull request URL (e.g. `https://github.com/owner/repo/pull/123`),
pull request number, or branch to download.

**--help, -h**
: Show this help page.

**--base-branch, -b**
: Instead of the base branch of the pull request, or the auto-detected one for a branch,
use the given branch as the base.

# EXAMPLES

Download pull request 123 of the current repository.

: $ `revup download 123`

Download the pull request given by a URL.

: $ `revup download https://github.com/owner/repo/pull/123`
