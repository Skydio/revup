import argparse
import logging

from revup import git
from revup.types import GitCommitHash, GitTreeHash, RevupUsageException


async def find_branch_fetch_if_necessary(git_ctx: git.Git, branch_to_pick: str) -> str:
    """
    Resolve the given branch_to_pick to a local branch, or fetch it from the remote

    Throws RevupUsageException if the branch doesn't exist locally or remotely

    Returns the ref for the local or remote branch
    """
    remote_branch_to_pick = git_ctx.ensure_branch_prefix(branch_to_pick)
    branch_exists = await git_ctx.is_branch_or_commit(branch_to_pick)

    if not branch_exists:
        logging.info(
            f"Couldn't find '{branch_to_pick}', trying to fetch from remote '{git_ctx.remote_name}'"
        )

        await git_ctx.git(
            "fetch",
            "--no-write-fetch-head",
            "--no-auto-maintenance",
            "--quiet" if git_ctx.sh.quiet else "--verbose",
            "--force",
            git_ctx.remote_name,
            f"{branch_to_pick}:remotes/{git_ctx.remote_name}/{branch_to_pick}",
        )

        if await git_ctx.is_branch_or_commit(remote_branch_to_pick):
            logging.info(f"Found '{remote_branch_to_pick}'")
            branch_to_pick = remote_branch_to_pick
        else:
            raise RevupUsageException(f"Couldn't find ref '{branch_to_pick}'")

    return branch_to_pick


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Squash the given branch's changes into a single commit, and cherry-pick
    that commit onto the local branch.
    """
    branch_to_pick = await find_branch_fetch_if_necessary(git_ctx, args.branch[0])

    if args.base_branch:
        base_branch = await find_branch_fetch_if_necessary(git_ctx, args.base_branch)
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
    commit_info.tree = GitTreeHash(branch_to_pick + "^{tree}")
    commit_info.parents = [GitCommitHash(parent)]

    to_cherry_pick = await git_ctx.commit_tree(commit_info)

    return await git_ctx.git_return_code("cherry-pick", to_cherry_pick)
