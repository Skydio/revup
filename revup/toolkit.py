import argparse
import asyncio
import logging

from revup import git
from revup.types import RevupUsageException


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Miscellaneous commands exposing subunits of possibly useful functionality.
    Mainly designed for expert users or scripts.
    """
    if args.toolkit_cmd == "detect-branch":
        if args.show_all:
            target_branches = await git_ctx.get_best_base_branch_candidates(
                "HEAD", not args.no_limit
            )
            logging.info(", ".join(target_branches))
        else:
            target_branch = await git_ctx.get_best_base_branch("HEAD", not args.no_limit)
            logging.info(target_branch)
    elif args.toolkit_cmd == "cherry-pick":
        await asyncio.gather(
            git_ctx.verify_branch_or_commit(args.commit),
            git_ctx.verify_branch_or_commit(args.parent),
        )

        commit_header = git.parse_rev_list(
            await git_ctx.rev_list(args.commit, max_revs=1, header=True)
        )
        if len(commit_header) != 1:
            raise RevupUsageException(f"Commit {args.commit} doesn't exist!")
        logging.info(await git_ctx.synthetic_cherry_pick_from_commit(commit_header[0], args.parent))
    elif args.toolkit_cmd == "diff-target":
        await asyncio.gather(
            git_ctx.verify_branch_or_commit(args.old_head),
            git_ctx.verify_branch_or_commit(args.new_head),
        )

        if not args.old_base:
            args.old_base = git_ctx.to_commit_hash(args.old_head + "~")
        if not args.new_base:
            args.new_base = git_ctx.to_commit_hash(args.new_head + "~")
        logging.info(
            await git_ctx.make_virtual_diff_target(
                args.old_base, args.old_head, args.new_base, args.new_head, args.parent
            )
        )
    elif args.toolkit_cmd == "fork-point":
        await asyncio.gather(
            git_ctx.verify_branch_or_commit(args.branches[0]),
            git_ctx.verify_branch_or_commit(args.branches[1]),
        )
        logging.info(await git_ctx.to_commit_hash(await git_ctx.fork_point(*args.branches)))
    elif args.toolkit_cmd == "closest-branch":
        await git_ctx.verify_branch_or_commit(args.branch[0])
        logging.info(await git_ctx.get_best_base_branch(args.branch[0], allow_self=args.allow_self))

    return 0
