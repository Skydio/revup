from __future__ import annotations

import logging
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Iterator, List, Optional, Set, Tuple

from rich import get_console
from rich.markup import escape

from revup import git, github, github_utils
from revup.github_utils import PrComment, PrInfo, PrUpdate
from revup.types import (
    GitCommitHash,
    GitConflictException,
    RevupConflictException,
    RevupUsageException,
)


def format_remote_branch(uploader: str, base_branch: str, topic: str) -> str:
    """
    Branches are named so that it is clear that they are made by revup
    and can be force pushed at any time, and to minimize collision with
    manually created branches.
    """
    return f"{uploader}/revup/{base_branch}/{topic}"


RE_TAGS = re.compile(r"^(?P<tagname>[a-zA-Z\-]+):(?P<tagvalue>.*)$", re.MULTILINE)

TAG_REVIEWER = "reviewer"
TAG_ASSIGNEE = "assignee"
TAG_BRANCH = "branch"
TAG_LABEL = "label"
TAG_TOPIC = "topic"
TAG_RELATIVE = "relative"
TAG_RELATIVE_BRANCH = "relative-branch"
TAG_UPLOADER = "uploader"
VALID_TAGS = {
    TAG_BRANCH,
    TAG_LABEL,
    TAG_RELATIVE,
    TAG_RELATIVE_BRANCH,
    TAG_REVIEWER,
    TAG_ASSIGNEE,
    TAG_TOPIC,
    TAG_UPLOADER,
}

RE_COMMIT_LABEL = re.compile(r"^(?P<label1>[a-zA-Z\-_0-9]+):.*|^\[(?P<label2>[a-zA-Z\-_0-9]+)\].*")

PATCHSETS_FIRST_LINE = "| # | head | base | diff | date | summary |\r\n| - | - | - | - | - | - |"
REVIEW_GRAPH_FIRST_LINE = "Reviews in this chain:\r\n"


def add_tags(original: Dict[str, Set[str]], new: Dict[str, Set[str]]) -> None:
    """
    Update original with tags from new.
    """
    for tag, val in new.items():
        original[tag].update(val)


def translate_if_exists(names: Set[str], translation: Dict[str, str]) -> Set[str]:
    """
    Return the translation entry for each name, only if it exists.
    """
    return set(translation[name] for name in names if name in translation)


# The current state of each review within github.
class PrStatus(Enum):
    NEW = "new"  # needs to be created, or was just created
    UPDATED = "updated"  # github data needs to be modified (title, reviewers, labels, etc)
    NOCHANGE = "no change"  # no github mutations are necessary
    MERGED = "already merged"  # change has already merged (and no mutations are possible)


# The status of the git branch
class PushStatus(Enum):
    PUSHED = "pushed"  # commit hash for the branch changed and will or has been pushed
    REBASE = "rebase"  # branch is not being pushed because it is a rebase
    NOCHANGE = "no change"  # branch is not being pushed because it has not changed at all


@dataclass
class Review:
    """
    Represents a single github pull request. Uniquely keyed by topic name and base branch.
    """

    # Reference to the enclosing topic object
    topic: Topic

    # The local base ref that is the parent of all commits in new_commits
    base_ref: Optional[GitCommitHash] = None

    # The commits actually used for the review. These may have been created
    # by cherry-picking. The last commit is the one that will be pushed to the
    # remote ref.
    new_commits: List[GitCommitHash] = field(default_factory=list)

    # Name for the remote head ref. Will be based on topic name + base branch
    remote_head: str = ""

    # Name for the remote base ref. One of a base branch / relative branch / another topic's head
    remote_base: str = ""

    # Name of a relative branch if it exists
    relative_branch: str = ""

    # List of commits the remote has for this review
    remote_commits: List[git.CommitHeader] = field(default_factory=list)

    # Corresponding output of git patch-id for each remote commit
    remote_patch_ids: List[str] = field(default_factory=list)

    # Existing PR details for this review. None if no PR currently exists
    pr_info: Optional[PrInfo] = None

    # PR update argument for this review
    pr_update: PrUpdate = field(default_factory=PrUpdate)

    # Whether this review was newly created (as opposed to updated)
    status: PrStatus = PrStatus.NOCHANGE

    # Whether the review is a pure rebase of the remote changes.
    is_pure_rebase: bool = False

    # Whether refs needed to be git pushed.
    push_status: PushStatus = PushStatus.PUSHED

    # Other reviews that have marked this one as relative.
    children: List[Review] = field(default_factory=list)

    # Whether a PR is a draft
    is_draft: bool = False

    # Comment indexes identify a matching comment for the given feature to update.
    # If greater than len(pr_info.comments), identifies a new comment.
    review_graph_index: Optional[int] = None
    patchsets_index: Optional[int] = None


@dataclass
class Topic:
    """
    Represents a series of commits that could be relative to another topic.
    It can have multiple reviews -- one for each base branch.
    """

    # Name of the topic
    name: str

    # The local topic that this topic is relative to
    relative_topic: Optional[Topic] = None

    # Original commits included in this topic
    original_commits: List[git.CommitHeader] = field(default_factory=list)

    # Corresponding output of git patch-id for each commit
    patch_ids: List[str] = field(default_factory=list)

    # Tags for this topic (union of all tags for commits in the topic)
    tags: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))

    # Reviews for this topic, keyed by base branch
    reviews: Dict[str, Review] = field(default_factory=dict)


@dataclass
class TopicStack:
    """
    Constructs and manages data related to topics and reviews.
    """

    # Git context
    git_ctx: git.Git

    # Default base branch for topics that don't specify one
    base_branch: str

    # Branch / review that current reviews are relative to
    relative_branch: str

    # Github access info
    github_ep: Optional[github.GitHubEndpoint] = None
    repo_info: Optional[github_utils.GitHubRepoInfo] = None
    fork_info: Optional[github_utils.GitHubRepoInfo] = None

    # Original list of relevant commits given to the submitter
    commits: List[git.CommitHeader] = field(default_factory=list)

    # Topic names to topic info
    topics: Dict[str, Topic] = field(default_factory=dict)

    # Github node id of the repo
    repo_id: Optional[str] = None

    # Github node ids of users (reviewer/assignee)
    names_to_ids: Optional[Dict[str, str]] = None

    # Github full login names of users
    names_to_logins: Optional[Dict[str, str]] = None

    # Github node ids of labels
    labels_to_ids: Optional[Dict[str, str]] = None

    # Relative branch names to pr_info for those branches
    relative_infos: Dict[str, PrInfo] = field(default_factory=dict)

    # All virtual diff targets for the current upload are chained into a dummy branch
    last_virtual_diff_target: Optional[GitCommitHash] = None

    # Whether populate() was successfully called
    populated: bool = False

    def all_reviews_iter(self) -> Iterator[Tuple[str, Topic, str, Review]]:
        """
        One liner for common iteration pattern to reduce indentation a bit.
        """
        for name, topic in self.topics.items():
            for base_branch, review in topic.reviews.items():
                yield name, topic, base_branch, review

    def parse_commit_tags(self, commit_msg: str) -> Tuple[Dict[str, Set[str]], str]:
        """
        Parse all commit tags in the commit message and return them in a dict, as well as
        a version of the message with commit tags removed.
        Tag parsing is fairly generous, tags can appear in any case and can accept plural forms
        Values are comma separated, and if a tag appears multiple times its values get combined
        """
        ret = defaultdict(set)
        trimmed_msg = []
        for ln in commit_msg.split("\n"):
            m = RE_TAGS.match(ln)
            if m is None:
                trimmed_msg.append(ln)
                continue
            tag = m.group("tagname").lower().strip()
            val = set(s.strip() for s in m.group("tagvalue").split(","))
            if (
                not tag.startswith(TAG_RELATIVE)
                and not tag.startswith(TAG_RELATIVE_BRANCH)
                and not tag.startswith(TAG_TOPIC)
                and not tag.startswith(TAG_UPLOADER)
            ):
                # That's right, plurals don't even have to be grammatically correct
                if tag.endswith("ees"):
                    tag = tag[:-1]
                elif tag.endswith("es"):
                    tag = tag[:-2]
                elif tag.endswith("s"):
                    tag = tag[:-1]
            val.discard("")  # Discards any whitespace only values, since it was stripped prior
            if tag in VALID_TAGS:
                if tag in (TAG_BRANCH, TAG_RELATIVE_BRANCH):
                    val = set(self.git_ctx.ensure_branch_prefix(b) for b in val)
                ret[tag].update(val)
            else:
                trimmed_msg.append(ln)
        return ret, "\n".join(trimmed_msg).strip()

    async def create_patchsets_comment(
        self, review: Review, orig: Optional[PrComment]
    ) -> Optional[PrComment]:
        if (
            review.push_status != PushStatus.PUSHED
            or review.status == PrStatus.MERGED
            or not self.repo_info
            or not self.fork_info
            or review.pr_info is None
            or review.base_ref is None
        ):
            return None

        if not orig:
            # No PR yet or no comment yet, add the initial header.
            ret = PATCHSETS_FIRST_LINE
            number = 0
        elif not orig.text.startswith(PATCHSETS_FIRST_LINE):
            # First comment isn't a revup patchsets comment, so don't try to modify it.
            return None
        else:
            # Parse the patch number from the previous patch
            ret = orig.text
            last_line = ret.split("\r\n")[-1].split("|")
            if len(last_line) < 2:
                return None
            try:
                number = int(last_line[1]) + 1
            except ValueError:
                return None

        if not review.is_pure_rebase:
            if review.status == PrStatus.NEW:
                # New PR, diff against base to show full diff
                diff_base = review.base_ref
            elif review.base_ref != review.pr_info.baseRefOid:
                # Rebased review, make a virtual diff target
                if self.last_virtual_diff_target is None:
                    self.last_virtual_diff_target = GitCommitHash(self.base_branch)
                self.last_virtual_diff_target = await self.git_ctx.make_virtual_diff_target(
                    review.pr_info.baseRefOid,
                    review.pr_info.headRefOid,
                    review.base_ref,
                    review.new_commits[-1],
                    self.last_virtual_diff_target,
                )
                diff_base = self.last_virtual_diff_target
            else:
                # Non rebase push, diff against previous version of the branch
                diff_base = review.pr_info.headRefOid
            diff = (
                f"[diff](/{self.fork_info.owner}/{self.repo_info.name}/compare/"
                f"{diff_base}..{review.new_commits[-1]})"
            )
            summary = await self.git_ctx.get_diff_summary(diff_base, review.new_commits[-1])
            if not summary:
                summary = "0 files changed"
        else:
            # Skip the effort of making a virtual diff target since we already know this is a rebase
            diff = "rebase"
            summary = "0 files changed"

        d = datetime.now()
        ret += (
            f"\r\n| {number} | [{review.new_commits[-1][:8]}]"
            f"(/{self.fork_info.owner}/{self.repo_info.name}/commit/{review.new_commits[-1]}) "
            f"| [{review.base_ref[:8]}]"
            f"(/{self.fork_info.owner}/{self.repo_info.name}/commit/{review.base_ref}) "
            f"| {diff} | {d:%b} {d.day} {d.hour}:{d.minute:02} {d:%p} | {summary} |"
        )
        return PrComment(ret, orig.id if orig else None)

    async def populate_topics(
        self,
        auto_topic: bool = False,
        trim_tags: bool = False,
    ) -> None:
        """
        Parse all commits and sort them into individual topics.
        """
        if self.populated:
            return
        if self.base_branch:
            self.base_branch = self.git_ctx.ensure_branch_prefix(self.base_branch)
            await self.git_ctx.verify_branch_or_commit(self.base_branch)
        else:
            # Base branch can be autodetected if not specified
            self.base_branch = await self.git_ctx.get_best_base_branch("HEAD", True)

        if self.relative_branch:
            self.relative_branch = self.git_ctx.ensure_branch_prefix(self.relative_branch)
            await self.git_ctx.verify_branch_or_commit(self.relative_branch)
        else:
            # If relative branch is not specified, its just the base branch
            self.relative_branch = self.base_branch

        branch_point = await self.git_ctx.fork_point("HEAD", self.relative_branch)
        if self.base_branch != self.relative_branch:
            base_branch_point = await self.git_ctx.fork_point("HEAD", self.base_branch)
            if not await self.git_ctx.is_ancestor(base_branch_point, branch_point):
                # Our model expects relative branch to be forked off base branch, and HEAD
                # to be forked off relative branch.
                raise RevupUsageException(
                    "Relative branch structure is invalid: HEAD is closer to"
                    f" {self.base_branch} than {self.relative_branch}."
                    f"Specifically we expect the fork point with {self.base_branch} "
                    f"({base_branch_point}) to be an ancestor of the fork point with "
                    f"{self.relative_branch} ({branch_point})."
                )

        self.commits = git.parse_rev_list(
            await self.git_ctx.rev_list("HEAD", branch_point, header=True, first_parent=True)
        )

        # Parse tags and add each commit to appropriate topics
        for c in self.commits:
            parsed_tags, trimmed_msg = self.parse_commit_tags(c.commit_msg)
            if not parsed_tags[TAG_TOPIC]:
                if auto_topic:
                    parsed_tags[TAG_TOPIC].add(
                        "_".join(
                            trimmed_msg.split("\n", maxsplit=1)[0].lower().split()[:5]
                        ).translate({ord(":"): None, ord("["): None, ord("]"): None})
                    )
                else:
                    # No topic tags, not a revup commit
                    continue

            if len(parsed_tags[TAG_TOPIC]) > 1:
                raise RevupUsageException(
                    f"Can't specify more than one topic for a commit!\n\n{c.commit_msg}"
                )
            else:
                if trim_tags:
                    c.commit_msg = trimmed_msg
                name = min(parsed_tags[TAG_TOPIC])
                if name not in self.topics:
                    self.topics[name] = Topic(name)
                self.topics[name].original_commits.append(c)
                add_tags(self.topics[name].tags, parsed_tags)
        self.populated = True

    async def populate_reviews(
        self,
        uploader: str,
        force_relative_chain: bool = False,
        labels: str = None,
        user_aliases: str = "",
        auto_add_users: str = "",
        self_authored_only: bool = False,
    ) -> None:
        """
        Populate reviews for already-parsed topics. Verify base branch and relative topic info to
        ensure it is valid.
        """
        seen_topics: Dict[str, Topic] = {}
        for name, topic in list(self.topics.items()):
            relative_topic = ""

            if self_authored_only:
                # Don't upload if this topic doesn't have commits authored by the current user
                # Do this early so we don't throw errors for changes that won't be uploaded
                has_self_authored = False
                for c in topic.original_commits:
                    if c.author_email.lower() == self.git_ctx.email:
                        has_self_authored = True
                if not has_self_authored:
                    logging.info(
                        f"Skipping topic '{name}' since it has no self-authored commits,"
                        " pass '--no-self-authored-only' to override"
                    )
                    del self.topics[name]
                    continue

            if len(topic.tags[TAG_UPLOADER]) > 1:
                raise RevupUsageException(f"Can't specify more than one uploader for topic {name}!")

            if force_relative_chain and seen_topics:
                relative_topic = list(seen_topics)[-1]
            elif len(topic.tags[TAG_RELATIVE]) > 1:
                raise RevupUsageException(
                    "Can't specify more than 1 relative topic per topic! Got"
                    f" {topic.tags[TAG_RELATIVE]} for topic {name}"
                )
            elif len(topic.tags[TAG_RELATIVE]) == 1:
                # Each topic can have at most 1 relative topic.
                # If the topic doesn't specify base branches, it will automatically get
                # all the base branches for the relative topic. However it can't specify
                # any base branches the relative topic doesn't have.
                relative_topic = min(topic.tags[TAG_RELATIVE])
                if relative_topic not in seen_topics:
                    if relative_topic in self.topics:
                        # Relative topics can have interleaved commits, however the first commit of
                        # the relative topic must come before the first commit of this topic. This
                        # prevents cycles of relatives.
                        raise RevupUsageException(
                            f"Topic '{name}' is relative to '{relative_topic}' but doesn't appear"
                            " after it"
                        )
                    else:
                        logging.warning(
                            f"Relative topic '{relative_topic}' not found in stack, assuming it was"
                            " merged"
                        )
                        relative_topic = ""

            if self.repo_info and self.fork_info and self.fork_info.owner != self.repo_info.owner:
                if len(topic.tags[TAG_RELATIVE_BRANCH]) > 1:
                    raise RevupUsageException(
                        "Can't use 'Relative-Branch' across forks due to github limitations!"
                    )
                if relative_topic:
                    logging.warning(
                        f"Skipping topic '{name}' since github does not allow relative reviews"
                        f" across forks. It will be uploaded when '{relative_topic}' merges."
                    )
                    del self.topics[name]
                    continue

            if relative_topic:
                topic.relative_topic = self.topics[relative_topic]
                if len(topic.tags[TAG_BRANCH]) == 0:
                    topic.tags[TAG_BRANCH].update(topic.relative_topic.tags[TAG_BRANCH])
                elif not topic.tags[TAG_BRANCH].issubset(topic.relative_topic.tags[TAG_BRANCH]):
                    raise RevupUsageException(
                        f"Topic {name} has branches"
                        f" {topic.tags[TAG_BRANCH] - topic.relative_topic.tags[TAG_BRANCH]} not in"
                        f" relative topic {relative_topic}"
                    )

                if len(topic.tags[TAG_RELATIVE_BRANCH]) == 0:
                    topic.tags[TAG_RELATIVE_BRANCH].update(
                        topic.relative_topic.tags[TAG_RELATIVE_BRANCH]
                    )
                elif (
                    topic.tags[TAG_RELATIVE_BRANCH]
                    != topic.relative_topic.tags[TAG_RELATIVE_BRANCH]
                ):
                    raise RevupUsageException(
                        f"Topic {name} and relative topic {relative_topic} have differing relative "
                        f"branches, {topic.tags[TAG_RELATIVE_BRANCH]} vs "
                        f"{topic.relative_topic.tags[TAG_RELATIVE_BRANCH]}"
                    )
            else:
                # No relative topic specified. Base ref is just the branch(es)
                if len(topic.tags[TAG_BRANCH]) == 0:
                    topic.tags[TAG_BRANCH].add(self.base_branch)
                    if len(topic.tags[TAG_RELATIVE_BRANCH]) == 0:
                        # Only add the default relative branch if the review is using the default
                        # branch. If the user manually specified the default branch, it indicates
                        # they don't want the default relative branch.
                        topic.tags[TAG_RELATIVE_BRANCH].add(self.relative_branch)
                    else:
                        # User has specified a relative branch without a base branch. We'll allow
                        # this for now but if it generally results in confusion since the base
                        # branch will be set to the default and not autodetected, we'll raise.
                        pass

            # Each topic can have at most 1 relative branch.
            # If there is a relative branch, only one base branch can be specified, and all
            # topics in the same chain must use the same relative branch and base branch.
            if len(topic.tags[TAG_RELATIVE_BRANCH]) > 1:
                raise RevupUsageException(
                    "Can't specify more than 1 relative branch per topic! Got"
                    f" {topic.tags[TAG_RELATIVE_BRANCH]} for topic {name}"
                )
            elif topic.tags[TAG_RELATIVE_BRANCH] and len(topic.tags[TAG_BRANCH]) > 1:
                raise RevupUsageException(
                    "Can't specify more than one base branch when there is a relative branch! Got"
                    f" {topic.tags[TAG_BRANCH]} for topic {name}"
                )

            if (
                topic.tags[TAG_UPLOADER]
                and topic.relative_topic
                and topic.tags[TAG_UPLOADER] != topic.relative_topic.tags[TAG_UPLOADER]
            ):
                raise RevupUsageException(
                    f"Topic {name} has uploader '{topic.tags[TAG_UPLOADER]}' while relative topic"
                    f" {relative_topic} has uploader"
                    f" {topic.relative_topic.tags[TAG_UPLOADER] or '{}'}"
                )
            topic_uploader = min(topic.tags[TAG_UPLOADER]) if topic.tags[TAG_UPLOADER] else uploader

            for c in topic.original_commits:
                m = RE_COMMIT_LABEL.match(c.commit_msg)
                if m:
                    extra_label = m.group("label1") or m.group("label2") or None
                    if extra_label:
                        topic.tags[TAG_LABEL].add(extra_label.lower())

            if labels is not None:
                topic.tags[TAG_LABEL].update([label.lower() for label in labels.split(",")])

            if user_aliases is not None:
                for mapping in user_aliases.split(","):
                    # Map usernames from alias -> user_target
                    alias, _, user_target = mapping.partition(":")
                    for tag in [TAG_REVIEWER, TAG_ASSIGNEE]:
                        if alias in topic.tags[tag]:
                            topic.tags[tag].remove(alias)
                            topic.tags[tag].add(user_target)

            for branch in topic.tags[TAG_BRANCH]:
                review = Review(topic)
                # Track whether we need to query for the relative pr
                review.relative_branch = (
                    min(topic.tags[TAG_RELATIVE_BRANCH]) if topic.tags[TAG_RELATIVE_BRANCH] else ""
                )
                # Don't query if relative and base branch are the same
                if review.relative_branch == branch:
                    review.relative_branch = ""
                relative_branch = review.relative_branch or branch
                base_branch = self.git_ctx.remove_branch_prefix(branch)

                if topic.relative_topic is not None:
                    topic.relative_topic.reviews[branch].children.append(review)

                    review.remote_base = topic.relative_topic.reviews[branch].remote_head
                    # Base ref is empty since it doesn't exist until create_commits()
                else:
                    if relative_branch == self.relative_branch:
                        review.base_ref = self.commits[0].parents[0]
                    else:
                        review.base_ref = await self.git_ctx.to_commit_hash(relative_branch)
                    review.remote_base = self.git_ctx.remove_branch_prefix(relative_branch)

                review.remote_head = format_remote_branch(topic_uploader, base_branch, name)

                topic.reviews[branch] = review

                review.is_draft = "draft" in topic.tags[TAG_LABEL]

            # Don't add draft as a label since its instead used to mark a pr as a draft
            topic.tags[TAG_LABEL].discard("draft")

            if auto_add_users in ("r2a", "both"):
                topic.tags[TAG_ASSIGNEE].update(topic.tags[TAG_REVIEWER])
            if auto_add_users in ("a2r", "both"):
                topic.tags[TAG_REVIEWER].update(topic.tags[TAG_ASSIGNEE])

            seen_topics[name] = topic

    async def mark_rebases(self, skip_rebase: bool) -> None:
        """
        Scan all topics and compare patch-ids to remote patch-ids. Appropriately mark any
        changes that are already merged, or where push can be skipped due to being rebases or
        being identical.
        """
        for _, topic, base_branch, review in self.all_reviews_iter():
            # If the relative branch already merged, reset the remote base directly to the base
            # branch.
            if review.relative_branch:
                relative_br = self.git_ctx.remove_branch_prefix(review.relative_branch)
                if relative_br not in self.relative_infos:
                    # If we wanted to be stricter we could enforce that a PR must exist for the
                    # relative branch, but for now we don't. The side effect is that we could
                    # get an error upon PR creation if the branch doesn't exist.
                    logging.warning(f"Failed to look up relative PR for branch {relative_br}")
                elif self.relative_infos[relative_br].state == "MERGED":
                    review.relative_branch = ""

                    if topic.relative_topic is None:
                        # Only the first review in a chain needs to be reset
                        if base_branch == self.base_branch:
                            review.base_ref = self.commits[0].parents[0]
                        else:
                            review.base_ref = await self.git_ctx.to_commit_hash(base_branch)
                        review.remote_base = self.git_ctx.remove_branch_prefix(base_branch)

            # If the relative topic was already merged, reset to the base branch.
            # We know at this point that any relative branch would have already merged.
            if (
                topic.relative_topic is not None
                and topic.relative_topic.reviews[base_branch].status == PrStatus.MERGED
            ):
                review.remote_base = self.git_ctx.remove_branch_prefix(base_branch)

            # At this point we should have resolved the correct base branch. If the review
            # was actually merged into a different branch, warn and try to create it again.
            if (
                review.status == PrStatus.MERGED
                and review.pr_info is not None
                and review.remote_base != review.pr_info.baseRef
            ):
                logging.warning(
                    f"Branch {review.remote_head} was merged into {review.pr_info.baseRef}"
                    "instead of {review.remote_base} as expected!"
                )
                # NOTE: This may not iteract well with the check at the end of create_commits
                # but they are both corner cases and the worst that could happen is we fail to
                # recreate the pr (but warn anyway).
                review.status = PrStatus.NEW

            if review.pr_info is None:
                # This is a new pr, no need to check patch ids
                review.is_pure_rebase = False
            else:
                if not topic.patch_ids:
                    # Lazily load patch ids for the topic.
                    # TODO async gather to generate patch ids in parallel
                    topic.patch_ids = [
                        await self.git_ctx.get_patch_id(c.commit_id) for c in topic.original_commits
                    ]

                review.remote_commits = git.parse_rev_list(
                    await self.git_ctx.rev_list(
                        review.pr_info.headRefOid,
                        review.pr_info.baseRefOid,
                        header=True,
                        first_parent=True,
                    )
                )

                # TODO async gather to generate patch ids in parallel
                review.remote_patch_ids = [
                    await self.git_ctx.get_patch_id(c.commit_id) for c in review.remote_commits
                ]

                # This review is a rebase iff all commit diffs match
                is_rebase = len(review.remote_commits) == len(topic.original_commits) and all(
                    local_id == remote_id
                    for local_id, remote_id in zip(topic.patch_ids, review.remote_patch_ids)
                )
                # This review is a "complete rebase" iff all commit diffs and metadata match
                review.is_pure_rebase = is_rebase and all(
                    git.commits_match(local_commit, remote_commit)
                    for local_commit, remote_commit in zip(
                        topic.original_commits, review.remote_commits
                    )
                )
                logging.debug(
                    "Review {}/{} is rebase {} pure {}".format(
                        base_branch, topic.name, is_rebase, review.is_pure_rebase
                    )
                )

                if is_rebase and not review.is_pure_rebase:
                    if review.status == PrStatus.MERGED:
                        # Commit messages changed but there is no way to update because the PR
                        # has already merged. User will lose these changes on next pull, so
                        # warn them here.
                        logging.warning(
                            f"Review for {topic.name} was reworded but has already been merged"
                        )
                        review.is_pure_rebase = True
                    else:
                        # TODO: We can do more optimization by reusing the remote trees.
                        # We wouldn't benefit as much from skipping the push, but we'd save time
                        # on creating commits.
                        pass

            if review.is_pure_rebase and review.pr_info is not None:
                # For a relative series of reviews, revup will only ever upload them directly
                # on top of each other. If this relationship is ever broken, we always reupload
                # This ensures predictable and consistent CI behavior between the branches.
                is_on_top_of_relative = (
                    topic.relative_topic is None
                    or topic.relative_topic.reviews[base_branch].pr_info is None
                    or review.remote_commits[0].parents[0]
                    == topic.relative_topic.reviews[base_branch].remote_commits[-1].commit_id
                )

                relative_topic_is_nochange = (
                    topic.relative_topic is not None
                    and topic.relative_topic.reviews[base_branch].push_status == PushStatus.NOCHANGE
                )
                relative_topic_is_skippable = (
                    topic.relative_topic is None
                    or topic.relative_topic.reviews[base_branch].push_status != PushStatus.PUSHED
                )

                if review.base_ref == review.remote_commits[0].parents[0] or (
                    relative_topic_is_nochange and is_on_top_of_relative
                ):
                    # This is a rebase and the parent is the same, so there must be no change.
                    # Alternatively, it is relative to a topic with no change.
                    review.push_status = PushStatus.NOCHANGE
                elif review.status == PrStatus.MERGED or (
                    skip_rebase and is_on_top_of_relative and relative_topic_is_skippable
                ):
                    # Never push merged changes.
                    # Also don't push if the user asked to skip pushing rebases, but only if the
                    # relative base is correct and relative topic won't be pushed.
                    review.push_status = PushStatus.REBASE

                if review.push_status == PushStatus.NOCHANGE:
                    # If there was no change, we copy the remote commit ids so that future
                    # topics can cherry-pick on that point. We don't have to do this in any
                    # other case, since they'll be made if status is PUSHED, and they'll either
                    # be skipped or marked as push if status is REBASE.
                    review.new_commits = [c.commit_id for c in review.remote_commits]
            else:
                if review.status == PrStatus.MERGED:
                    # This PR was "merged" but isn't a rebase, meaning there is actually new
                    # content that should be in a new PR.
                    review.status = PrStatus.NEW

            if review.push_status == PushStatus.PUSHED:
                # If this change must be pushed, then all changes it depends on cannot be
                # skipped due to rebase (although can be due to nochange), otherwise github
                # will show the wrong commit diff between the two reviews
                cur_topic = topic.relative_topic
                while cur_topic is not None:
                    cur_review = cur_topic.reviews[base_branch]
                    if cur_review.push_status == PushStatus.REBASE:
                        cur_review.push_status = PushStatus.PUSHED
                        if cur_review.status == PrStatus.MERGED:
                            # User has changed the base of an already merged commit, but hasn't
                            # moved forward enough such that the commit would be dropped. There
                            # isn't any way for us to handle this that wouldn't potentially
                            # generate a conflict or show the incorrect commit diff. We settle
                            # with showing the wrong diff and warning the user.
                            # This should be relatively uncommon
                            logging.warning(
                                f"Attempted to rebase an already merged PR {cur_topic.name}"
                            )
                            logging.warning("'git pull' and upload again to fix this.")

                        cur_topic = cur_topic.relative_topic
                    else:
                        break

    async def create_commits(self, trim_tags: bool) -> None:
        """
        Populate new_commits for all reviews by cherry-picking to the base ref if necessary.
        """
        for name, topic, base_branch, review in self.all_reviews_iter():
            if review.push_status != PushStatus.PUSHED:
                # Don't need to create branches if we're not pushing them.
                continue

            if topic.relative_topic is not None:
                if not topic.relative_topic.reviews[base_branch].new_commits:
                    raise RuntimeError(
                        f"Bug! Relative topic {topic.relative_topic.name} is missing commits "
                        f"(status {topic.relative_topic.reviews[base_branch].push_status})"
                    )
                # The base ref for this topic is the last commit in the relative topic
                review.base_ref = topic.relative_topic.reviews[base_branch].new_commits[-1]

            if not review.base_ref:
                raise RuntimeError("Bug! review doesn't have a base ref")

            next_parent = review.base_ref
            for commit in topic.original_commits:
                if commit.parents[0] == next_parent and not trim_tags:
                    # If the intended parent is the same as the actual parent, skip the
                    # cherry-pick process (unless the commit msg needs to change).
                    review.new_commits.append(commit.commit_id)
                    next_parent = commit.commit_id
                else:
                    # TODO: Potential optimization here: if remote_base_oid and base_ref are
                    # the same, we can use trees to pick the first N commits where patch-id
                    # is equal to remote patch-id
                    # TODO: We can parallelize independent chains of commit creation
                    try:
                        next_parent = await self.git_ctx.synthetic_cherry_pick_from_commit(
                            commit, next_parent
                        )
                    except GitConflictException as exc:
                        parent_info = (
                            "the same topic"
                            if next_parent != review.base_ref
                            else f'relative topic "{topic.relative_topic.name}"'
                            if topic.relative_topic
                            else f'base branch "{base_branch}"'
                        )
                        raise RevupConflictException(
                            "Failed to cherry-pick commit:\n"
                            f'"{commit.title}" ({commit.commit_id[:8]}) in topic "{name}"\n'
                            f"to new parent ({next_parent[:8]}) in {parent_info}\n\n"
                            "You must specify relative branches to prevent this conflict!"
                        ) from exc
                    review.new_commits.append(next_parent)

            if review.pr_info is not None and review.pr_info.headRefOid == review.new_commits[-1]:
                # There are a few cases where we might not know a review is no change until after
                # creating commits:
                # 1. The relative PR was closed without merging after being uploaded. We don't look
                # at closed PRs, so we don't know whether that branch has changed. However actually
                # building both branches could reveal that the resulting commit is the same.
                # 2. A commit might not match the remote based on patch id, but applying the patch
                # would result in the same commit as the remote. This could happen if a part of the
                # patch is dependent on a local commit, but is a no-op when applied to the base.
                review.push_status = PushStatus.NOCHANGE
                if review.status == PrStatus.NEW:
                    # A PR marked as new but with a pr_info must have previously been merged, but
                    # marked as new when checking rebases. Return it to merged.
                    review.status = PrStatus.MERGED
                # TODO: If 2 above were true *and* a rebase occurred this wouldn't catch it and
                # an erroneous push / pr creation would happen. We'd have to compute patch ids again
                # to catch this which is a bit inefficient for all reviews. Lets see how common this
                # is / try to think of alternative solutions.

    async def fetch_git_refs(self) -> None:
        """
        Fetch base and head refs so later logic can properly handle rebases and merged changes.
        These would normally exist locally, but might not due to running a git gc, or user
        switching to a different machine.
        """
        if not self.github_ep or not self.repo_info:
            raise RuntimeError("Can't fetch without github info")

        to_fetch = set()
        for _, _, _, review in self.all_reviews_iter():
            if review.pr_info is not None and not await self.git_ctx.commit_exists(
                review.pr_info.headRefOid
            ):
                to_fetch.add(review.pr_info.headRefOid)

        if to_fetch:
            fetch_args = [
                "fetch",
                "--no-write-fetch-head",
                "--no-auto-maintenance",
                "--quiet" if self.git_ctx.sh.quiet else "--verbose",
                self.git_ctx.remote_name,
                *to_fetch,
            ]
            await self.git_ctx.git(*fetch_args)

    async def push_git_refs(self, uploader: str, create_local_branches: bool) -> None:
        """
        Push all refs to their branch on the remote.
        """
        if not self.github_ep or not self.repo_info:
            raise RuntimeError("Can't push without github info")

        push_targets = []
        for _, _, _, review in self.all_reviews_iter():
            if review.push_status != PushStatus.PUSHED or review.status == PrStatus.MERGED:
                continue

            push_targets.append(f"{review.new_commits[-1]}:refs/heads/{review.remote_head}")

            if create_local_branches:
                await self.git_ctx.git(
                    "update-ref",
                    "-m",
                    "revup: update local branch",
                    review.remote_head,
                    review.new_commits[-1],
                )

        if self.last_virtual_diff_target is not None:
            virtual_diff_branch = f"{uploader}/revup/virtual_diff_targets"
            push_targets.append(f"{self.last_virtual_diff_target}:refs/heads/{virtual_diff_branch}")

        # It's much faster to push all refs in one command
        if push_targets:
            push_args = [
                "push",
                "--force",
                "--no-verify",
                "--atomic",
                "--quiet" if self.git_ctx.sh.quiet else "--verbose",
                self.git_ctx.remote_name,
                *push_targets,
            ]
            await self.git_ctx.git(*push_args, stderr=subprocess.PIPE)

    async def query_github(self) -> None:
        """
        Query pr and reviewer/label info from github
        """
        if not self.github_ep or not self.repo_info:
            raise RuntimeError("Can't query without github info")

        pr_targets = []
        user_ids = set()
        labels = set()
        for _, topic, base_branch, review in self.all_reviews_iter():
            pr_targets.append(review.remote_head)
            user_ids |= topic.tags[TAG_REVIEWER]
            user_ids |= topic.tags[TAG_ASSIGNEE]
            labels |= topic.tags[TAG_LABEL]
            labels.add(self.git_ctx.remove_branch_prefix(base_branch))

        relative_targets = set()
        # Add queries for relative branches at the end
        for _, topic, _, review in self.all_reviews_iter():
            if review.relative_branch:
                relative_targets.add(self.git_ctx.remove_branch_prefix(review.relative_branch))
        pr_targets.extend(relative_targets)

        # Queries currently cannot be mixed with mutations. However we can save
        # time by querying everything we need in one call.
        (
            self.repo_id,
            prs,
            self.names_to_ids,
            self.names_to_logins,
            self.labels_to_ids,
        ) = await github_utils.query_everything(
            self.github_ep, self.repo_info, pr_targets, list(user_ids), list(labels)
        )

        i = 0
        for _, _, _, review in self.all_reviews_iter():
            review.pr_info = prs[i]
            if review.pr_info is None:
                review.status = PrStatus.NEW
            elif review.pr_info.state == "MERGED":
                review.status = PrStatus.MERGED
            i += 1

        while i < len(pr_targets):
            pr_info = prs[i]
            if pr_info is not None:
                self.relative_infos[pr_targets[i]] = pr_info
            i += 1

    def populate_update_info(
        self,
        update_pr_body: bool,
    ) -> None:
        """
        Populate information necessary to do PR creation / update in github.
        """
        if (
            not self.repo_id
            or self.names_to_ids is None
            or self.names_to_logins is None
            or self.labels_to_ids is None
        ):
            raise RuntimeError("Need to query before updating")

        for topic in self.topics.values():
            commit_msg_lines = topic.original_commits[0].commit_msg.split("\n")
            body = "\n".join(commit_msg_lines[1:]).strip()
            title = commit_msg_lines[0]
            for branch, review in topic.reviews.items():
                if review.status == PrStatus.NEW:
                    if not review.base_ref:
                        raise RuntimeError(
                            f"Bug! review {review.topic.name} {review.remote_base} doesn't have a"
                            " base ref"
                        )
                    review.pr_info = PrInfo(
                        baseRef=review.remote_base,
                        baseRefOid=review.base_ref,
                        headRef=review.remote_head,
                        headRefOid=review.new_commits[-1],
                        body=body,
                        title=title,
                        is_draft=review.is_draft,
                    )

                if not review.pr_info or review.status == PrStatus.MERGED:
                    continue

                for i in range(github_utils.MAX_COMMENTS_TO_QUERY):
                    # Match comment indexes for various features (they will be populated later)
                    if i >= len(review.pr_info.comments):
                        if review.review_graph_index is None:
                            review.review_graph_index = i
                        elif review.patchsets_index is None:
                            review.patchsets_index = i
                    elif review.pr_info.comments[i].text.startswith(REVIEW_GRAPH_FIRST_LINE):
                        review.review_graph_index = i
                    elif review.pr_info.comments[i].text.startswith(PATCHSETS_FIRST_LINE):
                        review.patchsets_index = i

                labels = set(topic.tags[TAG_LABEL])
                base_branch_name = self.git_ctx.remove_branch_prefix(branch)
                if base_branch_name in self.labels_to_ids:
                    # Add the base branch name as a tag which can show all changes on that branch
                    labels.add(base_branch_name)

                label_ids = translate_if_exists(labels, self.labels_to_ids).difference(
                    review.pr_info.label_ids
                )
                valid_labels = set(label for label in labels if label in self.labels_to_ids)

                # Don't request reviewers that are already added, otherwise the request will clear
                # the "reviewed" status in the UI.
                reviewer_ids = translate_if_exists(
                    topic.tags[TAG_REVIEWER], self.names_to_ids
                ).difference(review.pr_info.reviewer_ids)
                reviewer_logins = translate_if_exists(
                    topic.tags[TAG_REVIEWER], self.names_to_logins
                ).difference(review.pr_info.reviewers)

                assignee_ids = translate_if_exists(
                    topic.tags[TAG_ASSIGNEE], self.names_to_ids
                ).difference(review.pr_info.assignee_ids)
                assignee_logins = translate_if_exists(
                    topic.tags[TAG_ASSIGNEE], self.names_to_logins
                ).difference(review.pr_info.assignees)

                if review.pr_info.baseRef != review.remote_base:
                    review.pr_update.baseRef = review.remote_base
                if update_pr_body and review.pr_info.body != body:
                    review.pr_update.body = body
                if update_pr_body and review.pr_info.title != title:
                    review.pr_update.title = title
                if review.pr_info.is_draft != review.is_draft:
                    review.pr_update.is_draft = review.is_draft
                review.pr_update.label_ids = label_ids
                review.pr_update.reviewer_ids = reviewer_ids
                review.pr_update.assignee_ids = assignee_ids

                review.pr_info.reviewers |= reviewer_logins
                review.pr_info.assignees |= assignee_logins
                review.pr_info.labels |= valid_labels

    def create_review_graph(self) -> Dict[str, List[str]]:
        """
        Return a dict of remote branch names to a string containing a graph-formatted
        representation of the entire relative review structure in that chain.
        """
        ret: Dict[str, List[str]] = {}

        def graph_helper(review: Review, back: str, prefix: str) -> int:
            if review.pr_info is None:
                return 0
            review_title = review.pr_update.title or review.pr_info.title
            num_nodes = 1
            ret[review.remote_head][0] += f"{back}{prefix}{review.pr_info.url} {review_title}\n"
            for i, child in enumerate(review.children):
                ret[child.remote_head] = ret[review.remote_head]
                num_nodes += graph_helper(
                    child,
                    back + ("\u3000" if prefix == "└" else "│"),
                    ("└" if i == len(review.children) - 1 else "├"),
                )
            return num_nodes

        for _, topic, _, review in self.all_reviews_iter():
            if topic.relative_topic is None:
                # Uses a single element list so all members of the chain reference the same string.
                ret[review.remote_head] = [""]
                graph_helper(review, "", "└")

        return ret

    def populate_review_graph(self) -> None:
        # Create the review graph after populating PrInfos for new topics
        review_graph = self.create_review_graph()
        for _, _, _, review in self.all_reviews_iter():
            if (
                review.review_graph_index is None
                or not review.pr_info
                or review.status == PrStatus.MERGED
            ):
                continue
            review_title = review.pr_update.title or review.pr_info.title
            review_graph_text = REVIEW_GRAPH_FIRST_LINE + (
                review_graph[review.remote_head][0]
                .replace(f"{review.pr_info.url}", f"**{review.pr_info.url}**")
                .replace(f"{review_title}", f"**{review_title}**")
            )
            if len(review.pr_info.comments) > review.review_graph_index:
                if review_graph_text != review.pr_info.comments[review.review_graph_index].text:
                    # edit existing comment
                    review.pr_update.comments.append(
                        PrComment(
                            review_graph_text,
                            review.pr_info.comments[review.review_graph_index].id,
                        )
                    )
            else:
                # Try to make review graph the first comment
                review.pr_update.comments.insert(0, PrComment(review_graph_text, None))

    async def populate_patchsets(self) -> None:
        for _, _, _, review in self.all_reviews_iter():
            if (
                review.patchsets_index is None
                or not review.pr_info
                or review.status == PrStatus.MERGED
            ):
                continue
            patchsets_comment = await self.create_patchsets_comment(
                review,
                review.pr_info.comments[review.patchsets_index]
                if len(review.pr_info.comments) > review.patchsets_index
                else None,
            )
            if patchsets_comment:
                review.pr_update.comments.append(patchsets_comment)

    async def create_prs(self) -> None:
        """
        Actually perform the github graphql PR creation
        """
        if not self.github_ep or not self.repo_info or not self.fork_info or not self.repo_id:
            raise RuntimeError("Can't update without github info")

        prs_to_create = []
        for _, _, _, review in self.all_reviews_iter():
            if review.status == PrStatus.NEW and review.pr_info is not None:
                prs_to_create.append(review.pr_info)

        if prs_to_create:
            # Create all prs in one request. These will most likely end up being modified
            # later since its not possible to add labels or reviewers at creation time.
            await github_utils.create_pull_requests(
                self.github_ep, self.repo_id, self.repo_info, self.fork_info, prs_to_create
            )

    async def update_prs(self) -> None:
        """
        Actually perform the github graphql PR updates
        """
        if not self.github_ep or not self.repo_info or not self.fork_info or not self.repo_id:
            raise RuntimeError("Can't update without github info")

        prs_to_update = []
        for _, _, _, review in self.all_reviews_iter():
            if not review.pr_info:
                continue
            if (
                review.pr_update.baseRef is not None
                or review.pr_update.body is not None
                or review.pr_update.title is not None
                or review.pr_update.reviewer_ids
                or review.pr_update.assignee_ids
                or review.pr_update.label_ids
                or review.pr_update.is_draft is not None
                or review.pr_update.comments
            ):
                review.pr_update.id = review.pr_info.id
                prs_to_update.append(review.pr_update)
                if review.status != PrStatus.NEW:
                    review.status = PrStatus.UPDATED

        if prs_to_update:
            await github_utils.update_pull_requests(self.github_ep, prs_to_update)

    async def restack(self, topicless_last: bool) -> GitCommitHash:
        """
        Create a new commit chain consisting of current commits but with commits
        in a single topic consolidated together.
        """
        to_pick = []
        for topic in self.topics.values():
            this_topic = []
            topic_is_empty = True
            for commit in topic.original_commits:
                this_topic.append(commit)
                if not await self.git_ctx.have_identical_trees(commit.commit_id, commit.parents[0]):
                    topic_is_empty = False
            # Drop empty topics, ie topics with all empty commits. git pull --rebase
            # doesn't automatically drop empty commits if they're been merged.
            if not topic_is_empty:
                to_pick.extend(this_topic)
        no_topic = []
        for commit in self.commits:
            if commit not in to_pick and not await self.git_ctx.have_identical_trees(
                commit.commit_id, commit.parents[0]
            ):
                no_topic.append(commit)

        new_parent = self.commits[0].parents[0]
        if topicless_last:
            to_restack = to_pick + no_topic
        else:
            to_restack = no_topic + to_pick
        for commit in to_restack:
            try:
                new_parent = await self.git_ctx.synthetic_cherry_pick_from_commit(
                    commit, new_parent
                )
            except GitConflictException as exc:
                raise RevupConflictException(
                    "Failed to cherry-pick commit:\n"
                    f'"{commit.title}" ({commit.commit_id[:8]})\n'
                    f"to new parent ({new_parent[:8]})\n\n"
                    f"You may need to `git rebase -i {new_parent[:8]}` to resolve these conflicts!"
                ) from exc
        git_env = {
            "GIT_REFLOG_ACTION": "reset --soft (revup restack)",
        }
        await self.git_ctx.git("reset", "--soft", new_parent, env=git_env)
        return new_parent

    def num_reviews_changed(self) -> int:
        """
        Return the number of reviews that require some action (push / create / update).
        This is similar to the logic that hides reviews in print() but different in some cases,
        for example we take no action for merged prs but still print them out.
        """
        ret = 0
        for _, topic in reversed(self.topics.items()):
            for _, review in topic.reviews.items():
                if (
                    review.status in (PrStatus.NOCHANGE, PrStatus.MERGED)
                    and review.push_status != PushStatus.PUSHED
                ):
                    continue
                ret += 1
        return ret

    def print(self, skip_empty: bool) -> None:
        """
        Output a formatted version of whatever fields are currently populated.
        """
        if skip_empty and self.num_reviews_changed() == 0:
            get_console().print("Nothing to upload! :rocket:")
            return

        for name, topic in reversed(self.topics.items()):
            for base, review in topic.reviews.items():
                if (
                    skip_empty
                    and review.status == PrStatus.NOCHANGE
                    and review.push_status != PushStatus.PUSHED
                ):
                    continue

                get_console().print("")

                maybe_relative_topic = ""
                if topic.relative_topic is not None:
                    maybe_ellipses = (
                        "… → " if topic.relative_topic.relative_topic is not None else ""
                    )
                    maybe_relative_topic = (
                        f"[bold yellow]{topic.relative_topic.name}[/] → {maybe_ellipses}"
                    )
                maybe_relative_branch = ""
                if review.relative_branch:
                    maybe_relative_branch = f"[bold magenta]{review.relative_branch}[/] → "
                maybe_draft = " (draft)" if review.is_draft else ""

                get_console().print(
                    f"[green]Topic:[/] [bold cyan]{name}[/]{maybe_draft} → "
                    f"{maybe_relative_topic}{maybe_relative_branch}[bold red]{base}[/]"
                )
                logging.debug(f"Base rev: {review.base_ref}")
                if review.new_commits:
                    logging.debug(f"New head: {review.new_commits[-1]}")

                reviewers = topic.tags[TAG_REVIEWER]
                assignees = topic.tags[TAG_ASSIGNEE]
                labels = topic.tags[TAG_LABEL]
                if review.pr_info:
                    reviewers = review.pr_info.reviewers
                    assignees = review.pr_info.assignees
                    labels = review.pr_info.labels
                if reviewers:
                    get_console().print(f"[green]Reviewers:[/] {', '.join(reviewers)}")
                if assignees:
                    get_console().print(f"[green]Assignees:[/] {', '.join(assignees)}")
                if labels:
                    get_console().print(f"[green]Labels:[/] {', '.join(labels)}")
                get_console().print("[green]Commits:[/]")
                for i, commit in enumerate(topic.original_commits):
                    title = commit.commit_msg.split("\n")[0]
                    if i == 0:
                        # Highlight the PR title
                        get_console().print(f"  [bold green]{escape(title)}[/]")
                    else:
                        get_console().print(f"  {escape(title)}")
                if review.pr_info:
                    status_str = f"({review.status.value})"
                    if review.push_status != PushStatus.NOCHANGE:
                        # Push status is redundant if there's no change.
                        status_str += f" ({review.push_status.value})"
                    get_console().print("[green]Github URL:[/]")
                    get_console().print(f"  [underline]{review.pr_info.url}[/] {status_str}")
