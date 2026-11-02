"""
Greedily arrange the topics in a stack into a forest of relative chains with low total
depth. Placement is decided purely by whether each commit cherry-picks cleanly.
"""

from __future__ import annotations

import argparse
import itertools
import logging
from dataclasses import dataclass

from revup import git
from revup.core_types import CommitHeader, GitCommitHash, GitConflictException, RevupUsageException
from revup.topic_stack import (
    TAG_BRANCH,
    TAG_OPTIMIZED_RELATIVE,
    TAG_RELATIVE,
    TAG_RELATIVE_BRANCH,
    TAG_TOPIC,
    TopicStack,
    format_graph,
    relative_tags,
    single_tag,
    strip_tag_lines,
)


@dataclass
class Tree:
    # Commits replayed onto the base, in stack order
    commits: list[CommitHeader]
    # Tip of the replayed chain
    head: GitCommitHash
    # Commit the chain was replayed onto, either the base branch or a Relative-Branch
    base: GitCommitHash


@dataclass
class Forest:
    trees: list[Tree]
    # Topics that could not be fully placed, mapped to a description of the failure
    failed: dict[str, str]
    topic_of: dict[GitCommitHash, str]
    # Relative branch of trees not based on the base branch, keyed by base commit
    relative_branches: dict[GitCommitHash, str]

    def topics(self, tree: Tree) -> list[str]:
        return list(dict.fromkeys(self.topic_of[c.commit_id] for c in tree.commits))


def relative_topics(topics: TopicStack) -> dict[str, str]:
    """
    Map each topic to the topic its Relative: tag names, if that topic is in the stack.
    """
    ret = {}
    for name, topic in topics.topics.items():
        relative = topics.relative_in_stack(
            single_tag(topic, "relative topic", topic.tags[TAG_RELATIVE])
        )
        if relative:
            ret[name] = relative
    return ret


class Builder:
    def __init__(
        self,
        git_ctx: git.Git,
        topics: TopicStack,
        base: GitCommitHash,
        relative_of: dict[str, str],
        sequence: list[CommitHeader],
    ):
        self.git_ctx = git_ctx
        self.base = base
        self.order = {c.commit_id: i for i, c in enumerate(sequence)}
        self.relative_of = relative_of
        # Topics with a Relative-Branch are based on it instead of the base branch
        self.relative_branch_of = {
            name: branch
            for name, topic in topics.topics.items()
            if (branch := single_tag(topic, "relative branch", topic.tags[TAG_RELATIVE_BRANCH]))
        }
        self.topic_base: dict[str, GitCommitHash] = {}
        self.forest = Forest(
            [],
            {},
            {c.commit_id: name for name, t in topics.topics.items() for c in t.original_commits},
            {},
        )

    async def resolve_bases(self) -> None:
        for name, branch in self.relative_branch_of.items():
            base = await self.git_ctx.to_commit_hash(branch)
            self.topic_base[name] = base
            self.forest.relative_branches[base] = branch

    def base_of(self, name: str) -> GitCommitHash:
        return self.topic_base.get(name, self.base)

    def tree_of(self, name: str) -> Tree | None:
        return next((t for t in self.forest.trees if name in self.forest.topics(t)), None)

    async def try_pick(
        self, commit: CommitHeader, parent: GitCommitHash
    ) -> GitCommitHash | GitConflictException:
        try:
            return await self.git_ctx.synthetic_cherry_pick_from_commit(commit, parent)
        except GitConflictException as exc:
            return exc

    async def merge(self, members: list[Tree]) -> Tree | None:
        """
        Replay the commits of all members, which share a base, onto it in stack order, or
        return None if they don't apply cleanly together.
        """
        commits = sorted(
            itertools.chain.from_iterable(t.commits for t in members),
            key=lambda c: self.order[c.commit_id],
        )
        head = base = members[0].base
        for c in commits:
            result = await self.try_pick(c, head)
            if isinstance(result, GitConflictException):
                return None
            head = result
        return Tree(commits, head, base)

    async def merge_until_pick(
        self, own: Tree | None, commit: CommitHeader, eligible: list[Tree]
    ) -> bool:
        """
        Merge the topic's own tree (if any) with ever larger subsets of the other eligible
        trees sharing its base until the commit cherry-picks onto the result. The successful
        merge permanently replaces its members.
        """
        trees = self.forest.trees
        required = [own] if own else []
        others = [t for t in eligible if t is not own and (own is None or t.base == own.base)]
        for size in range(1 if own else 2, len(others) + 1):
            for combo in itertools.combinations(others, size):
                if len({t.base for t in combo}) > 1:
                    continue
                members = required + list(combo)
                merged = await self.merge(members)
                if merged is None:
                    continue
                result = await self.try_pick(commit, merged.head)
                if isinstance(result, GitConflictException):
                    continue
                merged.commits.append(commit)
                merged.head = result
                position = min(trees.index(t) for t in members)
                trees[:] = [t for t in trees if t not in members]
                trees.insert(position, merged)
                return True
        return False

    async def place(self, commit: CommitHeader) -> None:
        forest = self.forest
        name = forest.topic_of[commit.commit_id]
        own = self.tree_of(name)
        base = self.base_of(name)
        relative = self.relative_of.get(name)
        if own is None and relative is not None:
            # The topic must land in its relative's tree, which puts it after the relative in
            # the chain. Other topics may still end up between them.
            if relative in forest.failed:
                forest.failed[name] = f"relative topic {relative} could not be placed"
                return
            own = self.tree_of(relative)
            if own is None:
                forest.failed[name] = f"relative topic {relative} targets another branch"
                return
            if name in self.relative_branch_of and own.base != base:
                forest.failed[name] = (
                    f"relative topic {relative} is not on {self.relative_branch_of[name]}"
                )
                return

        # A topic with a Relative-Branch can only join trees based on it, any other topic can
        # join any tree since it inherits the relative branch from the chain
        eligible = [
            t for t in forest.trees if name not in self.relative_branch_of or t.base == base
        ]

        # A topic already in a tree must stay there. Otherwise prefer a new independent tree,
        # then any eligible tree in order.
        candidates: list[Tree | None] = [own] if own else [None, *eligible]
        for tree in candidates:
            result = await self.try_pick(commit, base if tree is None else tree.head)
            if isinstance(result, GitConflictException):
                continue
            if tree is None:
                forest.trees.append(Tree([commit], result, base))
            else:
                tree.commits.append(commit)
                tree.head = result
            return

        if not await self.merge_until_pick(own, commit, eligible):
            forest.failed[name] = f'commit {commit.commit_id[:12]} "{commit.title}" conflicts'


async def build_forest(git_ctx: git.Git, topics: TopicStack, ignore_existing: bool) -> Forest:
    # Topics targeting other base branches are left out entirely
    skipped = {
        name
        for name, topic in topics.topics.items()
        if topic.tags[TAG_BRANCH] and topics.base_branch not in topic.tags[TAG_BRANCH]
    }
    for skipped_name in skipped:
        logging.info(f"Skipping topic {skipped_name} since it doesn't target {topics.base_branch}")

    # Process topics the way upload applies them: each after the topic it is relative to,
    # otherwise in stack order, with a topic's commits taken consecutively
    relative_of = {} if ignore_existing else relative_topics(topics)
    for name, relative in relative_of.items():
        topics.topics[name].relative_topic = topics.topics[relative]
    ordered = [t for name, t in topics.topological_topics() if name not in skipped]
    sequence = [c for t in ordered for c in t.original_commits]

    builder = Builder(
        git_ctx, topics, await git_ctx.to_commit_hash(topics.relative_branch), relative_of, sequence
    )
    await builder.resolve_bases()
    for topic in ordered:
        before = [Tree(list(t.commits), t.head, t.base) for t in builder.forest.trees]
        for commit in topic.original_commits:
            await builder.place(commit)
            if topic.name in builder.forest.failed:
                # Drop whatever the topic placed, including merges done on its behalf, and
                # stop since later topics are likely to depend on it
                builder.forest.trees[:] = before
                return builder.forest
    return builder.forest


def format_tree(chain: list[str], relative_branch: str | None = None) -> str:
    """
    Render a chain of topics as a review graph, labeled with its relative branch if it isn't
    based on the base branch.
    """
    following = dict(zip(chain, chain[1:]))
    graph = format_graph(
        chain[0], lambda name: name, lambda name: [following[name]] if name in following else []
    )
    return f"{relative_branch}:\n{graph}" if relative_branch else graph


@dataclass
class RelativeChange:
    """
    A change to the relative in effect for a topic.
    """

    topic: str
    old: str | None
    new: str | None

    def __str__(self) -> str:
        if self.old is None:
            return f"{self.topic}: added relative {self.new}"
        if self.new is None:
            return f"{self.topic}: removed relative {self.old}"
        return f"{self.topic}: relative changed from {self.old} -> {self.new}"


def tag_mode(args: argparse.Namespace) -> tuple[bool, str]:
    """
    Return whether Relative tags get stripped and which tag records the forest.
    """
    strip_relative = args.force_write or args.ignore_existing
    return strip_relative, "Relative" if args.force_write else "Optimized-Relative"


def plan_changes(topics: TopicStack, forest: Forest) -> list[RelativeChange]:
    """
    Return how recording the forest changes each placed topic's relative, resolved the way
    upload does.
    """
    changes = []
    for tree in forest.trees:
        chain = forest.topics(tree)
        for previous, name in zip([None, *chain], chain):
            topic = topics.topics[name]
            old = single_tag(topic, "relative topic", relative_tags(topic))
            if old not in topics.topics:
                old = None
            if old != previous:
                changes.append(RelativeChange(name, old, previous))
    return changes


def append_tag(msg: str, line: str) -> str:
    """
    Append a tag line, keeping a blank line between a bare subject and its first tag.
    """
    return msg + ("\n" if "\n\n" in msg else "\n\n") + line


async def write_tags(
    git_ctx: git.Git, topics: TopicStack, forest: Forest, strip_relative: bool, tag: str
) -> None:
    """
    Rewrite the stack to record the forest, stripping Optimized-Relative and optionally Relative
    tags from every commit of each topic and tagging its first commit with the topic before
    it, reusing each commit's tree. Commits whose topic was assigned automatically get a Topic
    tag.
    """
    stripped = (
        {TAG_RELATIVE, TAG_OPTIMIZED_RELATIVE} if strip_relative else {TAG_OPTIMIZED_RELATIVE}
    )
    previous_of: dict[str, str | None] = {}
    for tree in forest.trees:
        chain = forest.topics(tree)
        previous_of.update(zip(chain, [None, *chain]))

    new_msgs: dict[GitCommitHash, str] = {}
    tagged: set[str] = set()
    for commit in topics.commits:
        name = forest.topic_of.get(commit.commit_id)
        if name not in previous_of:
            continue
        msg = strip_tag_lines(commit.commit_msg, lambda t, _: t in stripped)
        if not topics.parse_commit_tags(commit.commit_msg)[0][TAG_TOPIC]:
            msg = append_tag(msg, f"Topic: {name}")
        previous = previous_of[name]
        if name not in tagged:
            tagged.add(name)
            if previous is not None and (
                strip_relative or previous not in topics.topics[name].tags[TAG_RELATIVE]
            ):
                msg = append_tag(msg, f"{tag}: {previous}")
        if msg != commit.commit_msg:
            new_msgs[commit.commit_id] = msg
    if not new_msgs:
        return

    first = next(i for i, c in enumerate(topics.commits) if c.commit_id in new_msgs)
    for commit in topics.commits[first:]:
        commit.commit_msg = new_msgs.get(commit.commit_id, commit.commit_msg)
    new_head = await git_ctx.reparent_commits(
        topics.commits[first:], topics.commits[first].parents[0]
    )
    flag = "--force-write" if tag == "Relative" else "--write"
    await git_ctx.soft_reset(new_head, {"GIT_REFLOG_ACTION": f"revup optimize-stack {flag}"})


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    if args.write and args.force_write:
        raise RevupUsageException("--write and --force-write are mutually exclusive")

    topics = TopicStack(git_ctx, args.base_branch, args.relative_branch)
    await topics.populate_topics(auto_topic=args.auto_topic, raise_on_invalid=True)
    forest = await build_forest(git_ctx, topics, args.ignore_existing)
    if forest.failed:
        for name, reason in forest.failed.items():
            logging.error(f"Topic {name} can't be placed in any tree: {reason}")
        return 1

    changes = plan_changes(topics, forest)
    for change in changes:
        print(change)
    if changes:
        print()
    for tree in forest.trees:
        relative_branch = forest.relative_branches.get(tree.base)
        print(
            format_tree(
                forest.topics(tree),
                relative_branch and git_ctx.remove_branch_prefix(relative_branch),
            ),
            end="",
        )
    if args.write or args.force_write:
        await write_tags(git_ctx, topics, forest, *tag_mode(args))
    untagged = args.auto_topic and any(
        not topics.parse_commit_tags(c.commit_msg)[0][TAG_TOPIC] for c in topics.commits
    )
    if not (args.write or args.force_write) and (changes or untagged):
        print("\nRun with --write to modify commit tags to use this structure")
    return 0
