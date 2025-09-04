from __future__ import annotations

import argparse
import logging
import os
import stat
import subprocess
import sys
from builtins import FileNotFoundError
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, List, Tuple

import revup
from revup import config, git, logs, shell
from revup.config import RevupArgParser
from revup.types import RevupUsageException

REVUP_CONFIG_ENV_VAR = "REVUP_CONFIG_PATH"
CONFIG_FILE_NAME = ".revupconfig"


class HelpAction(argparse.Action):
    """
    A help action that displays a manpage formatted from the markdown documentation if available.
    """

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: Any = None) -> None:
        source_dir = os.path.dirname(os.path.abspath(__file__))
        man_cmd = ("man", "-M", source_dir, parser.prog.split()[-1])
        try:
            if subprocess.call(man_cmd) != 0:
                print("Error in showing man page")
                print(parser.format_help())
        except FileNotFoundError:
            print("'man' binary not found.")
            print(parser.format_help())
        sys.exit(0)


def make_toplevel_parser() -> RevupArgParser:
    revup_parser = RevupArgParser(add_help=False, prog="revup")
    revup_parser.add_argument("--help", "-h", action=HelpAction, nargs=0)
    revup_parser.add_argument(
        "--version", action="version", version=f"%(prog)s {revup.__version__}"
    )
    revup_parser.add_argument("--proxy")
    revup_parser.add_argument("--github-oauth")
    revup_parser.add_argument("--github-username")
    revup_parser.add_argument("--github-url", default="github.com")
    revup_parser.add_argument("--remote-name", default="origin")
    revup_parser.add_argument("--fork-name", default="")
    revup_parser.add_argument("--editor")
    revup_parser.add_argument("--verbose", "-v", action="store_true")
    revup_parser.add_argument("--keep-temp", "-k", action="store_true")
    revup_parser.add_argument("--git-path", default="")
    revup_parser.add_argument("--main-branch", default="main")
    revup_parser.add_argument("--base-branch-globs", default="")
    revup_parser.add_argument("--git-version", default="2.43.0")
    return revup_parser


def get_config_path() -> str:
    home_path = os.path.expanduser("~")
    return os.environ.get(
        os.path.expanduser(REVUP_CONFIG_ENV_VAR), os.path.join(home_path, CONFIG_FILE_NAME)
    )


async def get_config() -> config.Config:
    config_path = get_config_path()
    if os.path.isfile(config_path) and hasattr(os, "getuid"):
        config_stat = os.stat(config_path)
        if config_stat.st_uid != os.getuid():
            raise RevupUsageException("Config file is not owned by the current user!")
        if stat.S_IMODE(config_stat.st_mode) != 0o600:
            raise RevupUsageException(
                f"Permissions too loose on config file!\nTry `chmod 0600 {config_path}`"
            )

    # There's a chicken/egg problem in getting git path from config when we need git
    # to find the path of the config file. Just this once, we use the default.
    sh = shell.Shell()
    repo_root = (await sh.sh(git.get_default_git(), "rev-parse", "--show-toplevel"))[1].rstrip()
    conf = config.Config(config_path, os.path.join(repo_root, CONFIG_FILE_NAME))
    conf.read()
    return conf


async def get_git(args: argparse.Namespace) -> git.Git:
    sh = shell.Shell(not args.verbose)
    git_ctx = await git.make_git(
        sh,
        args.git_path,
        args.git_version,
        args.fork_name if args.fork_name else args.remote_name,
        args.main_branch,
        args.base_branch_globs,
        args.keep_temp,
        args.editor,
    )

    return git_ctx


def dump_args(args: argparse.Namespace) -> None:
    if args.verbose:
        import json

        logging.debug(json.dumps(vars(args), default=str, indent=2))


@asynccontextmanager
async def github_connection(
    git_ctx: git.Git, args: argparse.Namespace, conf: config.Config
) -> AsyncGenerator[Tuple, None]:
    from revup import github_real

    repo_info = await git_ctx.get_github_repo_info(
        github_url=args.github_url, remote_name=args.remote_name
    )

    if not repo_info.owner or not repo_info.name:
        raise RevupUsageException(
            f'Configured remote "{args.remote_name}" does not '
            "point to the a github repository! "
            "You can set it manually by running "
            f"`git remote set-url {args.remote_name} git@github.com:{{OWNER}}/{{PROJECT}}` "
            f"or change the configured remote in {conf.config_path}/"
        )

    fork_info = repo_info
    if args.fork_name and args.fork_name != args.remote_name:
        fork_info = await git_ctx.get_github_repo_info(
            github_url=args.github_url, remote_name=args.fork_name
        )

    if not fork_info.owner or not fork_info.name:
        raise RevupUsageException(
            f'Configured remote fork "{args.fork_info}" does not '
            "point to the a github repository! "
            "You can set it manually by running "
            f"`git remote set-url {args.fork_info} git@github.com:{{OWNER}}/{{PROJECT}}` "
            f"or change the configured remote in {conf.config_path}."
        )

    if repo_info.name != fork_info.name:
        raise RevupUsageException(
            f'Configured remote fork "{args.fork_info}" is not '
            f"the same repo as the remote {args.remote_info}."
        )

    if not args.github_oauth:
        # Try environment variables first
        args.github_oauth = os.environ.get("GITHUB_TOKEN")
        if args.github_oauth:
            logs.redact({args.github_oauth: "<GITHUB_OAUTH>"})
            logging.debug("Used GitHub token from environment variable")
        else:
            # Fall back to git credential helper
            args.github_oauth = await git_ctx.credential(
                protocol="https",
                host=args.github_url,
                path=f"{fork_info.owner}/{fork_info.name}.git",
            )
            if args.github_oauth:
                logs.redact({args.github_oauth: "<GITHUB_OAUTH>"})
                logging.debug("Used credential from git-credential")

    if not args.github_oauth:
        raise RevupUsageException(
            "No Github OAuth token found! "
            "Set the GITHUB_TOKEN environment variable, "
            "login with 'gh auth login', "
            "or make one at https://github.com/settings/tokens/new "
            "(revup needs full repo permissions) "
            "then set it with `revup config github_oauth`."
        )

    github_ep = github_real.RealGitHubEndpoint(
        oauth_token=args.github_oauth, proxy=args.proxy, github_url=args.github_url
    )
    try:
        yield github_ep, repo_info, fork_info
    finally:
        await github_ep.close()


async def main() -> int:
    # Description / help text isn't given to the parser since the actual
    # help text is in the markdown files.
    revup_parser = make_toplevel_parser()
    subparsers = revup_parser.add_subparsers(dest="cmd", required=True, parser_class=RevupArgParser)

    upload_parser = subparsers.add_parser("upload", add_help=False)
    restack_parser = subparsers.add_parser(
        "restack",
        add_help=False,
    )
    cherry_pick_parser = subparsers.add_parser(
        "cherry-pick",
        add_help=False,
    )
    amend_parser = subparsers.add_parser("amend", aliases=["commit"], add_help=False)
    config_parser = subparsers.add_parser(
        "config",
        add_help=False,
    )
    toolkit_parser = subparsers.add_parser(
        "toolkit", description="Exercise various subfunctionalities."
    )

    # Intentionally does not contain config or toolkit parsers since the those are not configurable
    all_parsers: List[RevupArgParser] = [
        revup_parser,
        amend_parser,
        cherry_pick_parser,
        restack_parser,
        upload_parser,
    ]

    for p in [upload_parser, restack_parser, amend_parser]:
        # Some args are used by both upload and restack
        p.add_argument("--help", "-h", action=HelpAction, nargs=0)
        p.add_argument("--base-branch", "-b")
        p.add_argument("--relative-branch", "-e")

    upload_parser.add_argument("topics", nargs="*")
    upload_parser.add_argument("--rebase", "-r", action="store_true")
    upload_parser.add_argument("--skip-confirm", "-s", action="store_true")
    upload_parser.add_argument("--dry-run", "-d", action="store_true")
    upload_parser.add_argument("--push-only", action="store_true")
    upload_parser.add_argument("--status", "-t", action="store_true")
    upload_parser.add_argument("--update-pr-body", action="store_true", default=True)
    upload_parser.add_argument("--create-local-branches", action="store_true")
    upload_parser.add_argument("--review-graph", action="store_true", default=True)
    upload_parser.add_argument("--trim-tags", action="store_true")
    upload_parser.add_argument("--patchsets", action="store_true", default=True)
    upload_parser.add_argument("--self-authored-only", action="store_true", default=True)
    upload_parser.add_argument("--labels")
    upload_parser.add_argument(
        "--auto-add-users", default="no", choices=["no", "a2r", "r2a", "both"]
    )
    upload_parser.add_argument(
        "--user-aliases",
    )
    upload_parser.add_argument("--uploader")
    upload_parser.add_argument(
        "--branch-format", choices=["user+branch", "user", "branch", "none"], default="user+branch"
    )
    upload_parser.add_argument("--pre-upload", "-p")
    upload_parser.add_argument("--relative-chain", "-c", action="store_true")
    upload_parser.add_argument("--auto-topic", "-a", action="store_true")
    upload_parser.add_argument("--head", default="HEAD")

    restack_parser.add_argument("--topicless-last", "-t", action="store_true")

    amend_parser.add_argument("ref_or_topic", nargs="?")
    amend_parser.add_argument("--edit", "-s", default=True, action="store_true")
    amend_parser.add_argument("--insert", "-i", action="store_true")
    amend_parser.add_argument("--drop", "-d", action="store_true")
    amend_parser.add_argument("--all", "-a", action="store_true")

    amend_parser.add_argument("--parse-topics", default=True, action="store_true")
    amend_parser.add_argument("--parse-refs", default=True, action="store_true")

    cherry_pick_parser.add_argument("--help", "-h", action=HelpAction, nargs=0)
    cherry_pick_parser.add_argument("branch", nargs=1)
    cherry_pick_parser.add_argument("--base-branch", "-b")

    config_parser.add_argument("--help", "-h", action=HelpAction, nargs=0)
    config_parser.add_argument("flag", nargs=1)
    config_parser.add_argument("value", nargs="?")
    config_parser.add_argument("--repo", "-r", action="store_true")
    config_parser.add_argument("--delete", "-d", action="store_true")

    toolkit_subparsers = toolkit_parser.add_subparsers(dest="toolkit_cmd", required=True)
    detect_branch = toolkit_subparsers.add_parser(
        "detect-branch", description="Detect the base branch of the current branch."
    )
    detect_branch.add_argument(
        "--show-all", "-s", action="store_true", help="Show all candidates, not just the best one"
    )
    detect_branch.add_argument(
        "--no-limit", "-n", action="store_true", help="Don't limit to release branches"
    )
    toolkit_cherry_pick = toolkit_subparsers.add_parser(
        "cherry-pick", description="Cherry pick given commit to a new parent"
    )
    toolkit_cherry_pick.add_argument("--commit", "-c", help="Commit to cherry-pick", required=True)
    toolkit_cherry_pick.add_argument("--parent", "-p", help="Parent commit", required=True)
    toolkit_diff_target = toolkit_subparsers.add_parser(
        "diff-target", description="Make a virtual diff target from the given commits"
    )
    toolkit_diff_target.add_argument("--old-head", "-oh", help="Old head commit", required=True)
    toolkit_diff_target.add_argument(
        "--old-base", "-ob", help="Old base commit (parent of old head by default)"
    )
    toolkit_diff_target.add_argument("--new-head", "-nh", help="New head commit", required=True)
    toolkit_diff_target.add_argument(
        "--new-base", "-nb", help="New base commit (parent of old head by default)"
    )
    toolkit_diff_target.add_argument("--parent", "-p", help="Parent commit")
    toolkit_fork_point = toolkit_subparsers.add_parser(
        "fork-point", description="Find the first divergence between two branches"
    )
    toolkit_fork_point.add_argument("branches", nargs=2, help="Branches to compare")
    toolkit_closest_branch = toolkit_subparsers.add_parser(
        "closest-branch", description="Find the nearest base branch to the given commit."
    )
    toolkit_closest_branch.add_argument("branch", nargs=1, help="Commit/branch")
    toolkit_closest_branch.add_argument(
        "--allow-self", action="store_true", help='Allow the branch itself to be a valid "closest"'
    )
    toolkit_list_topics = toolkit_subparsers.add_parser(
        "list-topics", description="List all topics and their commits"
    )
    toolkit_list_topics.add_argument(
        "--base-branch", "-b", help="Use the given branch as the base instead of autodetecting."
    )
    toolkit_list_topics.add_argument(
        "--relative-branch", "-e", help="Use the given relative branch."
    )
    list_topics_commit_options = toolkit_list_topics.add_mutually_exclusive_group()
    list_topics_commit_options.add_argument(
        "--commit-ids",
        "-c",
        action="store_true",
        help="Print the IDs for all commits within a topic",
    )
    list_topics_commit_options.add_argument(
        "--titles",
        "-t",
        action="store_true",
        help="Print the titles for all commits within a topic",
    )

    # Do an initial parsing pass, which handles HelpAction
    args = revup_parser.parse_args()
    conf = await get_config()

    # Run config before setting the config, in order to avoid the situation where a broken
    # config prevents you from running config at all.
    if args.cmd == "config":
        logs.configure_logger(False, {})
        return config.config_main(conf, args, all_parsers)

    for p in all_parsers:
        assert isinstance(p, RevupArgParser)
        p.set_defaults_from_config(conf.get_config())
    args = revup_parser.parse_args()

    # So users don't accidentally leak their oauth when sharing logs
    logs.configure_logger(
        debug=args.verbose,
        redactions={args.github_oauth: "<GITHUB_OAUTH>"} if args.github_oauth else {},
    )
    dump_args(args)

    git_ctx = await get_git(args)

    if args.cmd == "toolkit":
        from revup import toolkit

        return await toolkit.main(args=args, git_ctx=git_ctx)

    elif args.cmd == "cherry-pick":
        from revup import cherry_pick

        return await cherry_pick.main(args=args, git_ctx=git_ctx)

    elif args.cmd in ["commit", "amend"]:
        from revup import amend

        # "commit" is an alias of "amend --insert"
        args.insert = args.cmd == "commit" or args.insert

        repo_info = await git_ctx.get_github_repo_info(
            github_url=args.github_url, remote_name=args.remote_name
        )

        if not repo_info.owner or not repo_info.name:
            # Don't try to get topics for repos that are not in use with github
            args.parse_topics = False

        return await amend.main(args=args, git_ctx=git_ctx)

    elif args.cmd == "restack":
        from revup import restack

        return await restack.main(args=args, git_ctx=git_ctx)

    async with github_connection(args=args, git_ctx=git_ctx, conf=conf) as (
        github_ep,
        repo_info,
        fork_info,
    ):
        if args.cmd == "upload":
            from revup import upload

            return await upload.main(
                args=args,
                git_ctx=git_ctx,
                github_ep=github_ep,
                repo_info=repo_info,
                fork_info=fork_info,
            )

    return 1
