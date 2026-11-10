"""
Regression tests for revup.shell's stdin wiring.

`create_sh_task` and `feed_input` used truthiness checks on `input_str` to decide
whether the caller wanted a stdin pipe. That conflates `input_str=None` ("no stdin
redirect requested") with `input_str=""` ("pipe zero bytes, then close stdin",
i.e. send immediate EOF) -- the exact case `git.Git.empty_tree()` relies on when it
calls `git_stdout("mktree", input_str="")`.
"""

import asyncio
import os
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest
from git_env import GitTestEnvironment, async_test

from revup import shell

EMPTY_TREE_HASH = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@async_test
async def test_feed_input_closes_writer_for_empty_string():
    """An empty input_str must still close the writer, giving the child immediate EOF."""
    writer = Mock()
    writer.drain = AsyncMock()

    await shell.feed_input(writer, "")

    writer.write.assert_not_called()
    writer.drain.assert_not_called()
    writer.close.assert_called_once()


@async_test
async def test_feed_input_writes_and_closes_for_nonempty_string():
    writer = Mock()
    writer.drain = AsyncMock()

    await shell.feed_input(writer, "hello")

    writer.write.assert_called_once_with(b"hello")
    writer.drain.assert_awaited_once()
    writer.close.assert_called_once()


@async_test
async def test_feed_input_noop_without_writer():
    # Must not raise even though input_str is set, since there's no writer to feed.
    await shell.feed_input(None, "hello")


@async_test
async def test_create_sh_task_pipes_stdin_for_empty_input_str(mocker):
    """
    input_str="" must still cause stdin=subprocess.PIPE to be passed to the child,
    not stdin=None (which would make the child inherit the real parent stdin).
    """
    fake_proc = Mock()

    mock_create = mocker.patch(
        "revup.shell.asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)
    )

    sh = shell.Shell(cwd="/tmp")
    coros = await sh.create_sh_task("some-cmd", input_str="")
    for coro in coros:
        coro.close()

    assert mock_create.call_args.kwargs["stdin"] == subprocess.PIPE


@async_test
async def test_create_sh_task_rejects_both_stdin_and_empty_input_str():
    """
    The guard against passing both `stdin` and `input_str` must also use `is not
    None`, so it actually catches `stdin=subprocess.PIPE, input_str=""` -- a
    truthiness check can't, since `""` is falsy.
    """
    sh = shell.Shell(cwd="/tmp")
    with pytest.raises(AssertionError):
        await sh.create_sh_task("true", stdin=subprocess.PIPE, input_str="")


@async_test
async def test_empty_tree_does_not_depend_on_inherited_stdin():
    """
    End-to-end regression test for git.Git.empty_tree(), which calls
    git_stdout("mktree", input_str="") expecting `git mktree` to see immediate EOF
    on stdin and produce the well-known empty tree hash.

    Redirect this process's real fd 0 to the read end of a pipe we never write to
    or close before the call. Under the old buggy code, create_sh_task launches
    `git mktree` with stdin=None, so it inherits that fd 0 and blocks forever
    waiting for input that never arrives (exactly what happens to `revup amend
    --last-touched` at a real interactive terminal). Under the fix, `git mktree`
    gets its own pipe that is closed immediately, regardless of what fd 0 is doing.
    """
    async with GitTestEnvironment() as env:
        read_fd, write_fd = os.pipe()
        saved_stdin_fd = os.dup(0)
        try:
            os.dup2(read_fd, 0)
            os.close(read_fd)

            result = await asyncio.wait_for(env.git_ctx.empty_tree(), timeout=5)
            assert result == EMPTY_TREE_HASH
        finally:
            os.dup2(saved_stdin_fd, 0)
            os.close(saved_stdin_fd)
            # Unblock (and let exit) any leftover child that inherited the pipe,
            # in case the call above hung and we're only here via a timeout.
            os.close(write_fd)
