import argparse
import enum
import subprocess
from typing import AsyncGenerator, Tuple

from rich import get_console

from revup import git, topic_stack
from revup.forge import Forge
from revup.types import RevupShellException


class UploadPhase(enum.Enum):
    POPULATED = "populated"
    QUERIED = "queried"
    COMMITS_CREATED = "commits_created"
    READY_TO_PUSH = "ready_to_push"
    PUSHED = "pushed"
    PRS_UPDATED = "prs_updated"


async def main(
    args: argparse.Namespace,
    git_ctx: git.Git,
    forge: Forge,
) -> int:
    async for _ in run(args, git_ctx, forge):
        pass
    return 0


async def run(
    args: argparse.Namespace,
    git_ctx: git.Git,
    forge: Forge,
    skip_push: bool = False,
) -> AsyncGenerator[Tuple[UploadPhase, topic_stack.TopicStack], None]:
    """
    Core upload logic as an async generator yielding (phase, topics) at each stage.
    """
    topics = topic_stack.TopicStack(
        git_ctx,
        args.base_branch,
        args.relative_branch,
        forge,
        args.head,
    )
    with get_console().status("Finding topics…"):
        await topics.populate_topics(
            auto_topic=args.auto_topic,
            trim_tags=args.trim_tags,
            raise_on_invalid=True,
        )
        await topics.populate_reviews(
            force_relative_chain=args.relative_chain,
            labels=args.labels,
            user_aliases=args.user_aliases,
            auto_add_users=args.auto_add_users,
            self_authored_only=args.self_authored_only,
            limit_topics=args.topics,
        )
        await topics.populate_relative_reviews(
            args.uploader if args.uploader else git_ctx.author,
            branch_format=args.branch_format,
        )

    yield UploadPhase.POPULATED, topics

    if not args.dry_run and not args.push_only:
        with get_console().status(f"Querying {forge.name}…"):
            await topics.query()
            await topics.fetch_git_refs()
            await topics.mark_rebases(not args.rebase)

    yield UploadPhase.QUERIED, topics

    if args.status or args.verbose:
        topics.print(skip_empty=False)

    if args.status:
        return

    with get_console().status("Creating commits…"):
        await topics.create_commits(args.trim_tags, args.skip_empty_first_commit)

    yield UploadPhase.COMMITS_CREATED, topics

    if args.dry_run:
        topics.print(not args.verbose)
        return

    if not args.push_only:
        topics.populate_update_info(args.update_pr_body, args.force_reviewers, args.pr_body_source)
    if not args.skip_confirm and topics.num_reviews_changed() > 0:
        topics.print(not args.verbose)
        if git_ctx.sh.wait_for_confirmation():
            return

    if args.pre_upload:
        with get_console().status("Running pre-upload command"):
            result = subprocess.run(
                args.pre_upload,
                shell=True,
                cwd=git_ctx.sh.cwd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
            )
            if result.returncode != 0:
                raise RevupShellException(f"Pre-upload command failed:\n{result.stdout}")

    yield UploadPhase.READY_TO_PUSH, topics

    if not skip_push:
        with get_console().status("Pushing remote branches…"):
            if args.patchsets:
                await topics.populate_patchsets()
            if not args.push_only:
                await topics.retarget_orphaned_prs()
            await topics.push_git_refs(git_ctx.author, args.create_local_branches)

    yield UploadPhase.PUSHED, topics

    if args.push_only:
        topics.print(not args.verbose)
        return

    try:
        with get_console().status(f"Updating {forge.name} PRs…"):
            await topics.create_prs()
            if args.review_graph:
                topics.populate_review_graph()
            await topics.update_prs()

        if not skip_push and topics.use_reordering_workaround:
            topics.use_reordering_workaround = False
            with get_console().status("Pushing again to work around reordering issues…"):
                await topics.push_git_refs(git_ctx.author, create_local_branches=False)
    finally:
        topics.print(not args.verbose)

    yield UploadPhase.PRS_UPDATED, topics
