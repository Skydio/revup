# NAME

revup upload - Modify or create code reviews.

# SYNOPSIS

`revup [--verbose] [--keep-temp]`
: `upload [--help] [--base-branch=<br>] [--relative-branch=<br>]`
`[--rebase] [--relative-chain] [--skip-confirm] [--dry-run] [--push-only]`
`[--status] [--no-cherry-pick] [--no-update-pr-body] [--review-graph]`
`[--trim-tags] [--create-local-branches] [--patchsets] [--auto-add-users=<o>]`
`[--labels=<labels>] [--gt-track] [<topics>]`

# DESCRIPTION

Automatically detect a base branch and parse all commits on top
of the base branch for "Topic" tags. All commits within a topic
are then cherry-picked to the base branch, pushed to a remote
branch, and a pull request created for that review.

Within a commit, tags must take the format of a name followed
by a comma separated list of values.

    <Tagname>: <value1>, <value2>, <value3>

`<Tagname>` is case insensitive and accepts both singular and plural
forms of the name.

Valid tags and their functions are:

**Topic:**
: Specifies the topic for the commit, which determines which review
the commit will be in. Each commit can only have one topic. The tags
for a topic are the union of all tags and values of all commits in the
topic. Within a review, commits will appear in the same order as they
appear in the topic. However, commits in a topic do not have to be
consecutive locally.

**Relative:**
: Optionally specifies a relative topic for this topic. Each topic can
have at most one relative topic. Commits in this topic will be cherry-picked
on top of the branch for the relative topic, and the pull request will
be created targeted to the relative topic's branch.

**Branches:**
: Optionally specifies base branches for this topic. Base branches are long
living development branches that do not change or get deleted. Several base
branches can be specified, in which case reviews are made for this topic on top
of all given branches. If this topic has a relative topic, it must contain
only branches that are also contained in the relative topic. If no branch
is specified, the auto detected base branch will be used.

**Reviewers:**
: Specifies reviewers that will be added on github. Names as given here can be
any prefix of that user's github login name. If multiple users match a name,
the user with the shortest login name will be used. If a reviewer cannot
be found a warning is printed.

**Assignees:**
: Specifies assignees that will be added on github. Semantics are the same as
for reviewers.

**Labels:**
: Specifies labels that will be added on github. Labels must match the label
name in github exactly. If a label cannot be found a warning is printed. The
label "draft" is special and instead of showing up in labels, will cause the
PR to either be marked or unmarked as a draft.

**Uploader:**
: Optionally specifies a custom uploader name that will be used instead of the
local git username for naming generated branches. When this tag is specified,
any user can check out this branch and upload to the same branch name, allowing
multiple users to collaborate on the same PR. If uploader is specified, all
relative topics must specify the same uploader. However a topic without a
specified uploader can still be relative to a topic with one.

**Branch-Format:**
: Specifies how the remote branches get named, which mainly affects how names
conflict. Default is "user+branch", which never conflicts, but does not allow
retargeting a PR to a different base branch. "user" will allow retargeting, but
will not allow multiple base branches. "branch" and "none" are also supported.
This tag takes precedence over the config option.

**Relative-Branch:**
: Optionally specifies a relative branch that this review is targeted against.
A relative branch is one that represents another user's work or PR, and is
eventually intended to merge into the base branch. Github will delete these
branches once they are merged. If a relative branch is specified, only one
"Branch:" can be specified and all relative topics must specify the same
relative branch (or none, in which case it will get set automatically).

**Update-Pr-Body:**
: When set to true (default) revup will attempt to update the github PR body
whenever the local version has changes. When set to false, revup will create
PRs with the commit text to start out, but does not attempt to update it again.
This allows users to use the github UI to edit the PR body without having
revup overwrite those changes.

For each review, the pull request title is the title of the first commit
message in that topic, and the body is the body text of the first commit
message in that topic. A user can also create an empty commit if they
want dedicated pull-request text.

If any conflict arises while cherry-picking commits to a base branch,
the conflicting paths are printed and the program exits without making
any changes. In this case a user should specify relative topics such
that conflicts will not happen.

# OPTIONS

**`<topics>`**
: Optionally specify any number of topic names to upload. If none are
specified, all topics are uploaded. If topics are specified they will
be uploaded regardless of author.

**--help, -h**
: Show this help page.

**--base-branch, -b**
: Instead of automatically detecting the base branch, use the given
branch as the base. See above section for definition of a base branch.

**--relative-branch, -e**
: Use the given branch as the relative branch. See above section for
definition of a relative branch.

**--uploader**
: Used as the username for naming remote branches. If not set value is taken
from the portion of "git config user.email" before the "@".

**--branch-format**
: Specify how branches are named. See the Branch-Format: tag section for
options and their meaning.

**--rebase, -r**
: By default revup will not push changes if local commits are a pure
rebase of the remote changes. This flag overrides that behavior and causes
all changes to be pushed.

**--relative-chain, -c**
: Ignore all relative topic tags and instead act as though each topic is
relative to the topic before it. This can save effort typing out relative
tags when you know all reviews in the stack will be dependent.

**--auto-topic, -a**
: For commits that don't have a topic, treat the topic as a combination of
the first few words of the commit message. This allows faster uploading
and PR creation, but changes to the commit title may change the PR branch.

**--skip-confirm, -s**
: Don't require the user to confirm before uploading to github. Also skips
printing topic info before the upload.

**--pre-upload**
: A shell command that will be run before uploading any reviews.
Upload will fail with error status if this command fails. Can be
customized to ensure lint checks pass before uploading.

**--dry-run, -d**
: Performs all steps of a normal upload except those that actually involve
github. This means that changes are still cherry-picked if necessary, and
the command can still fail if there are conflicts. This will also skip the
confirmation step and only print topic info.

**--push-only, -p**
: Like --dry-run except this also pushes branches to the git remote, but
does not issue any github queries or attempt rebase detection.

**--status, -t**
: Print out status info of pull requests for existing topics but don't attempt
to push or update them.

**--update-pr-body**
: Boolean that specifies how the pr body text is updated. See the
Update-Pr-Body: tag section for details.

**--review-graph**
: Enable the review graph feature, which adds a comment containing links and
titles of all PRs in the relative chain. This comment is updated if the graph
ever changes.

**--trim-tags**
: Trim all lines containing revup related tags from all commit messages before
pushing the branch. This also affects the default PR body text.

**--create-local-branches**
: Also create local branches for each review with the same name as the
corresponding remote branch. This provides targets for debugging or testing
that particular review.

**--patchsets**
: Enable the patchsets feature, which adds a table comment containing info about each push
to the branch. On every push, a new row is added that links the head and base commits,
the date, and a link to the diff and summary of changes from previous push. The diff
link could involve a "virtual" diff target that ignores upstream changes in case
of a rebase + push.

**--self-authored-only**
: By default revup will only uploaded topics that contain at least one commit authored
by the current user (same git config user.email). Turn this off to upload topic by any
author.

**--labels**
: Specifies an additional comma separated list of labels that will be added on
github. These labels supplement the "Labels:" list in the commit description. See
the above section for details.

**--user-aliases**
: Specifies a comma separated list of colon separated username mappings. These
mappings are used to transform usernames specified in Reviewers/Assignees.

**--auto-add-users**
: If "no", do nothing extra. If "r2a", add users from the Reviewers tag as assignees.
If "a2r", add users from the Assignees tag as reviewers. If "both", do both of the previous.

**--head**
: The name or commit of the branch to be uploaded. If not specified, defaults to HEAD.

**--gt-track**
: Run `gt track` (Graphite CLI) on all branches in the stack after pushing. Branches
are tracked in topological order (parents before children) so Graphite can understand
the full stack structure. This allows you to use revup's multi-branch workflow while
also having Graphite track and display your stacks. Local branches are created as
needed for `gt track` to work.
