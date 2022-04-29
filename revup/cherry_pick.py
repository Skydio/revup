import argparse
import asyncio
import logging

from revup import git
from revup.types import GitCommitHash, RevupUsageException


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Squash the given branch's changes into a single commit, and cherry-pick
    that commit onto the local branch.
    """
    branch_to_pick = args.branch[0]
    remote_branch_to_pick = git_ctx.ensure_branch_prefix(branch_to_pick)
    branch_exists, remote_branch_exists = await asyncio.gather(
        git_ctx.commit_exists(branch_to_pick), git_ctx.commit_exists(remote_branch_to_pick)
    )
    if remote_branch_exists and not branch_exists:
        logging.warning(
            f"Couldn't find '{branch_to_pick}', assuming you meant '{remote_branch_to_pick}'"
        )
        branch_to_pick = remote_branch_to_pick
    elif not branch_exists:
        raise RevupUsageException(f"Couldn't find ref '{branch_to_pick}'")

    if args.base_branch:
        base_branch = args.base_branch
        await git_ctx.verify_branch_or_commit(base_branch)
    else:
        base_branch = await git_ctx.get_best_base_branch(branch_to_pick, True)

    # This is the most recent version of the base branch that has been merged in
    parent = (
        await git_ctx.sh.sh(
            git_ctx.git_path,
            "rev-list",
            "--first-parent",
            base_branch,
            "^" + branch_to_pick,
            "--reverse",
        )
    )[1].split("\n")[0]
    if parent:
        # Most recent version of the base branch is the parent of the last reachable commit.
        parent = parent + "~"
    else:
        # Base branch has not moved at all since it was forked, so no commits are reachable.
        parent = base_branch

    # First commit on the cherry-pick branch. We use this for message and author info
    first_commit = (
        await git_ctx.sh.sh(
            git_ctx.git_path,
            "rev-list",
            "--first-parent",
            "--exclude-first-parent-only",
            branch_to_pick,
            "^" + base_branch,
            "--reverse",
        )
    )[1].split("\n")[0]

    if not first_commit:
        raise RevupUsageException(f"No commits found on {branch_to_pick} relative to {base_branch}")

    commit_info = git.parse_rev_list(
        await git_ctx.git_stdout("rev-list", "--header", first_commit, "--not", first_commit + "~"),
    )[0]
    commit_info.tree = branch_to_pick + "^{tree}"
    commit_info.parents = [GitCommitHash(parent)]

    to_cherry_pick = await git_ctx.commit_tree(commit_info)

    return await git_ctx.git_return_code("cherry-pick", to_cherry_pick)
