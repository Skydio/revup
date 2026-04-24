import argparse
import copy
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from revup import git, topic_stack
from revup.topic_stack import (
    TAG_ASSIGNEE,
    TAG_BRANCH,
    TAG_BRANCH_FORMAT,
    TAG_LABEL,
    TAG_RELATIVE,
    TAG_RELATIVE_BRANCH,
    TAG_REVIEWER,
    TAG_TOPIC,
    TAG_UPDATE_PR_BODY,
    TAG_UPLOADER,
    TopicStack,
    add_tags,
)
from revup.types import (
    GitCommitHash,
    GitConflictException,
    GitTreeHash,
    RevupConflictException,
)

TAG_ORDER = [
    TAG_TOPIC,
    TAG_RELATIVE,
    TAG_RELATIVE_BRANCH,
    TAG_BRANCH,
    TAG_REVIEWER,
    TAG_ASSIGNEE,
    TAG_LABEL,
    TAG_UPLOADER,
    TAG_UPDATE_PR_BODY,
    TAG_BRANCH_FORMAT,
]


def merge_commit_messages(topics: TopicStack, commits: List[git.CommitHeader]) -> str:
    all_tags: Dict[str, Set[str]] = defaultdict(set)
    bodies: List[str] = []

    for commit in commits:
        tags, trimmed = topics.parse_commit_tags(commit.commit_msg)
        add_tags(all_tags, tags)
        if trimmed:
            bodies.append(trimmed)

    merged = "\n\n".join(bodies)

    tag_lines = []
    for tag in TAG_ORDER:
        if tag in all_tags:
            tag_lines.append(f"{tag.capitalize()}: {', '.join(sorted(all_tags[tag]))}")
    if tag_lines:
        merged += "\n\n" + "\n".join(tag_lines)

    return merged


async def squash_topics(
    topics: TopicStack,
    head: GitCommitHash,
    to_restack: List[git.CommitHeader],
) -> GitCommitHash:
    topic_commit_groups: List[Tuple[str, List[git.CommitHeader]]] = []
    current_name: Optional[str] = None
    current_group: List[git.CommitHeader] = []

    for commit in to_restack:
        name = None
        for topic_name, topic in topics.topics.items():
            if commit in topic.original_commits:
                name = topic_name
                break

        if name != current_name:
            if current_group:
                topic_commit_groups.append((current_name or "", current_group))
            current_name = name
            current_group = [commit]
        else:
            current_group.append(commit)
    if current_group:
        topic_commit_groups.append((current_name or "", current_group))

    base = await topics.git_ctx.git_stdout("rev-parse", f"{head}~{len(to_restack)}")
    new_parent = GitCommitHash(base)

    for name, group in topic_commit_groups:
        if len(group) <= 1 or name == "":
            for commit in group:
                new_parent = await topics.git_ctx.synthetic_cherry_pick_from_commit(
                    commit, new_parent
                )
        else:
            pick_parent = new_parent
            for commit in group:
                pick_parent = await topics.git_ctx.synthetic_cherry_pick_from_commit(
                    commit, pick_parent
                )
            squashed = copy.deepcopy(group[0])
            squashed.tree = GitTreeHash(
                await topics.git_ctx.git_stdout("rev-parse", f"{pick_parent}^{{tree}}")
            )
            squashed.parents = [new_parent]
            squashed.commit_msg = merge_commit_messages(topics, group)
            new_parent = await topics.git_ctx.commit_tree(squashed)

    return new_parent


async def restack(topics: TopicStack, topicless_last: bool, squash: bool = False) -> GitCommitHash:
    to_pick = []
    for _, topic in topics.topological_topics():
        this_topic = []
        topic_is_empty = True
        for commit in topic.original_commits:
            this_topic.append(commit)
            if not await topics.git_ctx.have_identical_trees(commit.commit_id, commit.parents[0]):
                topic_is_empty = False
        # Drop empty topics, ie topics with all empty commits. git pull --rebase
        # doesn't automatically drop empty commits if they're been merged.
        if not topic_is_empty:
            to_pick.extend(this_topic)
    no_topic = []
    for commit in topics.commits:
        if commit not in to_pick and not await topics.git_ctx.have_identical_trees(
            commit.commit_id, commit.parents[0]
        ):
            no_topic.append(commit)

    new_parent = topics.commits[0].parents[0]
    if topicless_last:
        to_restack = to_pick + no_topic
    else:
        to_restack = no_topic + to_pick
    for commit in to_restack:
        try:
            new_parent = await topics.git_ctx.synthetic_cherry_pick_from_commit(commit, new_parent)
        except GitConflictException as exc:
            await topics.git_ctx.dump_conflict(exc)
            raise RevupConflictException(
                commit,
                new_parent,
                "You may need to `git rebase -i` to resolve these conflicts!",
            ) from exc

    if squash:
        new_parent = await squash_topics(topics, new_parent, to_restack)

    git_env = {
        "GIT_REFLOG_ACTION": "reset --soft (revup restack)",
    }
    await topics.git_ctx.soft_reset(new_parent, git_env)
    return new_parent


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Handles the "restack" command.
    """
    topics = topic_stack.TopicStack(
        git_ctx,
        args.base_branch,
        args.relative_branch,
        None,
        None,
    )

    await topics.populate_topics()
    await topics.populate_reviews()
    await restack(topics, args.topicless_last, args.squash)
    return 0
