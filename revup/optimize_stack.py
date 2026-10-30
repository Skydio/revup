"""
Greedily arrange the topics in a stack into a forest of relative chains with low total
depth. Placement is decided purely by whether each commit cherry-picks cleanly, never by
inspecting commit contents.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from revup import git
from revup.core_types import CommitHeader, GitCommitHash, GitConflictException
from revup.topic_stack import (
    TAG_OPTIMIZED_RELATIVE,
    TAG_RELATIVE,
    TopicStack,
    format_graph,
    single_tag,
    strip_tag_lines,
)


@dataclass
class Tree:
    # Commits replayed onto the base branch, in stack order
    commits: list[CommitHeader]
    # Tip of the replayed chain
    head: GitCommitHash


@dataclass
class Forest:
    trees: list[Tree]
    # Topics that could not be fully placed, mapped to a description of the failure
    failed: dict[str, str]
    topic_of: dict[GitCommitHash, str]
    # The conflict behind each failure that was caused by one
    conflicts: dict[str, GitConflictException]

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
        self, git_ctx: git.Git, topics: TopicStack, base: GitCommitHash, ignore_existing: bool
    ):
        self.git_ctx = git_ctx
        self.base = base
        self.order = {c.commit_id: i for i, c in enumerate(topics.commits)}
        self.relative_of = {} if ignore_existing else relative_topics(topics)
        self.forest = Forest(
            [],
            {},
            {c.commit_id: name for name, t in topics.topics.items() for c in t.original_commits},
            {},
        )

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
        Replay the commits of all members onto the base in stack order, or return None if
        they don't apply cleanly together.
        """
        commits = sorted(
            itertools.chain.from_iterable(t.commits for t in members),
            key=lambda c: self.order[c.commit_id],
        )
        head = self.base
        for c in commits:
            result = await self.try_pick(c, head)
            if isinstance(result, GitConflictException):
                return None
            head = result
        return Tree(commits, head)

    async def merge_until_pick(self, own: Tree | None, commit: CommitHeader) -> bool:
        """
        Merge the topic's own tree (if any) with ever larger subsets of the other trees until
        the commit cherry-picks onto the result. The successful merge permanently replaces its
        members.
        """
        trees = self.forest.trees
        required = [own] if own else []
        others = [t for t in trees if t is not own]
        for size in range(1 if own else 2, len(others) + 1):
            for combo in itertools.combinations(others, size):
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
        relative = self.relative_of.get(name)
        if own is None and relative is not None:
            # The topic must land in its relative's tree, which puts it after the relative in
            # the chain. Other topics may still end up between them.
            if relative in forest.failed:
                forest.failed[name] = f"relative topic {relative} could not be placed"
                return
            own = self.tree_of(relative)
            if own is None:
                forest.failed[name] = f"relative topic {relative} appears later in the stack"
                return

        # A topic already in a tree must stay there. Otherwise prefer a new independent tree,
        # then any existing tree in order.
        candidates: list[Tree | None] = [own] if own else [None, *forest.trees]
        first_conflict = None
        for tree in candidates:
            result = await self.try_pick(commit, self.base if tree is None else tree.head)
            if isinstance(result, GitConflictException):
                first_conflict = first_conflict or result
                continue
            if tree is None:
                forest.trees.append(Tree([commit], result))
            else:
                tree.commits.append(commit)
                tree.head = result
            return

        assert first_conflict is not None
        if not await self.merge_until_pick(own, commit):
            forest.failed[name] = f'commit {commit.commit_id[:12]} "{commit.title}" conflicts'
            forest.conflicts[name] = first_conflict


async def build_forest(git_ctx: git.Git, topics: TopicStack, ignore_existing: bool) -> Forest:
    builder = Builder(
        git_ctx, topics, await git_ctx.to_commit_hash(topics.base_branch), ignore_existing
    )
    for commit in topics.commits:
        name = builder.forest.topic_of.get(commit.commit_id)
        if name is not None and name not in builder.forest.failed:
            await builder.place(commit)
    return builder.forest


def format_tree(chain: list[str]) -> str:
    """
    Render a chain of topics as a review graph.
    """
    following = dict(zip(chain, chain[1:]))
    return format_graph(
        chain[0], lambda name: name, lambda name: [following[name]] if name in following else []
    )


async def write_tags(git_ctx: git.Git, topics: TopicStack, forest: Forest) -> int:
    """
    Rewrite the stack so the first commit of each placed topic carries an Optimized-Relative
    tag naming the topic before it in its tree. No tag is written for a topic that heads a
    tree or whose Relative tag already names the same topic. Stale Optimized-Relative tags are
    removed, failed topics are left alone, and trees are reused since contents don't change.
    Returns the number of commits whose message changed.
    """
    inferred: dict[str, str | None] = {}
    for tree in forest.trees:
        chain = forest.topics(tree)
        inferred.update(zip(chain, [None, *chain]))

    new_msgs: dict[GitCommitHash, str] = {}
    tagged: set[str] = set()
    for commit in topics.commits:
        name = forest.topic_of.get(commit.commit_id)
        if name not in inferred:
            continue
        msg = strip_tag_lines(commit.commit_msg, lambda tag, _: tag == TAG_OPTIMIZED_RELATIVE)
        relative = inferred[name]
        if name not in tagged:
            tagged.add(name)
            if relative is not None and relative not in topics.topics[name].tags[TAG_RELATIVE]:
                msg += f"\nOptimized-Relative: {relative}"
        if msg != commit.commit_msg:
            new_msgs[commit.commit_id] = msg
    if not new_msgs:
        return 0

    first = next(i for i, c in enumerate(topics.commits) if c.commit_id in new_msgs)
    for commit in topics.commits[first:]:
        commit.commit_msg = new_msgs.get(commit.commit_id, commit.commit_msg)
    new_head = await git_ctx.reparent_commits(
        topics.commits[first:], topics.commits[first].parents[0]
    )
    await git_ctx.soft_reset(
        new_head, {"GIT_REFLOG_ACTION": "revup toolkit optimize-stack --write"}
    )
    return len(new_msgs)
