import argparse
import logging
import subprocess
from typing import Optional

from rich import get_console

from revup import git, github, github_utils, topic_stack
from revup.types import GitConflictException, RevupShellException


async def main(
    args: argparse.Namespace,
    git_ctx: git.Git,
    github_ep: Optional[github.GitHubEndpoint] = None,
    repo_info: Optional[github_utils.GitHubRepoInfo] = None,
    fork_info: Optional[github_utils.GitHubRepoInfo] = None,
) -> int:
    """
    Handles the "upload" command.
    """
    topics = topic_stack.TopicStack(
        git_ctx,
        args.base_branch,
        args.relative_branch,
        github_ep,
        repo_info,
        fork_info,
    )
    with get_console().status("Finding topics…"):
        await topics.populate_topics(
            auto_topic=args.auto_topic,
            trim_tags=args.trim_tags,
        )
        await topics.populate_reviews(
            args.uploader if args.uploader else git_ctx.author,
            force_relative_chain=args.relative_chain,
            labels=args.labels,
            user_aliases=args.user_aliases,
            auto_add_users=args.auto_add_users,
            self_authored_only=args.self_authored_only,
        )

    if not args.dry_run:
        with get_console().status("Querying github…"):
            await topics.query_github()
            # Fetch uses the oid results from the query
            await topics.fetch_git_refs()

            # Rebase detection uses object results from query / fetch
            await topics.mark_rebases(not args.rebase)

    if args.status or args.verbose:
        topics.print(skip_empty=False)

    if args.status:
        return 0

    try:
        with get_console().status("Creating commits…"):
            # Need to know rebase information before creating commits
            await topics.create_commits(args.trim_tags)
    except GitConflictException as e:
        logging.error(str(e))
        logging.error("You need to specify relative topics to prevent this conflict.")
        return 1

    if args.dry_run:
        topics.print(not args.verbose)
        return 0

    topics.populate_update_info(args.update_pr_body)
    if not args.skip_confirm and topics.num_reviews_changed() > 0:
        topics.print(not args.verbose)
        if git_ctx.sh.wait_for_confirmation():
            return 1

    if args.pre_upload:
        # Wait until we're sure there aren't any conflicts before running pre upload command
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

    with get_console().status("Pushing remote branches…"):
        if args.patchsets:
            # Patchsets require completed commit ids
            await topics.populate_patchsets()
        # Must push refs after creating them. Includes the virtual diff branch for patchsets.
        await topics.push_git_refs(git_ctx.author, args.create_local_branches)

    try:
        # Must create PRs after refs are pushed, and must update PRs after creating them.
        with get_console().status("Updating github PRs…"):
            await topics.create_prs()
            if args.review_graph:
                # Review graph requires populated PR urls from creation
                topics.populate_review_graph()
            await topics.update_prs()
    finally:
        topics.print(not args.verbose)
    return 0
