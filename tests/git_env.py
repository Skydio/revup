import asyncio
import functools
import os
import shutil
import stat
import tempfile

from revup import git, shell
from revup.types import GitCommitHash

TEST_AUTHOR_NAME = "Test Author"
TEST_AUTHOR_EMAIL = "test@example.com"
TEST_COMMITTER_NAME = "Test Committer"
TEST_COMMITTER_EMAIL = "committer@example.com"
BASE_TIMESTAMP = 1700000000
TIMESTAMP_INCREMENT = 100


def async_test(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


def make_editor_script(tmp_dir, content):
    path = os.path.join(tmp_dir, "editor.sh")
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\necho "{content}" > "$1"\n')
    os.chmod(path, stat.S_IRWXU)
    return path


def make_empty_editor_script(tmp_dir):
    path = os.path.join(tmp_dir, "editor.sh")
    with open(path, "w") as f:
        f.write('#!/bin/sh\nprintf "" > "$1"\n')
    os.chmod(path, stat.S_IRWXU)
    return path


def make_passthrough_editor_script(tmp_dir):
    path = os.path.join(tmp_dir, "editor.sh")
    with open(path, "w") as f:
        f.write('#!/bin/sh\nhead -1 "$1" > /tmp/_revup_edit_tmp\n' 'mv /tmp/_revup_edit_tmp "$1"\n')
    os.chmod(path, stat.S_IRWXU)
    return path


class GitTestEnvironment:
    def __init__(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="revup_test_")
        self.sh = shell.Shell(quiet=True, cwd=self.tmp_dir)
        self.git_ctx = None
        self.commit_count = 0
        self._git_path = git.get_default_git()

    async def setup(self):
        await self.sh.sh(self._git_path, "init", "--initial-branch=main")

        self.git_ctx = git.Git(
            sh=self.sh,
            git_path=self._git_path,
            remote_name="origin",
            main_branch="main",
            base_branch_globs="",
            keep_temp=False,
        )
        self.git_ctx.repo_root = self.tmp_dir
        self.git_ctx.git_dir = (
            await self.sh.sh(self._git_path, "rev-parse", "--path-format=absolute", "--git-dir")
        )[1].rstrip()
        self.git_ctx.email = TEST_AUTHOR_EMAIL
        self.git_ctx.author = TEST_AUTHOR_EMAIL.split("@")[0]
        self.git_ctx.editor = "true"

    async def __aenter__(self):
        await self.setup()
        return self

    async def __aexit__(self, *args):
        self.cleanup()

    def cleanup(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _next_timestamp(self):
        ts = BASE_TIMESTAMP + (self.commit_count * TIMESTAMP_INCREMENT)
        return f"{ts} +0000"

    async def write_file(self, name, content):
        path = os.path.join(self.tmp_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    async def read_file(self, name):
        path = os.path.join(self.tmp_dir, name)
        with open(path, "r") as f:
            return f.read()

    async def commit(self, message, files=None):
        if files:
            for name, content in files.items():
                await self.write_file(name, content)
                await self.git_ctx.git("add", name)
        ts = self._next_timestamp()
        env = {
            "GIT_AUTHOR_NAME": TEST_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": TEST_AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": ts,
            "GIT_COMMITTER_NAME": TEST_COMMITTER_NAME,
            "GIT_COMMITTER_EMAIL": TEST_COMMITTER_EMAIL,
            "GIT_COMMITTER_DATE": ts,
        }
        await self.git_ctx.git("commit", "-m", message, "--allow-empty", env=env)
        self.commit_count += 1
        return GitCommitHash(await self.git_ctx.git_stdout("rev-parse", "HEAD"))

    async def stage_file(self, name, content):
        await self.write_file(name, content)
        await self.git_ctx.git("add", name)

    async def get_commit_message(self, ref="HEAD"):
        return await self.git_ctx.git_stdout("log", "-1", "--format=%B", ref)

    async def get_commit_hash(self, ref="HEAD"):
        return GitCommitHash(await self.git_ctx.git_stdout("rev-parse", ref))

    async def get_file_at_commit(self, name, ref="HEAD"):
        return await self.git_ctx.git_stdout("show", f"{ref}:{name}")

    async def get_commit_count(self):
        return int(await self.git_ctx.git_stdout("rev-list", "--count", "HEAD"))

    async def get_tree_hash(self, ref="HEAD"):
        return await self.git_ctx.git_stdout("rev-parse", f"{ref}^{{tree}}")

    async def has_staged_changes(self):
        return await self.git_ctx.git_return_code("diff", "--cached", "--quiet") != 0

    async def has_unstaged_changes(self):
        return await self.git_ctx.git_return_code("diff", "--quiet") != 0

    async def get_log_subjects(self):
        output = await self.git_ctx.git_stdout("log", "--format=%s", "--first-parent")
        return [line for line in output.split("\n") if line]

    async def get_staged_files(self):
        output = await self.git_ctx.git_stdout("diff", "--cached", "--name-only")
        return [line for line in output.split("\n") if line]
