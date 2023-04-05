import argparse

from revup import git, topic_stack


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Handles the "restack" command.
    """
    topics = topic_stack.TopicStack(
        git_ctx,
        args.base_branch,
        args.relative_branch,
        None,
        None,
    )

    await topics.populate_topics()

    await topics.restack(args.topicless_last)
    return 0
