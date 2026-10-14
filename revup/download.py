from __future__ import annotations

import argparse
import logging
import re

from revup import config, git
from revup.cherry_pick import resolve_pr_url, squash_and_cherry_pick
from revup.core_types import RevupUsageException
from revup.forge_utils import RE_PR_URL, parse_forge_info
from revup.topic_stack import match_commit_tag

DOWNLOAD_TAG = "Revup-Download"
DOWNLOAD_WARNING = (
    'This commit is managed by "revup download", '
    "any changes made to it will be lost so don't modify it manually."
)


def rewrite_download_msg(msg: str, branch: str) -> str:
    """
    Drop revup tags (Topic etc.) from msg, so the downloaded change isn't mistaken for one
    of the user's own topics, and add the warning and the tag that records its source branch.
    """
    kept = [
        ln
        for ln in msg.split("\n")
        if match_commit_tag(ln) is None and not ln.startswith(f"{DOWNLOAD_TAG}:")
    ]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return f"{body}\n\n{DOWNLOAD_WARNING}\n\n{DOWNLOAD_TAG}: {branch}\n"


async def is_downloaded(git_ctx: git.Git, branch: str) -> bool:
    """
    Whether the current stack (HEAD back to its base branch) already has a commit downloaded
    from the given branch.
    """
    found: bool
    try:
        base = await git_ctx.get_best_base_branch("HEAD", True)
        found = await git_ctx.is_branch_or_commit(base)
    except IndexError:
        found = False
    if not found:
        raise RevupUsageException(
            f"No base branch found for HEAD; fetch remote '{git_ctx.remote_name}' first"
        )
    log = await git_ctx.git_stdout("log", "--format=%B", f"{base}..HEAD")
    return f"{DOWNLOAD_TAG}: {branch}" in (ln.strip() for ln in log.split("\n"))


async def main(args: argparse.Namespace, git_ctx: git.Git, conf: config.Config) -> int:
    """
    Cherry-pick mode of download: squash the given PR or branch into one commit tagged with
    its source, and cherry-pick it onto HEAD.
    """
    target = args.pr_or_branch[0]
    base = args.base_branch

    repo = parse_forge_info(await git_ctx.get_remote_url(args.remote_name), args.forge_url)

    if re.fullmatch(r"[0-9]+", target):
        # A bare number is a PR in the repo that the remote points at.
        if not repo.owner or not repo.name:
            raise RevupUsageException(f"Can't turn '{target}' into a PR URL; use the full URL")
        target = f"https://{args.forge_url}/{repo.owner}/{repo.name}/pull/{target}"

    m = RE_PR_URL.match(target)
    if m:
        # The branch is fetched from the remote, so the PR must live in the remote's repo.
        if (m.group("forge_url"), m.group("owner").lower(), m.group("name").lower()) != (
            args.forge_url,
            repo.owner.lower(),
            repo.name.lower(),
        ):
            raise RevupUsageException(
                f"'{target}' is not in the repository of remote '{args.remote_name}' "
                f"({repo.owner}/{repo.name}); downloading from other repos or forks isn't supported"
            )
        target, pr_base = await resolve_pr_url(args, git_ctx, conf, target)
        base = base or pr_base

    # So "feature" and "origin/feature" are the same download.
    target = git_ctx.remove_branch_prefix(target)

    if await is_downloaded(git_ctx, target):
        raise RevupUsageException(f"'{target}' is already downloaded in the current stack")

    logging.info(f"Downloading '{target}'")
    return await squash_and_cherry_pick(
        git_ctx, target, base, lambda msg: rewrite_download_msg(msg, target)
    )
