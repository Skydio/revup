import asyncio
import logging
import os
import shlex
import subprocess
import sys
import time
from typing import (
    IO,
    Any,
    Coroutine,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

_HANDLE = Union[None, int, IO[Any]]


def log_command(args: Sequence[str]) -> None:
    """
    Given a command, print it in a both machine and human readable way.

    Args:
        *args: the list of command line arguments you want to run
        env: the dictionary of environment variable settings for the command
    """
    logging.debug("$ {}".format(" ".join(shlex.quote(arg) for arg in args)))


K = TypeVar("K")


V = TypeVar("V")


def merge_dicts(x: Dict[K, V], y: Dict[K, V]) -> Dict[K, V]:
    z = x.copy()
    z.update(y)
    return z


async def process_stream(
    proc_stream: Optional[asyncio.StreamReader], setting: _HANDLE, default_stream: IO[str]
) -> bytes:
    # The things we do for logging...
    #
    # - I didn't make a PTY, so programs are going to give
    #   output assuming there isn't a terminal at the other
    #   end.  This is less nice for direct terminal use, but
    #   it's better for logging (since we get to dispense
    #   with the control codes).
    #
    # - We assume line buffering.  This is kind of silly but
    #   we need to assume *some* sort of buffering with the
    #   stream API.
    output = []
    if proc_stream is None:
        return b""
    while True:
        try:
            line = await proc_stream.readuntil()
        except asyncio.LimitOverrunError as e:
            line = await proc_stream.readexactly(e.consumed)
        except asyncio.IncompleteReadError as e:
            line = e.partial
        if not line:
            if isinstance(setting, int) and setting != -1:
                os.close(setting)
            break
        if setting == subprocess.PIPE:
            output.append(line)
        elif setting == subprocess.STDOUT:
            sys.stdout.buffer.write(line)
        elif isinstance(setting, int) and setting != -1:
            os.write(setting, line)
        elif setting is None:
            # See https://stackoverflow.com/questions/55681488
            default_stream.write(line.decode("utf-8"))
        elif isinstance(setting, IO):
            # don't use setting.write directly, that will
            # not properly handle binary.  This gives us
            # "parity" with the normal subprocess implementation
            os.write(setting.fileno(), line)
    return b"".join(output)


async def feed_input(
    stdin_writer: Optional[asyncio.StreamWriter], input_str: Optional[str]
) -> None:
    if stdin_writer is None:
        return
    if not input_str:
        return
    stdin_writer.write(input_str.encode("utf-8"))
    await stdin_writer.drain()
    stdin_writer.close()


class Shell:
    """
    An object representing a shell (e.g., the bash prompt in your
    terminal), maintaining a concept of current working directory, and
    also the necessary accoutrements for testing.
    """

    # Current working directory of shell.
    cwd: str

    def __init__(
        self,
        quiet: bool = True,
        cwd: Optional[str] = None,
    ):
        """
        Args:
            cwd: Current working directory of the shell.  Pass None to
                initialize to the current cwd of the current process.
            quiet: If True, suppress printing out the command executed
                by the shell.  By default, we print out commands for ease
                of debugging.  Quiet is most useful for non-mutating
                shell commands.
        """
        self.quiet = quiet
        self.cwd = cwd if cwd else os.getcwd()

    async def create_sh_task(
        self,
        *args: str,  # noqa: C901
        env: Optional[Dict[str, str]] = None,
        stderr: _HANDLE = None,
        # TODO: Arguably bytes should be accepted here too
        input_str: Optional[str] = None,
        stdin: _HANDLE = None,
        stdout: _HANDLE = subprocess.PIPE,
    ) -> Tuple[
        Coroutine[Any, Any, None],
        Coroutine[Any, Any, bytes],
        Coroutine[Any, Any, bytes],
        Coroutine[Any, Any, int],
    ]:
        assert not (stdin and input_str)
        if input_str:
            stdin = subprocess.PIPE

        if env is not None:
            env = merge_dicts(dict(os.environ), env)

        proc = asyncio.create_subprocess_exec(
            *args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
        )

        ret = await proc

        return (
            feed_input(ret.stdin, input_str),
            process_stream(ret.stdout, stdout, sys.stdout),
            process_stream(ret.stderr, stderr, sys.stderr),
            ret.wait(),
        )

    async def sh(
        self,
        *args: str,  # noqa: C901
        env: Optional[Dict[str, str]] = None,
        stderr: _HANDLE = None,
        # TODO: Arguably bytes should be accepted here too
        input_str: Optional[str] = None,
        stdin: _HANDLE = None,
        stdout: _HANDLE = subprocess.PIPE,
        raiseonerror: bool = True,
    ) -> Tuple[int, str]:
        """
        Run a command specified by args, and return string representing
        the stdout of the run command, raising an error if exit code
        was nonzero.

        Args:
            *args: the list of command line arguments to run
            env: any extra environment variables to set when running the
                command.  Environment variables set this way are ADDITIVE
                (unlike subprocess default)
            stderr: where to pipe stderr; by default, we pipe it straight
                to this process's stderr
            input: string value to pass stdin.  This is mutually exclusive
                with stdin
            stdin: where to pipe stdin from.  This is mutually exclusive
                with input
            stdout: where to pipe stdout; by default, we capture the stdout
                and return it
            raiseonerror: whether to raise an error if return value is not 0
        """
        if not self.quiet:
            log_command(args)
        start_time = time.time()
        tasks = await self.create_sh_task(
            *args,
            env=env,
            stderr=stderr,
            input_str=input_str,
            stdin=stdin,
            stdout=stdout,
        )

        _, out, err, ret = await asyncio.gather(*tasks)

        ret = self.handle_sh_results(ret, out, err, stdout, raiseonerror, *args)
        if not self.quiet:
            logging.debug("Took {}s".format(time.time() - start_time))
        return ret

    async def piped_sh(
        self,
        args1: List[str],
        args2: List[str],
        env1: Optional[Dict[str, str]] = None,
        env2: Optional[Dict[str, str]] = None,
        stderr: _HANDLE = None,
        # TODO: Arguably bytes should be accepted here too
        input_str: Optional[str] = None,
        stdin: _HANDLE = None,
        stdout: _HANDLE = subprocess.PIPE,
        raiseonerror: bool = True,
    ) -> Tuple[int, str]:
        start_time = time.time()
        read, write = os.pipe()
        log_args = args1 + ["|"] + args2
        if not self.quiet:
            log_command(log_args)

        tasks = await asyncio.gather(
            self.create_sh_task(
                *args1, env=env1, stderr=stderr, input_str=input_str, stdin=stdin, stdout=write
            ),
            self.create_sh_task(*args2, env=env2, stderr=stderr, stdin=read, stdout=stdout),
        )

        _, _, err1, ret1, _, out2, err2, ret2 = await asyncio.gather(*tasks[0], *tasks[1])

        ret = self.handle_sh_results(
            ret2 if ret1 == 0 else ret1, out2, err1 + err2, stdout, raiseonerror, *log_args
        )
        os.close(read)
        if not self.quiet:
            logging.debug("Took {}s".format(time.time() - start_time))
        return ret

    def handle_sh_results(
        self,
        returncode: int,
        out: bytes,
        err: bytes,
        stdout: _HANDLE,
        raiseonerror: bool,
        *args: str,
    ) -> Tuple[int, str]:
        if returncode and err:
            logging.warning(err.decode(errors="backslashreplace"))
        elif not self.quiet and err:
            logging.debug("# stderr:\n{}".format(err.decode(errors="backslashreplace")))
        if not self.quiet and out:
            logging.debug(
                "{}{}".format(
                    ("# stdout:\n" if err else ""),
                    out.decode(errors="backslashreplace").replace("\0", "\\0"),
                )
            )

        if returncode != 0 and raiseonerror:
            raise RuntimeError("{} failed with exit code {}".format(" ".join(args), returncode))

        if stdout == subprocess.PIPE:
            return (returncode, out.decode())  # do a strict decode for actual return
        else:
            return (returncode, "")

    def open(self, fn: str, mode: str) -> IO[Any]:
        """
        Open a file, relative to the current working directory.

        Args:
            fn: filename to open
            mode: mode to open the file as
        """
        return open(os.path.join(self.cwd, fn), mode)

    def cd(self, d: str) -> None:
        """
        Change the current working directory.

        Args:
            d: directory to change to
        """
        self.cwd = os.path.join(self.cwd, d)

    def wait_for_confirmation(self) -> int:
        """
        Block until the user presses enter. Return whether the operation should continue.
        """
        try:
            input("Press <Enter> to continue or <Ctrl-C> to quit")
        except KeyboardInterrupt:
            return 1
        return 0
