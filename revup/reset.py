import argparse

from rich import get_console

from revup import git


async def main(_: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Handles the "reset" command.
    Resets the current branch to match the upstream tracking branch.
    """
    # Get the current branch
    current_branch = await git_ctx.git_stdout("branch", "--show-current")
    if not current_branch:
        raise RuntimeError("Not on a branch")

    # Get the upstream tracking branch
    upstream_ref = "@{u}"

    with get_console().status(f"Resetting {current_branch} to {upstream_ref}..."):
        try:
            # Verify the upstream exists
            await git_ctx.git_stdout("rev-parse", "--verify", upstream_ref)

            # Perform the hard reset
            await git_ctx.hard_reset(upstream_ref)

            print(f"Successfully reset {current_branch} to {upstream_ref}")
            return 0
        except RuntimeError as e:
            print(f"Error: {str(e)}")
            return 1
