# NAME

revup amend - Modify commits in the current stack.

# SYNOPSIS

`revup [--verbose] [--keep-temp]`
: `amend [--help] [--no-edit] [--all] <ref|topic>`

# DESCRIPTION

Add the contents of cache to the given commit or topic in the stack, and
also reword the commit text. Then re-apply all subsequent commits in the
stack.

The cache and working directory are never modified regardless
of whether the command succeeds or fails, which makes this command
faster than the corresponding `git rebase` for the same task.

When rewording commits, abort if the resulting message is empty.
If there are no changes to either the tree or the commit message,
no commit hashes will change.

Currently, conflicts between the cache and target commit or between
the new commit and later commits aren't handled. If any conflict arises,
the conflicting file paths will be printed and the program will exit
without making any changes.

In the future `revup amend` will be able to show conflict markers
and provide the user a way to resolve conflicts.

# OPTIONS

**`<ref|topic>`**
: The topic, commit or branch name to amend. Must be an ancestor of the
current HEAD. If no commit is specified, HEAD is used as the commit. If a
topic is provided that has more than one commit, the most recent commit is
used (although Git modifiers are supported, like `mytopic~3` for the third
ancestor of the most recent commit of the topic `mytopic`).

**--help, -h**
: Show this help page.

**--no-edit**
: Don't open up an editor to edit the commit message and instead
use the old commit message as-is.

**--insert, -i**
: Instead of amending the given commit, insert the changes in cache
as a new commit after the given commit. If there are no changes in
cache, this inserts an empty commit. Cannot be used with --no-edit,
since the new commit requires a commit message.

**--drop, -d**
: Instead of amending the given commit, drop it and leave any changes
it made in cache. Implies --no-edit and cannot be used with --insert.

**--all, -a**
: Tell revup to automatically stage files that have been modified and
deleted before amending the commit. Do not automatically add new files
that you have not told git about.

**--base-branch, -b**
: Instead of automatically detecting the base branch, use the given
branch as the base. See `revup upload -h` for the definition of a base
branch.

**--relative-branch, -e**
: Use the given branch as the relative branch. See `revup upload -h`
for the definition of a relative branch.

**--no-parse-topics**
: Don't attempt to parse the target as a topic.

**--no-parse-refs**
: Don't attempt to parse the target as a commit or branch name.

# EXAMPLES

Edits the third commit down in the stack.

: $ `revup amend HEAD~2`

Edits the most recent commit in the topic `mytopic`:

: $ `revup amend mytopic`
