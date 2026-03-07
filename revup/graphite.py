"""
Graphite integration for revup.

This module handles all Graphite-specific operations including:
- Pre-upload rebase to drop already-merged commits
- Tracking branches with gt track
- Submitting stacks to Graphite
"""

import argparse
import logging
import subprocess
from typing import List

from revup import git, topic_stack
from revup.topic_stack import PrStatus


async def pre_upload_rebase(
    git_ctx: git.Git,
    args: argparse.Namespace,
) -> None:
    """
    Fetch and rebase to drop commits already merged by Graphite.
    Graphite closes/deletes branches instead of merging, so we need to
    rebase to clean up local history.
    """
    logging.info("Fetching and rebasing to drop already-merged commits")
    await git_ctx.git("fetch", git_ctx.remote_name)
    base = args.base_branch if args.base_branch else git_ctx.main_branch
    await git_ctx.git("rebase", f"{git_ctx.remote_name}/{base}", raiseonerror=False)


async def run_gt_track(
    git_ctx: git.Git,
    topics: topic_stack.TopicStack,
) -> None:
    """
    Run 'gt track' on all branches in the stack in topological order.

    This allows Graphite to track the uploaded branches and understand the stack structure.
    Parents are tracked before children so Graphite can build the relationships.
    """
    # Collect ALL branches in topological order (parents first) with their parent info
    # Format: (branch_name, parent_branch_name)
    all_branches: List[tuple] = []
    for _, topic, base_branch, review in topics.all_reviews_iter():
        if review.status != PrStatus.MERGED:
            # Determine the parent branch for Graphite
            if topic.relative_topic is not None:
                # Parent is the relative topic's branch
                parent_branch = topic.relative_topic.reviews[base_branch].remote_head
            else:
                # Parent is the base branch (e.g., "main")
                parent_branch = git_ctx.remove_branch_prefix(base_branch)

            all_branches.append((review.remote_head, parent_branch))
            logging.debug(f"Will track branch: {review.remote_head} with parent: {parent_branch}")

    if not all_branches:
        return

    # Save current HEAD to restore later
    original_head = (await git_ctx.git("rev-parse", "HEAD"))[1].strip()
    original_branch = None
    ret, branch = await git_ctx.git("symbolic-ref", "--short", "HEAD", raiseonerror=False)
    if ret == 0:
        original_branch = branch.strip()

    try:
        for branch_name, parent_branch in all_branches:
            logging.info(f"Running gt track {branch_name} --parent {parent_branch}")
            # Create/reset local branch pointing to remote
            await git_ctx.git(
                "checkout",
                "-B",
                branch_name,
                f"{git_ctx.remote_name}/{branch_name}",
                raiseonerror=False,
            )
            # Use 'gt track' with --parent to specify the parent branch
            result = subprocess.run(
                ["gt", "track", branch_name, "--parent", parent_branch, "--force"],
                cwd=git_ctx.sh.cwd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
            )
            if result.returncode != 0:
                logging.warning(f"gt track failed for {branch_name}: {result.stdout}")
            elif result.stdout.strip():
                logging.info(f"gt track output: {result.stdout.strip()}")

        # Submit the stack to Graphite
        logging.info("Submitting stack to Graphite")
        subprocess.run(
            ["gt", "checkout", "main"],
            cwd=git_ctx.sh.cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        result = subprocess.run(
            ["gt", "submit", "-s"],
            cwd=git_ctx.sh.cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
        )
        if result.returncode != 0:
            logging.warning(f"gt submit failed: {result.stdout}")
        elif result.stdout.strip():
            logging.info(f"gt submit output: {result.stdout.strip()}")
    finally:
        # Return to original HEAD/branch
        if original_branch:
            await git_ctx.git("checkout", original_branch)
        else:
            await git_ctx.git("checkout", "--detach", original_head)
