import argparse
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from revup import config, git, logs
from revup.forge import Forge, ForgeRepoInfo, PullRequestParams
from revup.types import RevupUsageException

RE_PR_URL = re.compile(
    r"^https://(?P<forge_url>[^/]+)/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/(?P<number>[0-9]+)/?$"
)


def parse_forge_info(remote_url: str, forge_url: str) -> ForgeRepoInfo:
    """
    Parse owner and repo name from a remote URL, matching against the given forge host.
    """
    owner = ""
    name = ""
    while True:
        match = rf"^[^@]+@{forge_url}:([^/]+)/(.+)(?<!\.git)(?:\.git)?$"
        m = re.match(match, remote_url)
        if m:
            owner = m.group(1)
            name = m.group(2)
            break
        search = rf"{forge_url}/([^/]+)/(.+)(?<!\.git)(?:\.git)?$"
        m = re.search(search, remote_url)
        if m:
            owner = m.group(1)
            name = m.group(2)
            break

        break

    return ForgeRepoInfo(owner=owner, name=name)


def parse_pull_request_url(pull_request: str) -> PullRequestParams:
    m = RE_PR_URL.match(pull_request)
    if not m:
        raise RuntimeError("Did not understand PR argument.  PR must be URL")

    forge_url = m.group("forge_url")
    owner = m.group("owner")
    name = m.group("name")
    number = int(m.group("number"))
    return PullRequestParams(forge_url=forge_url, owner=owner, name=name, number=number)


@asynccontextmanager
async def forge_connection(
    git_ctx: git.Git, args: argparse.Namespace, conf: config.Config
) -> AsyncGenerator[Forge, None]:
    remote_url = await git_ctx.get_remote_url(args.remote_name)
    repo_info = parse_forge_info(remote_url, args.forge_url)

    if not repo_info.owner or not repo_info.name:
        raise RevupUsageException(
            f'Configured remote "{args.remote_name}" does not '
            "point to a recognized forge repository! "
            "You can set it manually by running "
            f"`git remote set-url {args.remote_name} git@<host>:{{OWNER}}/{{PROJECT}}` "
            f"or change the configured remote in {conf.config_path}/"
        )

    fork_info = repo_info
    if args.fork_name and args.fork_name != args.remote_name:
        fork_url = await git_ctx.get_remote_url(args.fork_name)
        fork_info = parse_forge_info(fork_url, args.forge_url)

    if not fork_info.owner or not fork_info.name:
        raise RevupUsageException(
            f'Configured remote fork "{args.fork_info}" does not '
            "point to a recognized forge repository! "
            "You can set it manually by running "
            f"`git remote set-url {args.fork_info} git@<host>:{{OWNER}}/{{PROJECT}}` "
            f"or change the configured remote in {conf.config_path}."
        )

    if repo_info.name != fork_info.name:
        raise RevupUsageException(
            f'Configured remote fork "{args.fork_info}" is not '
            f"the same repo as the remote {args.remote_info}."
        )

    if not args.forge_oauth:
        # Try environment variables first
        args.forge_oauth = os.environ.get("GITHUB_TOKEN")
        if args.forge_oauth:
            logs.redact({args.forge_oauth: "<FORGE_OAUTH>"})
            logging.debug("Used token from environment variable")
        else:
            # Fall back to git credential helper
            args.forge_oauth = await git_ctx.credential(
                protocol="https",
                host=args.forge_url,
                path=f"{fork_info.owner}/{fork_info.name}.git",
            )
            if args.forge_oauth:
                logs.redact({args.forge_oauth: "<FORGE_OAUTH>"})
                logging.debug("Used credential from git-credential")

    if not args.forge_oauth:
        raise RevupUsageException(
            "No OAuth token found! "
            "Set the GITHUB_TOKEN environment variable, "
            "login with 'gh auth login', "
            "or make one at https://github.com/settings/tokens/new "
            "(revup needs full repo permissions) "
            "then set it with `revup config forge_oauth`."
        )

    if "github" in args.forge_url:
        from revup.github.endpoint import GitHubEndpoint
        from revup.github.github import Github

        endpoint = GitHubEndpoint(
            oauth_token=args.forge_oauth, proxy=args.proxy, github_url=args.forge_url
        )
        forge = Github(endpoint=endpoint, repo_info=repo_info, fork_info=fork_info)
        try:
            yield forge
        finally:
            await forge.close()
    else:
        raise RevupUsageException(
            f'Unrecognized forge URL "{args.forge_url}". ' "Currently only GitHub is supported."
        )
