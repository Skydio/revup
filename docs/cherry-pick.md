# NAME

revup cherry-pick - Squash and cherry-pick a branch.

# SYNOPSIS

`revup [--verbose]`
: `cherry-pick [--help] [--base-branch=<base>] <branch | PR-URL>`

# DESCRIPTION

Create a squashed commit that represents all changes on the given
branch relative to the base branch, and then cherry-pick it to the
current HEAD. The given branch could have any number of commits on
top of the base branch, and could have merged in the base branch
several times.

The base branch is auto-detected by finding the closest release
branch to the given branch.

The cherry-picked commit will use the same commit message text and
author info as the first commit in the given branch. If there is a
conflict, the git repository will contain the conflicting files
for the user to resolve.

# OPTIONS

**`<branch | PR-URL>`**
: The branch to cherry-pick, or a GitHub pull request URL
(e.g. `https://github.com/owner/repo/pull/123`). When a PR URL is
given, the branch and base branch are derived from the pull request.
Must have some content difference from the base branch.

**--help, -h**
: Show this help page.

**--base-branch, -b**
: Instead of automatically detecting the base branch, use the given
branch as the base.

# EXAMPLES

Cherry-pick a feature branch that was previously on main to rc10.

: $ `revup cherry-pick feature_br --base-branch main`

Cherry-pick a branch from a GitHub pull request URL. The branch and
base branch are resolved automatically from the PR.

: $ `revup cherry-pick https://github.com/owner/repo/pull/123`
