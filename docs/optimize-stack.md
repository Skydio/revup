# NAME

revup optimize-stack - Automatically arrange topics into relative chains

# SYNOPSIS

`revup [--verbose] [--keep-temp]`
: `optimize-stack [--help] [--base-branch=<base>] [--relative-branch=<br>]`
`[--auto-topic] [--ignore-existing] [--write | --force-write]`

# DESCRIPTION

Arrange local topics into trees of relative changes with the lowest total
depth. This is achieved by greedily attempting to cherry-pick changes until
all cherry-picks succeed. For complex stacks, this can be slow.

Typical usage is to run without arguments to see the relationship diffs, then
run with --write to insert **Optimized-Relative:** tags into commit messages.
These tags are managed by optimize-stack, take precedence over hand-written
**Relative:**, and can be automatically modified. Existing **Relative:** tags
are interpreted as semantic dependencies where the order must be maintained
(although other topics can be inserted in the middle). Topics are processed
in the order **revup upload** applies them, each after its relative with all
of its commits consecutively, so a relative may appear later in the stack.
A cycle of **Relative:** tags is an error.

Topics whose **Branch:** tag does not include the base branch are skipped.
A topic with a **Relative-Branch:** tag starts a tree on that branch
instead, which later topics may chain onto.

Topics that cannot be placed anywhere are reported as errors. This usually
means a topic depends on a commit that has no topic, or that commits of two
topics are interleaved in a way that depends on each other.

# OPTIONS

**--help, -h**
: Show this help page.

**--base-branch, -b**
: Instead of automatically detecting the base branch, use the given
branch as the base.

**--relative-branch, -e**
: Use the given branch as the relative branch. See `revup upload -h`
for the definition of a relative branch.

**--auto-topic, -a**
: Include commits without a **Topic:** tag, naming each topic from the
commit title the way `revup upload --auto-topic` does. With --write or
--force-write the **Topic:** tag is written into those commits.

**--ignore-existing, -i**
: Ignore existing **Relative:** tags. When --write is also given, the
ignored **Relative:** tags are removed as well.

**--write, -w**
: Record each inferred relative as an **Optimized-Relative:** tag on the
first commit of its topic.

**--force-write, -f**
: Remove all existing **Relative:** and **Optimized-Relative:** tags and
record the inferred relatives as **Relative:** tags.
