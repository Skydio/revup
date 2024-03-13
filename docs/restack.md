# NAME

revup restack - Reorder commits to group topics together.

# SYNOPSIS

`revup [--verbose] [--keep-temp]`
: `restack [--help] [--base-branch=<base>] [--num-commits=<N>]`
`[--relative-chain]`

# DESCRIPTION

Parse commits up to the base branch and group them into topics.
First apply all commits without a topic in the order they appear.
Then apply all commits in each topic in topological order, placing
all topics after (although not necessarily immediately after) any
topic they are relative to. The resulting stack will be grouped to
make it more convenient to view history and perform interactive rebases.

Any empty commit without topics, and any topics consisting only
of empty commits are dropped. An empty commit that is part of
a topic that has nonempty commits is not dropped.

The cache and working directory are never modified regardless
of whether the command succeeds or fails.

If any conflict arises, the conflicting file paths and conflict markers
will be printed, and the conflicts will need to be fixed manually by
adjusting relative topics.

See the help page for **revup upload** for a description of how topic
tags are parsed.

# OPTIONS

**--help, -h**
: Show this help page.

**--base-branch, -b**
: Instead of automatically detecting the base branch, use the given
branch as the base.

**--relative-chain, -r**
: Ignore all relative topic tags and instead act as though each topic is
relative to the topic before it. This can save effort typing out relative
tags when you know all reviews in the stack will be dependent.

**--topicless-last, -t**
: Apply all topicless commits last (at the top of the commit stack) instead
of first.
