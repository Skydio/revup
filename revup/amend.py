import argparse
import asyncio
import logging
import re
import subprocess

from revup import git, topic_stack
from revup.types import (
    GitConflictException,
    GitTreeHash,
    RevupConflictException,
    RevupUsageException,
)

RE_TOPIC_WITH_MODIFIERS = re.compile(r"(?P<topic>[a-zA-Z\-_0-9]+)(?P<modifiers>[\^~]+[0-9]*)?")


def invoke_editor_for_commit_msg(
    git_ctx: git.Git, editor: str, topic_summary: str, commit_msg: str, cache_stat: str, stat: str
) -> str:
    """
    Allow the user to modify the given commit msg by opening an editor.
    Stats for the commit are shown in comment lines in the editor.
    Return the final message with comment lines stripped out.
    """
    full_stat = []
    if cache_stat:
        full_stat.append(f"Changes to be committed:\n{cache_stat}")
    if stat:
        full_stat.append(f"Original commit:\n{stat}")
    stat_text = "\n\n".join(full_stat)

    comment_text = f"""\nPlease enter the commit message for your changes. Lines starting
with '#' will be ignored, and an empty message aborts the commit.\n{topic_summary}\n{stat_text}"""
    comment_text = "\n# ".join(comment_text.splitlines())

    with open(git_ctx.get_scratch_dir() + "/COMMIT_EDITMSG", mode="w") as temp_file:
        temp_file.write(f"{commit_msg}\n{comment_text}")

    subprocess.check_call((*(editor.split()), temp_file.name))
    with open(temp_file.name, "r") as editor_file:
        msg = editor_file.read()

    # Strip out comment lines
    return re.sub(r"^\s*#.*$", "", msg, flags=re.M).strip()


async def get_topic_summary(topics: topic_stack.TopicStack) -> str:
    await topics.populate_topics()

    if len(topics.topics) == 0:
        return ""

    topic_lines = "".join([f"  {topic}\n" for topic in reversed(topics.topics.keys())])
    return f"\nTopics found between HEAD and {topics.relative_branch}:\n{topic_lines}"


async def parse_ref_or_topic(
    ref_or_topic: str,
    args: argparse.Namespace,
    git_ctx: git.Git,
    topics: topic_stack.TopicStack,
) -> str:
    """
    Parse and return the hash of the commit that is referred to by the given topic or commit-ish.
    """
    if args.parse_refs:
        if await git_ctx.is_branch_or_commit(ref_or_topic):
            return ref_or_topic

    if args.parse_topics:
        match = RE_TOPIC_WITH_MODIFIERS.match(ref_or_topic)
        if match:
            topic = match.group("topic")
            modifiers = match.group("modifiers") or ""

            await topics.populate_topics()

            if topic in topics.topics:
                ref = topics.topics[topic].original_commits[-1].commit_id + modifiers
                if await git_ctx.is_branch_or_commit(ref):
                    return ref

    if args.parse_refs and args.parse_topics:
        raise RevupUsageException(f"{ref_or_topic} is not a valid topic, commit, or branch name!")
    elif args.parse_refs:
        raise RevupUsageException(f"{ref_or_topic} is not a valid commit or branch name!")
    elif args.parse_topics:
        raise RevupUsageException(f"{ref_or_topic} is not a valid topic!")
    else:
        # It might make more sense to check this above, but if we do mypy thinks we've forgotten a
        # return.
        raise RevupUsageException("Can't have both --no-parse-refs and --no-parse-topics!")


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Amend the given commit and recreate the history on top of that commit to make
    a new head commit with the same tree as the cache. Then, soft reset to that commit.
    The result is that the given commit will be changed, but the cache and working
    tree will not be touched.
    """

    async def get_has_unstaged() -> bool:
        return args.all and await git_ctx.git_return_code("diff", "--quiet") != 0

    has_staged, has_unstaged = await asyncio.gather(
        git_ctx.git_return_code("diff", "--cached", "--quiet"),
        get_has_unstaged(),
    )

    has_diff = has_staged or has_unstaged or args.drop
    if not has_diff and args.no_edit:
        return 0
    if args.insert and args.no_edit:
        raise RevupUsageException("Can't skip wording an inserted commit!")

    if args.drop and args.insert:
        raise RevupUsageException("Doesn't make sense to drop and insert")

    if has_unstaged:
        await git_ctx.git("add", "--update")

    topics = topic_stack.TopicStack(
        git_ctx,
        args.base_branch,
        args.relative_branch,
        None,
        None,
    )
    if args.ref_or_topic:
        commit = await parse_ref_or_topic(args.ref_or_topic, args, git_ctx, topics)

        if not await git_ctx.is_ancestor(f"{commit}~", "HEAD"):
            raise RevupUsageException(
                "Specified commit is not a first parent ancestor of HEAD"
                if commit == args.ref_or_topic
                else (
                    f"Commit ({commit}, from topic {args.ref_or_topic}) is not a first parent"
                    " ancestor of HEAD"
                )
            )
    else:
        commit = "HEAD"

    stack = git.parse_rev_list(
        await git_ctx.rev_list(
            "HEAD", f"{commit}~", header=True, first_parent=True, exclude_first_parent=True
        )
    )
    if len(stack) == 0:
        raise RevupUsageException(f"Couldn't find any commits between HEAD and {commit}~")

    if args.insert:
        # Create a new empty commit after the given commit
        stack[0].parents = [stack[0].commit_id]
        # Clear commit specific fields
        stack[0].author_name = ""
        stack[0].author_email = ""
        stack[0].author_date = ""
        stack[0].committer_name = ""
        stack[0].committer_email = ""
        stack[0].committer_date = ""
        stack[0].commit_msg = ""

    if not args.no_edit and not args.drop:
        new_msg = invoke_editor_for_commit_msg(
            git_ctx,
            git_ctx.editor,
            await get_topic_summary(topics) if args.parse_topics else "",
            stack[0].commit_msg,
            await git_ctx.git_stdout("--no-pager", "diff", "--cached", "--stat", "--no-color")
            if has_diff
            else "",
            ""
            if args.insert
            else await git_ctx.git_stdout(
                "--no-pager", "diff", commit + "~", commit, "--stat", "--no-color"
            ),
        )
        if len(new_msg.strip()) == 0:
            logging.info("Exited due to empty commit message.")
            return 1

        if stack[0].commit_msg == new_msg and not has_diff:
            return 0

        stack[0].commit_msg = new_msg

    if has_diff:
        if not args.drop:
            stack[-1].tree = GitTreeHash(await git_ctx.git_stdout("write-tree"))
        new_commit = stack[0].parents[0]

        for i, commit_obj in enumerate(stack):
            if i == 0 and args.drop:
                # Drop the target commit
                continue
            elif i == 0 and len(stack) > 1:
                # Perform an amend for the first commit, unless there's only one
                # in which case we can use the tree shortcut.
                try:
                    new_commit = await git_ctx.synthetic_amend(commit_obj)
                except GitConflictException as exc:
                    raise RevupConflictException(
                        "Couldn't apply cached changes to\n"
                        f'"{commit_obj.title}" ({commit_obj.commit_id[:8]})\n'
                        "You may need to `git rebase -i` to resolve these conflicts!"
                    ) from exc
            else:
                if i == len(stack) - 1 and not args.drop:
                    # For the final commit (if drop isn't used) we can assume that
                    # the state is the exact same as the original cache, so we
                    # don't actually have to apply a patch.
                    new_commit = await git_ctx.cherry_pick_from_tree(commit_obj, new_commit)
                else:
                    try:
                        new_commit = await git_ctx.synthetic_cherry_pick_from_commit(
                            commit_obj, new_commit
                        )
                    except GitConflictException as exc:
                        raise RevupConflictException(
                            f'Couldn\'t re-apply commit "{commit_obj.title}"'
                            f" ({commit_obj.commit_id[:8]})\nYou may need to `git rebase -i` to"
                            " resolve these conflicts!"
                        ) from exc
    else:
        # If there's no diff (only text changed), its much faster to use the same trees
        new_commit = stack[0].parents[0]
        for stack_entry in stack:
            new_commit = await git_ctx.cherry_pick_from_tree(stack_entry, new_commit)

    git_env = {
        "GIT_REFLOG_ACTION": "reset --soft (revup amend)",
    }
    await git_ctx.git("reset", "--soft", new_commit, env=git_env)
    return 0
