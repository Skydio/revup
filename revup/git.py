import asyncio
import copy
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, NamedTuple, Optional, Pattern, Tuple

from revup import shell
from revup.types import (
    GitCommitHash,
    GitConflictException,
    GitTreeHash,
    RevupUsageException,
)

RE_RAW_COMMIT_ID = re.compile(r"^(?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_AUTHOR = re.compile(
    r"^author (?P<author>(?P<name>[^<]+?) <(?P<email>[^>]+)> (?P<date>[0-9]+ [+-][0-9]+))$",
    re.MULTILINE,
)
RE_RAW_COMMITTER = re.compile(
    r"^committer (?P<author>(?P<name>[^<]+?) <(?P<email>[^>]+)> (?P<date>[0-9]+ [+-][0-9]+))$",
    re.MULTILINE,
)
RE_RAW_PARENT = re.compile(r"^parent (?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_TREE = re.compile(r"^tree (?P<tree>.+)$", re.MULTILINE)
RE_RAW_COMMIT_MSG_LINE = re.compile(r"^    (?P<line>.*)$", re.MULTILINE)

RE_LS_FILES_LINE = re.compile(
    r"^[0-9]+ (?P<hash>[0-9a-f]+) (?P<stage>[0-9])\t(?P<path>.*)$", re.MULTILINE
)
RE_RAW_DIFF_TREE_LINE = re.compile(
    r"^:(?P<old_mode>[0-9]+) (?P<new_mode>[0-9]+) (?P<old_hash>[0-9a-f]+) (?P<new_hash>[0-9a-f]+)"
    r" (?P<type>[a-zA-Z]+)\t(?P<path>.*)$",
    re.MULTILINE,
)

RE_COMMIT_HASH = re.compile(r"^[0-9a-f]{8,}")

GIT_DIFF_ARGS = [
    "--no-pager",
    "diff",
    "--full-index",
    "--no-color",
    "--no-textconv",
    "-U1",
]

COMMON_MAIN_BRANCHES = ["main", "master"]  # Below logic assumes 2 values here


GitHubRepoInfo = NamedTuple(
    "GitHubRepoInfo",
    [("name", str), ("owner", str)],
)


@dataclass
class CommitHeader:
    """
    Represents the information extracted from `git rev-list --header`
    """

    tree: GitTreeHash
    parents: List[GitCommitHash]
    author_name: str = ""
    author_email: str = ""
    author_date: str = ""
    committer_name: str = ""
    committer_email: str = ""
    committer_date: str = ""
    commit_msg: str = ""
    title: str = ""
    commit_id: GitCommitHash = GitCommitHash("")


def parse_commit_header(raw_header: str) -> CommitHeader:
    def _search_group(raw_header: str, regex: Pattern[str], group: str) -> str:
        m = regex.search(raw_header)
        assert m
        return m.group(group)

    tree = GitTreeHash(_search_group(raw_header, RE_RAW_TREE, "tree"))
    title = _search_group(raw_header, RE_RAW_COMMIT_MSG_LINE, "line")
    commit_id = GitCommitHash(_search_group(raw_header, RE_RAW_COMMIT_ID, "commit"))
    parents = [GitCommitHash(m.group("commit")) for m in RE_RAW_PARENT.finditer(raw_header)]
    author_name = _search_group(raw_header, RE_RAW_AUTHOR, "name")
    author_email = _search_group(raw_header, RE_RAW_AUTHOR, "email")
    author_date = _search_group(raw_header, RE_RAW_AUTHOR, "date")
    committer_name = _search_group(raw_header, RE_RAW_COMMITTER, "name")
    committer_email = _search_group(raw_header, RE_RAW_COMMITTER, "email")
    committer_date = _search_group(raw_header, RE_RAW_COMMITTER, "date")
    commit_msg = "\n".join(m.group("line") for m in RE_RAW_COMMIT_MSG_LINE.finditer(raw_header))
    return CommitHeader(
        tree,
        parents,
        author_name,
        author_email,
        author_date,
        committer_name,
        committer_email,
        committer_date,
        commit_msg,
        title,
        commit_id,
    )


def parse_rev_list(s: str) -> List[CommitHeader]:
    """
    Parses output of rev-list -v and returns a list of commits
    """
    return list(map(parse_commit_header, s.split("\0")[:-1]))


def commits_match(a: CommitHeader, b: CommitHeader) -> bool:
    """
    Returns whether author and commit message are the same for the given commits
    """
    return (
        a.title == b.title
        and a.author_name == b.author_name
        and a.author_email == b.author_email
        and a.committer_name == b.committer_name
        and a.committer_email == b.committer_email
        and a.commit_msg == b.commit_msg
    )


def is_commit_hash(commit_ish: GitCommitHash) -> bool:
    """
    Determine if the given commit-ish ref is a hash.
    """
    return re.match(RE_COMMIT_HASH, commit_ish) is not None


def get_default_git() -> str:
    ret = shutil.which("git")
    if not ret:
        raise RevupUsageException("Could not find a 'git' binary on the current PATH.")
    return ret


async def make_git(
    sh: shell.Shell,
    git_path: str = "",
    git_version: str = "",
    remote_name: str = "",
    main_branch: str = "",
    base_branch_globs: str = "",
    keep_temp: bool = False,
    editor: str = "",
) -> "Git":
    if not git_path:
        git_path = get_default_git()

    git_ctx = Git(sh, git_path, remote_name, main_branch, base_branch_globs, keep_temp)

    async def get_email() -> str:
        email = await git_ctx.git_stdout("config", "user.email", raiseonerror=False)
        if not email:
            raise RuntimeError(
                "Couldn't get git email, set it with `git config --global user.email`"
            )
        return email

    async def get_editor() -> str:
        if editor:
            return editor
        ret = await git_ctx.git_stdout("config", "core.editor", raiseonerror=False)
        if not ret:
            ret = os.environ.get("GIT_EDITOR", os.environ.get("EDITOR", "nano"))
        return ret

    repo_root, git_dir, actual_version, email, editor, main_exists = await asyncio.gather(
        git_ctx.git_stdout("rev-parse", "--show-toplevel"),
        git_ctx.git_stdout("rev-parse", "--path-format=absolute", "--git-dir"),
        git_ctx.git_stdout("--version"),
        get_email(),
        get_editor(),
        git_ctx.commit_exists(main_branch),
    )

    if git_version:
        version_arr = [int(v) for v in git_version.split(".")]
        actual_version_arr = [int(v) for v in actual_version.split()[2].split(".")[:3]]
        for v, a in zip(version_arr, actual_version_arr):
            if a > v:
                break
            elif a == v:
                continue
            raise RuntimeError(
                f"revup requires git {version_arr}, you're running {actual_version_arr}"
            )
    git_ctx.repo_root = repo_root
    sh.cwd = git_ctx.repo_root
    git_ctx.git_dir = git_dir
    git_ctx.email = email.lower()
    git_ctx.author = git_ctx.email.split("@")[0]
    git_ctx.editor = editor
    if not main_exists:
        if main_branch in COMMON_MAIN_BRANCHES:
            git_ctx.main_branch = COMMON_MAIN_BRANCHES[1 - COMMON_MAIN_BRANCHES.index(main_branch)]
            logging.info(
                'Branch {} not found, falling back to "{}". We recommend you set this in'
                " .revupconfig".format(main_branch, git_ctx.main_branch)
            )
    return git_ctx


class Git:
    # Shell object to run git with
    sh: shell.Shell

    # Path for git executable
    git_path: str

    # Remote to use for most operations
    remote_name: str

    # Git branch configuration
    main_branch: str
    base_branch_globs: List[str]

    # Whether to keep temporary files
    keep_temp: bool

    # Scratch directory for temporary files
    temp_dir: tempfile.TemporaryDirectory

    # Root directory of the git repo
    repo_root: str

    # .git directory of the repo. Note that this may not be in
    # repo_root when inside a worktree
    git_dir: str

    email: str
    author: str
    editor: str

    def __init__(
        self,
        sh: shell.Shell,
        git_path: str,
        remote_name: str,
        main_branch: str,
        base_branch_globs: str,
        keep_temp: bool,
    ):
        self.sh = sh
        self.git_path = git_path
        self.remote_name = remote_name
        self.keep_temp = keep_temp
        self.main_branch = main_branch
        self.base_branch_globs = base_branch_globs.strip().splitlines()
        self.temp_dir = tempfile.TemporaryDirectory(  # pylint: disable=consider-using-with
            prefix="revup_"
        )
        if self.keep_temp:
            os.makedirs(self.get_scratch_dir(), exist_ok=True)

    def get_scratch_dir(self) -> str:
        """
        Get the name of the current scratch directory. Any contents will be deleted
        when the program exits.
        """
        return f"{self.repo_root}/.revup" if self.keep_temp else self.temp_dir.name

    async def git(self, *args: str, **kwargs: Any) -> Tuple[int, str]:
        """
        Run a git command.  The returned stdout has trailing newlines stripped.

        Args:
            *args: Arguments to git
            **kwargs: Any valid kwargs for sh()
        """

        def _maybe_rstrip(s: Tuple[int, str]) -> Tuple[int, str]:
            return (s[0], s[1].rstrip())

        return _maybe_rstrip(await self.sh.sh(*((self.git_path,) + args), **kwargs))

    async def git_return_code(self, *args: str, **kwargs: Any) -> int:
        return (await self.git(raiseonerror=False, *args, **kwargs))[0]

    async def git_stdout(self, *args: str, **kwargs: Any) -> str:
        return (await self.git(*args, **kwargs))[1]

    async def get_github_repo_info(self, github_url: str, remote_name: str) -> GitHubRepoInfo:
        """
        Return github repo's name and owner.
        """
        owner = ""
        name = ""
        ret = await self.git("remote", "get-url", remote_name, raiseonerror=False)
        if ret[0] != 0:
            return GitHubRepoInfo(owner=owner, name=name)
        remote_url = ret[1]
        while True:
            match = rf"^[^@]+@{github_url}:([^/]+)/([^.]+)(?:\.git)?$"
            m = re.match(match, remote_url)
            if m:
                owner = m.group(1)
                name = m.group(2)
                break
            search = rf"{github_url}/([^/]+)/([^.]+)"
            m = re.search(search, remote_url)
            if m:
                owner = m.group(1)
                name = m.group(2)
                break

            break

        info = GitHubRepoInfo(owner=owner, name=name)
        return info

    async def rev_list(
        self,
        include: str,
        exclude: str = None,
        first_parent: bool = False,
        exclude_first_parent: bool = False,
        header: bool = False,
        max_revs: int = 0,
    ) -> str:
        """
        Wrapper for git rev-list
        """
        rev_list_args = ["rev-list", "--reverse", include]
        if max_revs:
            rev_list_args.extend(["-n", f"{max_revs}"])
        if first_parent:
            rev_list_args.append("--first-parent")
        if exclude_first_parent:
            rev_list_args.append("--exclude-first-parent-only")
        if header:
            rev_list_args.append("--header")
        if exclude is not None:
            rev_list_args.extend(["--not", exclude])
        return await self.git_stdout(*rev_list_args)

    async def is_branch_or_commit(self, obj: str) -> bool:
        return await self.git_return_code("rev-parse", "--verify", "--quiet", obj) == 0

    async def verify_branch_or_commit(self, obj: str) -> None:
        if not await self.is_branch_or_commit(obj):
            raise RevupUsageException(f"{obj} is not a commit or branch name!")

    async def commit_exists(self, obj: str) -> bool:
        return (
            await self.git_return_code("rev-parse", "--verify", "--quiet", obj + "^{commit}") == 0
        )

    async def to_commit_hash(self, ref: str) -> GitCommitHash:
        return GitCommitHash(
            await self.git_stdout("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        )

    async def fork_point(self, ref: str, baseRef: str) -> GitCommitHash:
        """
        Define the fork-point of your branch and a base branch as the commit at
        which the two branches first diverged in history.
        To find this, get the list of commits reachable from your branch,
        but not reachable from the base branch. The fork point is the parent of
        the last commit in that list, and the length of that commit list is
        the number of changes being introduced by your branch.
        If that list of commits ends up being empty, it means your branch has not
        introduced any new commits, so the fork point is just your ref.

        Returns the fork point of ref and baseRef.
        """
        commit = (
            await self.sh.sh(
                self.git_path,
                "rev-list",
                "--first-parent",
                "--exclude-first-parent-only",
                ref,
                "^" + baseRef,
                "--reverse",
            )
        )[1].split("\n")[0]

        if not commit:
            return GitCommitHash(ref)
        return GitCommitHash(f"{commit}~")

    async def distance_to_fork_point(self, ref: str, baseRef: str, max_n: int = 0) -> int:
        """
        Return number of commits between ref and its fork point with baseRef, up to the given max.
        """
        max_args = ["-n", f"{max_n + 1}"] if max_n else []
        ret = await self.git_stdout(
            "rev-list",
            "--first-parent",
            "--exclude-first-parent-only",
            ref,
            "^" + baseRef,
            "--count",
            *max_args,
        )
        return int(ret)

    async def is_ancestor(self, ref: str, ancestor: str) -> bool:
        """
        Return whether ref is a first parent ancestor of the given ancestor.

        This is different from merge-base --is-ancestor since that checks all
        parents, not just the first.
        """
        return await self.distance_to_fork_point(ref, ancestor, 1) == 0

    async def have_identical_trees(self, ref1: GitCommitHash, ref2: GitCommitHash) -> bool:
        """
        Return whether two commit-ish have the same trees, which indicate that
        they have no diff.
        """
        tree1 = await self.git_stdout("rev-parse", f"{ref1}^{{tree}}")
        tree2 = await self.git_stdout("rev-parse", f"{ref2}^{{tree}}")
        return tree1 == tree2

    def ensure_branch_prefix(self, branch: str) -> str:
        """
        Ensure the branch is prefixed with the remote name.
        """
        if branch.startswith(self.remote_name + "/"):
            return branch
        return f"{self.remote_name}/{branch}"

    def remove_branch_prefix(self, branch: str) -> str:
        """
        Ensure the branch is not prefixed with the remote name.
        """
        if not branch.startswith(self.remote_name + "/"):
            return branch
        return branch[len(f"{self.remote_name}/") :]

    async def find_remote_branches(
        self, commit: str, limit_to_base_branches: bool, prune_old: bool
    ) -> List[str]:
        """
        Finds all branches that are candidates for auto-detected base branch of the given commit.
        Optionally, limit_to_base_branches will only select those branches which match
        a branch naming glob given in the config.
        prune_old will discard invalid branches to speed up the selection process.
        Return a list of branch names
        """
        args = ["--format", "%(refname)"]

        if limit_to_base_branches:
            if not self.base_branch_globs:
                return [f"{self.remote_name}/{self.main_branch}"]
            args.append(f"refs/remotes/{self.remote_name}/{self.main_branch}")
            for b in self.base_branch_globs:
                args.append(f"refs/remotes/{self.remote_name}/" + b)
        else:
            args.append(f"refs/remotes/{self.remote_name}/{self.main_branch}")
            args.append(f"refs/remotes/{self.remote_name}/*")

        if prune_old:
            fork_with_main = await self.fork_point(commit, f"{self.remote_name}/{self.main_branch}")
            # A branch that doesn't contain the fork with main must be too old
            args.extend(
                (
                    "--contains",
                    fork_with_main,
                )
            )

        RE_REMOTE_REF = re.compile(r"^refs/remotes/(?P<branch>.*)$")
        ret: List[str] = []
        for ref in (await self.git_stdout("for-each-ref", *args)).split("\n"):
            result = RE_REMOTE_REF.search(ref)
            if result is not None:
                ret.append(result.group("branch"))
        return ret

    async def get_best_base_branch_candidates(
        self, commit: str, limit_to_base_branches: bool = True, allow_self: bool = True
    ) -> List[str]:
        """
        Find the best base branch for the current HEAD by listing candidate remote branches
        Return the branch(es) with the shortest distance from HEAD to fork-point
        """
        branches = await self.find_remote_branches(commit, limit_to_base_branches, True)
        candidates: List[Tuple[int, str]] = []

        if len(branches) == 1:
            return branches

        for b in branches:
            if not allow_self and b == commit:
                continue

            # If we have valid candidates, we can stop iterating once the distance is greater
            # than the current best distance.
            dist = await self.distance_to_fork_point(
                commit, b, candidates[0][0] if candidates else 0
            )

            if len(candidates) == 0 or candidates[0][0] > dist:
                candidates = [(dist, b)]
            elif candidates[0][0] == dist:
                candidates.append((dist, b))
        return [c[1] for c in candidates]

    async def get_best_base_branch(
        self,
        commit: str,
        limit_to_base_branches: bool = True,
        allow_self: bool = True,
    ) -> str:
        """
        If the current branch or main is among the best branches, choose that.
        Otherwise choose the last lexographically.
        """
        candidates = await self.get_best_base_branch_candidates(
            commit, limit_to_base_branches, allow_self
        )
        ret = candidates[0]
        if len(candidates) == 1:
            return ret
        current_branch = (await self.git("branch", "--show-current"))[1]
        for c in candidates:
            if c == f"{self.remote_name}/{current_branch}":
                ret = c
                break
            elif c == f"{self.remote_name}/{self.main_branch}":
                ret = c
                break
            elif c > ret:
                ret = c
        return ret

    async def ls_files(
        self, show_conflicts: bool = False, env: Optional[Dict[str, str]] = None
    ) -> List[Tuple[GitTreeHash, int, str]]:
        args = ["ls-files"]
        if show_conflicts:
            args.append("-u")
        else:
            args.append("-s")
        raw = await self.git(*args, env=env)
        return [
            (GitTreeHash(m.group("hash")), int(m.group("stage")), m.group("path"))
            for m in RE_LS_FILES_LINE.finditer(raw[1])
        ]

    async def commit_tree(self, commit_info: CommitHeader) -> GitCommitHash:
        """
        Run git commit-tree with the args in commit_info.
        """
        git_env = {
            "GIT_AUTHOR_NAME": commit_info.author_name,
            "GIT_AUTHOR_EMAIL": commit_info.author_email,
            "GIT_AUTHOR_DATE": commit_info.author_date,
            "GIT_COMMITTER_NAME": commit_info.committer_name,
            "GIT_COMMITTER_EMAIL": commit_info.committer_email,
            "GIT_COMMITTER_DATE": commit_info.committer_date,
        }
        git_env = {k: v for k, v in git_env.items() if v != ""}
        commit_tree_args = ["commit-tree", commit_info.tree, "-m", commit_info.commit_msg]
        for p in commit_info.parents:
            commit_tree_args.extend(["-p", p])
        ret = await self.git_stdout(*commit_tree_args, env=git_env)
        return GitCommitHash(ret)

    async def get_patch_id(
        self,
        commit: GitCommitHash,
    ) -> str:
        """
        Return a patch-id that uniquely identifies this commit's diff (but not its other metadata).
        """
        patch_source = (
            [
                self.git_path,
            ]
            + GIT_DIFF_ARGS
            + [
                commit + "~",
                commit,
            ]
        )
        ret = (
            await self.sh.piped_sh(
                patch_source,
                [self.git_path, "patch-id", "--stable"],
            )
        )[1].split()
        # If the diff is empty, patch id will return nothing. We just use that as the patch-id since
        # it fulfills the requirement of matching other empty diffs.
        return ret[0] if ret else ""

    async def get_diff_summary(
        self,
        parent: GitCommitHash,
        commit: GitCommitHash,
    ) -> str:
        """
        Return the summary of the diff (files and lines changed)
        """
        return (await self.git_stdout("diff", "--shortstat", parent, commit)).rstrip()

    async def synthetic_cherry_pick(
        self,
        commit_info: CommitHeader,
        parent_tree: GitTreeHash,
        new_parent: GitCommitHash,
        patch_source: List[str],
    ) -> GitCommitHash:
        """
        Given a patch source command and a target tree, attempt to use the "synthetic" cherry-pick
        scheme to create a commit with the given info and a first parent of new_parent. Any
        other parents will be kept from commit_info. Returns the commit hash of the new commit.
        """
        temp_index_path = self.get_scratch_dir() + "/index.temp"
        git_env = {
            "GIT_INDEX_FILE": temp_index_path,
        }
        shutil.copy(f"{self.git_dir}/index", temp_index_path)
        await self.git("reset", "-q", "--no-refresh", parent_tree, "--", ":/", env=git_env)
        success = not (
            await self.sh.piped_sh(
                patch_source,
                [self.git_path, "apply", "--cached", "--3way", "--quiet", "--allow-empty", "-"],
                env2=git_env,
                raiseonerror=False,
            )
        )[0]
        if success:
            new_commit_info = copy.deepcopy(commit_info)
            new_commit_info.tree = GitTreeHash(await self.git_stdout("write-tree", env=git_env))
            new_commit_info.parents[0] = new_parent
            return await self.commit_tree(new_commit_info)
        else:
            conflicts = await self.ls_files(show_conflicts=True, env=git_env)
            if not conflicts:
                logging.info("Add / delete conflicts found!")
                logging.info("Listing conflicting paths isn't implemented for these yet.")
            for _, stage, path in conflicts:
                if stage == 1:
                    logging.info("Conflict in path {}".format(path))
            raise GitConflictException

    async def synthetic_amend(self, commit_info: CommitHeader) -> GitCommitHash:
        """
        Return a commit that contains the contents of the given commit plus the current
        contents of the cache.
        """
        patch_source = (
            [
                self.git_path,
            ]
            + GIT_DIFF_ARGS
            + [
                "--cached",
            ]
        )
        return await self.synthetic_cherry_pick(
            commit_info, commit_info.tree, commit_info.parents[0], patch_source
        )

    async def synthetic_cherry_pick_from_commit(
        self, commit_info: CommitHeader, new_parent: GitCommitHash
    ) -> GitCommitHash:
        """
        Return a commit that contains the contents of the given commit on top of a new parent.
        """
        patch_source = (
            [
                self.git_path,
            ]
            + GIT_DIFF_ARGS
            + [
                commit_info.commit_id + "~",
                commit_info.commit_id,
            ]
        )

        return await self.synthetic_cherry_pick(
            commit_info, GitTreeHash(new_parent), new_parent, patch_source
        )

    async def cherry_pick_from_tree(
        self, commit_info: CommitHeader, new_parent: GitCommitHash
    ) -> GitCommitHash:
        """
        Return a commit that uses the same tree as the given commit, but is on a new parent.
        """
        new_commit_info = copy.deepcopy(commit_info)
        new_commit_info.parents[0] = new_parent
        return await self.commit_tree(new_commit_info)

    async def make_virtual_diff_target(
        self,
        old_base: GitCommitHash,
        old_head: GitCommitHash,
        new_base: GitCommitHash,
        new_head: GitCommitHash,
        parent: Optional[GitCommitHash],
    ) -> GitCommitHash:
        """
        Return a commit (optionally on top of parent) that provides a way to get the diff from old
        head to new head while accounting for the fact that new base might have been rebased since
        old base. This new commit makes an effort to include only files that were actually changed,
        while excluding files that were changed upstream as part of the rebase.

        We do this by resetting any files that changed in the old_base->old_head diff to their
        old_head versions in new_base. The returned tree will thus have the following properties
        when diffed against new_head.

        For files not touched by old or new, ret->new_head won't show any diff. This is primarily
        what allows us to exclude upstream files.
        For files touched by both old and new, ret->new_head will show the entire old_head->new_head
        diff. This will include upstream changes for these files, which are difficult to untangle.
        For files not touched in old but touched by new (regardless of whether it existed in
        old_base), diff will show new_base->new_head.
        For files touched in old but not touched in new, there are 2 cases. If file exists in
        new_base, diff will show old_head->new_base. If file doesn't exist in new_base, diff will
        show old_head->(deleted) which isn't perfect since technically new_base->new_head did not
        delete the file, but probably the least confusing of the alternatives of showing no diff and
        showing the old_head->old_base diff.
        """
        new_index: List[str] = []

        # Transform diff-tree raw output to ls-files style output, taking only the new version
        for m in RE_RAW_DIFF_TREE_LINE.finditer(
            await self.git_stdout("diff-tree", "-r", "--no-commit-id", "--raw", old_base, old_head)
        ):
            new_index.append(f"{m.group('new_mode')} {m.group('new_hash')} 0\t{m.group('path')}")

        temp_index_path = self.get_scratch_dir() + "/index.temp"
        git_env = {
            "GIT_INDEX_FILE": temp_index_path,
        }
        shutil.copy(f"{self.git_dir}/index", temp_index_path)
        await self.git("reset", "-q", "--no-refresh", new_base, "--", ":/", env=git_env)
        await self.git(
            "update-index",
            "--index-info",
            input_str="\n".join(new_index),
            env=git_env,
        )

        tree = GitTreeHash(await self.git_stdout("write-tree", env=git_env))
        new_commit_info = CommitHeader(tree, [parent] if parent else [])
        new_commit_info.commit_msg = (
            f"revup virtual diff target\n\n{old_base}\n{old_head}\n{new_base}\n{new_head}"
        )

        return await self.commit_tree(new_commit_info)
