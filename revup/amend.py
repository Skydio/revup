import argparse
import asyncio
import logging
import re
import shlex
import subprocess
from collections import defaultdict
from typing import Dict, List, Set

from revup import git, topic_stack
from revup.types import (
    CommitHeader,
    GitCommitHash,
    GitConflictException,
    GitTreeHash,
    RevupConflictException,
    RevupUsageException,
)

RE_TOPIC_WITH_MODIFIERS = re.compile(r"(?P<topic>[a-zA-Z\-_0-9]+)(?P<modifiers>[\^~]+[0-9]*)?")

CLEANUP_SCISSOR_LINE = r"------------------------ >8 ------------------------"
CLEANUP_SCISSOR_COMMENT = """Do not modify or remove the line above.
Everything below it will be ignored."""
CLEANUP_STRIP_COMMENT = """Please enter the commit message for your changes. Lines starting
with '{}' will be ignored, and an empty message aborts the amend."""


async def invoke_editor_for_commit_msg(
    git_ctx: git.Git,
    editor: str,
    topic_summary: str,
    commit_msg: str,
    cache_stat: str,
    stat: str,
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

    # Respect the configured option for commit msg cleanup.
    cleanup_ret, cleanup_type = await git_ctx.git("config", "commit.cleanup", raiseonerror=False)
    if cleanup_ret != 0:
        # git's default if the message is being edited (which if we've reached this it is)
        cleanup_type = "strip"

    # Respect the configured comment character
    comment_ret, comment_char = await git_ctx.git("config", "core.commentChar", raiseonerror=False)
    if comment_ret != 0:
        comment_char = "#"

    comments = f"{topic_summary}\n{stat_text}"
    if cleanup_type == "scissors":
        comments = f"\n{CLEANUP_SCISSOR_LINE}\n{CLEANUP_SCISSOR_COMMENT}\n{comments}"
    elif cleanup_type == "strip":
        comments = f"\n{CLEANUP_STRIP_COMMENT.format(comment_char)}\n{comments}"

    comments = "\n{} ".format(comment_char).join(comments.splitlines())

    with open(git_ctx.get_scratch_dir() + "/COMMIT_EDITMSG", mode="w") as temp_file:
        temp_file.write(f"{commit_msg}\n{comments}")

    subprocess.check_call((*shlex.split(editor), temp_file.name))
    with open(temp_file.name, "r") as editor_file:
        msg = editor_file.read()

    if cleanup_type == "strip":
        # Strip out comment lines
        msg = re.sub(r"^{}.*$\n?".format(comment_char), "", msg, flags=re.M)
    elif cleanup_type == "scissors":
        msg = msg.split(f"{comment_char} {CLEANUP_SCISSOR_LINE}")[0]

    if cleanup_type != "verbatim":
        # Match behavior of git, which will trim all trailing whitespace
        msg = re.sub(r"[ \t]+$", "", msg, flags=re.M)
        # collapse consecutive empty lines
        msg = re.sub(r"[\n]{3,}", "\n\n", msg)
        # and remove all leading and trailing whitespace and newlines
        msg = msg.strip()

    return msg


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


async def replay_cherry_pick(
    git_ctx: git.Git, commit_obj: CommitHeader, new_parent: GitCommitHash
) -> GitCommitHash:
    try:
        return await git_ctx.synthetic_cherry_pick_from_commit(commit_obj, new_parent)
    except GitConflictException as exc:
        await git_ctx.dump_conflict(exc)
        raise RevupConflictException(
            commit_obj,
            new_parent,
            "You may need to `git rebase -i` to resolve these conflicts!",
        ) from exc


async def rebuild_stack_last_touched(
    git_ctx: git.Git, stack: List[CommitHeader], staged_files: Set[str]
) -> GitCommitHash:
    """
    Rebuild the stack, amending each staged file into the most recent commit that touched it.
    Returns the new HEAD commit.
    """
    # Map each staged file to its most recent commit in the stack (ordered oldest-first)
    file_to_commit: Dict[str, int] = {}
    for i, commit_obj in enumerate(stack):
        touched = await git_ctx.git_stdout(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "--no-renames",
            commit_obj.commit_id,
        )
        for f in touched.split("\n"):
            if f and f in staged_files:
                file_to_commit[f] = i

    commit_to_files: Dict[int, Set[str]] = defaultdict(set)
    for f, idx in file_to_commit.items():
        commit_to_files[idx].add(f)

    files_to_amend = set(file_to_commit.keys())
    if not files_to_amend:
        return GitCommitHash("")

    # Get index entries for each staged file (format: <mode> <blob> <stage>\t<path>)
    staged_entries: Dict[str, str] = {}
    ls_output = await git_ctx.git_stdout("ls-files", "--stage", "--", *files_to_amend)
    for line in ls_output.split("\n"):
        if not line:
            continue
        _, path = line.split("\t", 1)
        staged_entries[path] = line

    tmp_index = git_ctx.get_scratch_dir() + "/last_touched_index"

    new_commit = GitCommitHash(stack[0].parents[0]) if stack[0].parents else GitCommitHash("")
    for i, commit_obj in enumerate(stack):
        if i in commit_to_files:
            idx_env = {"GIT_INDEX_FILE": tmp_index}
            await git_ctx.git("read-tree", commit_obj.tree, env=idx_env)
            update_lines = [staged_entries[f] for f in commit_to_files[i]]
            await git_ctx.git(
                "update-index",
                "--index-info",
                env=idx_env,
                input_str="\n".join(update_lines) + "\n",
            )
            new_tree = GitTreeHash(await git_ctx.git_stdout("write-tree", env=idx_env))
            amended = CommitHeader(new_tree, [new_commit])
            amended.author_name = commit_obj.author_name
            amended.author_email = commit_obj.author_email
            amended.author_date = commit_obj.author_date
            amended.committer_name = commit_obj.committer_name
            amended.committer_email = commit_obj.committer_email
            # Refresh committer date, like git commit --amend.
            amended.committer_date = ""
            amended.commit_msg = commit_obj.commit_msg
            new_commit = await git_ctx.commit_tree(amended)
        else:
            new_commit = await replay_cherry_pick(git_ctx, commit_obj, new_commit)

    return new_commit


async def main(args: argparse.Namespace, git_ctx: git.Git) -> int:
    """
    Amend the given commit and recreate the history on top of that commit to make
    a new head commit with the same tree as the cache. Then, soft reset to that commit.
    The result is that the given commit will be changed, but the cache and working
    tree will not be touched.
    """

    async def get_has_unstaged() -> bool:
        return args.all and await git_ctx.git_return_code("diff", "--no-renames", "--quiet") != 0

    has_staged, has_unstaged = await asyncio.gather(
        git_ctx.git_return_code("diff", "--cached", "--no-renames", "--quiet"),
        get_has_unstaged(),
    )

    args.edit = args.edit or args.insert
    has_diff = has_staged or has_unstaged or args.drop
    if not has_diff and not args.edit and not args.last_touched:
        return 0

    if args.drop and args.insert:
        raise RevupUsageException("Doesn't make sense to drop and insert")

    if args.last_touched and (args.insert or args.drop):
        raise RevupUsageException("--last-touched is mutually exclusive with --insert and --drop")

    if args.last_touched and args.ref_or_topic:
        raise RevupUsageException(
            "--last-touched is mutually exclusive with providing a ref_or_topic"
        )

    if args.last_touched and not args.parse_topics:
        raise RevupUsageException("--last-touched requires --parse-topics")

    if has_unstaged:
        await git_ctx.git("add", "--update")

    topics = topic_stack.TopicStack(
        git_ctx,
        args.base_branch,
        args.relative_branch,
    )

    if args.last_touched:
        staged_files_output = await git_ctx.git_stdout(
            "diff", "--cached", "--name-only", "--no-renames"
        )
        if not staged_files_output:
            return 0

        await topics.populate_topics()
        if not topics.commits:
            return 0

        fork = await git_ctx.fork_point("HEAD", topics.relative_branch)
        stack = git.parse_rev_list(
            await git_ctx.rev_list(
                "HEAD", fork, header=True, first_parent=True, exclude_first_parent=True
            )
        )
        if not stack:
            return 0

        staged_files = set(staged_files_output.split("\n"))
        new_commit = await rebuild_stack_last_touched(git_ctx, stack, staged_files)
        if not new_commit:
            return 0

        await git_ctx.soft_reset(new_commit, {"GIT_REFLOG_ACTION": "revup amend --last-touched"})
        return 0

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
            "HEAD",
            f"{commit}~",
            header=True,
            first_parent=True,
            exclude_first_parent=True,
        )
    )
    if len(stack) == 0:
        raise RevupUsageException(f"Couldn't find any commits between HEAD and {commit}~")

    # Refresh the amended commit's committer date, like git commit --amend.
    stack[0].committer_date = ""

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

    if args.edit and not args.drop:
        new_msg = await invoke_editor_for_commit_msg(
            git_ctx,
            git_ctx.editor,
            await get_topic_summary(topics) if args.parse_topics else "",
            stack[0].commit_msg,
            (
                await git_ctx.git_stdout("--no-pager", "diff", "--cached", "--stat", "--no-color")
                if has_diff
                else ""
            ),
            (
                ""
                if args.insert
                else await git_ctx.git_stdout(
                    "--no-pager", "diff", commit + "~", commit, "--stat", "--no-color"
                )
            ),
        )
        if len(new_msg.strip()) == 0:
            logging.info("Exited due to empty commit message.")
            return 1

        if stack[0].commit_msg == new_msg and not has_diff:
            return 0

        stack[0].commit_msg = new_msg

    if has_diff:
        new_commit = stack[0].parents[0]
        if not args.drop:
            stack[-1].tree = GitTreeHash(await git_ctx.git_stdout("write-tree"))
        for i, commit_obj in enumerate(stack):
            if i == 0 and args.drop:
                # Drop the target commit
                continue
            elif i == 0 and len(stack) > 1:
                # Perform an amend for the first commit, unless there's only one
                # in which case we can use the tree shortcut.
                temp_commit = CommitHeader(stack[-1].tree, [git.HEAD_COMMIT])
                temp_commit.title = temp_commit.commit_msg = "cached changes"
                temp_commit.commit_id = await git_ctx.commit_tree(temp_commit)
                # drop must be false, so this will be the result of write-tree from above
                stack[-1].tree = temp_commit.tree
                try:
                    new_commit = await git_ctx.synthetic_amend(commit_obj, temp_commit)
                except GitConflictException as exc:
                    await git_ctx.dump_conflict(exc)
                    raise RevupConflictException(
                        temp_commit,
                        commit_obj.commit_id,
                        "You may need to `git rebase -i` to resolve these conflicts!",
                    ) from exc
            else:
                if i == len(stack) - 1 and not args.drop:
                    # For the final commit (if drop isn't used) we can assume that
                    # the state is the exact same as the original cache, so we
                    # don't actually have to apply a patch.
                    new_commit = await git_ctx.cherry_pick_from_tree(commit_obj, new_commit)
                else:
                    new_commit = await replay_cherry_pick(git_ctx, commit_obj, new_commit)
    else:
        # If there's no diff (only text changed), its much faster to use the same trees
        new_commit = stack[0].parents[0]
        for stack_entry in stack:
            new_commit = await git_ctx.cherry_pick_from_tree(stack_entry, new_commit)

    reflog_action_str = 'revup amend {}{}: "{}"'.format(
        "--drop " if args.drop else "--insert " if args.insert else "",
        stack[0].commit_id[:8],
        stack[0].commit_msg.splitlines()[0][:40],
    )
    git_env = {
        "GIT_REFLOG_ACTION": reflog_action_str,
    }
    await git_ctx.soft_reset(new_commit, git_env)
    return 0
