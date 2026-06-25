import argparse

import pytest
from git_env import (
    GitTestEnvironment,
    async_test,
    make_editor_script,
    make_empty_editor_script,
    make_passthrough_editor_script,
)

from revup import amend
from revup.types import RevupConflictException, RevupUsageException


def make_amend_args(**kwargs):
    defaults = {
        "ref_or_topic": None,
        "edit": False,
        "insert": False,
        "drop": False,
        "last_touched": False,
        "all": False,
        "base_branch": None,
        "relative_branch": None,
        "parse_topics": False,
        "parse_refs": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestAmendNoChanges:
    @async_test
    async def test_noop_when_no_staged_no_edit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("initial", {"a.txt": "hello"})
            orig_hash = await env.get_commit_hash()

            args = make_amend_args(edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_hash() == orig_hash


class TestAmendHead:
    @async_test
    async def test_amend_staged_changes_to_head(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "line1"})
            await env.commit("second", {"b.txt": "line2"})

            await env.stage_file("b.txt", "modified")
            args = make_amend_args(edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("b.txt") == "modified"
            msg = await env.get_commit_message()
            assert msg.strip() == "second"
            assert await env.get_commit_count() == 3

    @async_test
    async def test_amend_head_preserves_other_files(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "aaa"})
            await env.commit("second", {"b.txt": "bbb"})

            await env.stage_file("b.txt", "new_b")
            args = make_amend_args(edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt") == "aaa"
            assert await env.get_file_at_commit("b.txt") == "new_b"

    @async_test
    async def test_amend_head_adds_new_file(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("initial", {"a.txt": "aaa"})

            await env.stage_file("c.txt", "new_file")
            args = make_amend_args(edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("c.txt") == "new_file"
            assert await env.get_file_at_commit("a.txt") == "aaa"
            assert await env.get_commit_count() == 2


class TestAmendPriorCommit:
    @async_test
    async def test_amend_first_commit_in_stack_of_two(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "original_a"})
            await env.commit("second", {"b.txt": "original_b"})

            await env.stage_file("a.txt", "modified_a")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt", "HEAD~1") == "modified_a"
            assert await env.get_file_at_commit("b.txt") == "original_b"
            subjects = await env.get_log_subjects()
            assert subjects == ["second", "first", "root"]

    @async_test
    async def test_amend_middle_commit_in_stack_of_three(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            await env.commit("third", {"c.txt": "c"})

            await env.stage_file("b.txt", "b_modified")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("b.txt", "HEAD~1") == "b_modified"
            assert await env.get_file_at_commit("a.txt", "HEAD~2") == "a"
            assert await env.get_file_at_commit("c.txt") == "c"
            subjects = await env.get_log_subjects()
            assert subjects == ["third", "second", "first", "root"]

    @async_test
    async def test_amend_preserves_commit_messages(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first commit msg", {"a.txt": "a"})
            await env.commit("second commit msg", {"b.txt": "b"})
            await env.commit("third commit msg", {"c.txt": "c"})

            await env.stage_file("a.txt", "a_mod")
            args = make_amend_args(ref_or_topic="HEAD~2", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert (await env.get_commit_message("HEAD~2")).strip() == "first commit msg"
            assert (await env.get_commit_message("HEAD~1")).strip() == "second commit msg"
            assert (await env.get_commit_message()).strip() == "third commit msg"


class TestAmendDrop:
    @async_test
    async def test_drop_head_commit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})

            args = make_amend_args(drop=True, edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 2
            assert (await env.get_commit_message()).strip() == "first"
            assert await env.has_staged_changes()

    @async_test
    async def test_drop_head_leaves_changes_in_cache(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b_from_second"})

            args = make_amend_args(drop=True, edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.has_staged_changes()
            staged = await env.get_staged_files()
            assert "b.txt" in staged

    @async_test
    async def test_drop_middle_commit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            await env.commit("third", {"c.txt": "c"})

            args = make_amend_args(ref_or_topic="HEAD~1", drop=True, edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 3
            subjects = await env.get_log_subjects()
            assert subjects == ["third", "first", "root"]
            assert await env.get_file_at_commit("c.txt") == "c"
            assert await env.get_file_at_commit("a.txt") == "a"
            assert await env.has_staged_changes()

    @async_test
    async def test_drop_first_commit_in_stack(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            await env.commit("third", {"c.txt": "c"})

            args = make_amend_args(ref_or_topic="HEAD~2", drop=True, edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 3
            subjects = await env.get_log_subjects()
            assert subjects == ["third", "second", "root"]
            assert await env.has_staged_changes()

    @async_test
    async def test_drop_and_insert_raises(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})

            args = make_amend_args(drop=True, insert=True)
            with pytest.raises(RevupUsageException, match="drop and insert"):
                await amend.main(args, env.git_ctx)


class TestAmendInsert:
    @async_test
    async def test_insert_empty_commit_after_head(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})

            args = make_amend_args(ref_or_topic="HEAD", insert=True, edit=False)
            env.git_ctx.editor = "true"
            ret = await amend.main(args, env.git_ctx)

            # Editor 'true' writes nothing, so the message is empty -> returns 1
            assert ret == 1

    @async_test
    async def test_insert_with_staged_changes(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})

            await env.stage_file("c.txt", "new_c")

            env.git_ctx.editor = make_editor_script(env.tmp_dir, "inserted commit")
            args = make_amend_args(ref_or_topic="HEAD", insert=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 4
            assert (await env.get_commit_message()).strip() == "inserted commit"
            assert await env.get_file_at_commit("c.txt") == "new_c"

    @async_test
    async def test_insert_after_earlier_commit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            await env.commit("third", {"c.txt": "c"})

            await env.stage_file("extra.txt", "extra")

            env.git_ctx.editor = make_editor_script(env.tmp_dir, "inserted")
            args = make_amend_args(ref_or_topic="HEAD~2", insert=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 5
            subjects = await env.get_log_subjects()
            assert subjects[0] == "third"
            assert subjects[1] == "second"
            assert subjects[2] == "inserted"
            assert subjects[3] == "first"
            assert subjects[4] == "root"

    @async_test
    async def test_insert_implies_edit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})

            env.git_ctx.editor = make_editor_script(env.tmp_dir, "new commit")
            # insert=True should set edit=True inside amend.main
            args = make_amend_args(insert=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert (await env.get_commit_message()).strip() == "new commit"


class TestAmendAll:
    @async_test
    async def test_all_flag_stages_modified_files(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "original"})

            await env.write_file("a.txt", "modified_content")
            assert not await env.has_staged_changes()
            assert await env.has_unstaged_changes()

            args = make_amend_args(edit=False, **{"all": True})
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt") == "modified_content"

    @async_test
    async def test_all_flag_does_not_stage_new_untracked_files(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})

            await env.write_file("untracked.txt", "untracked")
            assert not await env.has_staged_changes()

            args = make_amend_args(edit=False, **{"all": True})
            ret = await amend.main(args, env.git_ctx)

            # No tracked file was modified, so nothing to amend
            assert ret == 0

    @async_test
    async def test_all_with_amend_target(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})

            await env.write_file("a.txt", "a_modified")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False, **{"all": True})
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt", "HEAD~1") == "a_modified"
            assert await env.get_file_at_commit("b.txt") == "b"


class TestAmendCachePreservation:
    @async_test
    async def test_working_tree_not_modified(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})

            await env.write_file("untracked.txt", "untracked")
            await env.stage_file("b.txt", "staged_b")

            args = make_amend_args(edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            content = await env.read_file("untracked.txt")
            assert content == "untracked"
            content = await env.read_file("b.txt")
            assert content == "staged_b"

    @async_test
    async def test_cache_preserved_after_amend_prior_commit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            await env.commit("third", {"c.txt": "c"})

            await env.stage_file("a.txt", "modified_a")
            args = make_amend_args(ref_or_topic="HEAD~2", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert not await env.has_staged_changes()


class TestAmendRefParsing:
    @async_test
    async def test_invalid_ref_raises(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})

            await env.stage_file("a.txt", "mod")
            args = make_amend_args(ref_or_topic="nonexistent_ref", edit=False)
            with pytest.raises(RevupUsageException, match="not a valid"):
                await amend.main(args, env.git_ctx)

    @async_test
    async def test_commit_hash_ref(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            first_hash = await env.get_commit_hash()
            await env.commit("second", {"b.txt": "b"})

            await env.stage_file("a.txt", "modified")
            args = make_amend_args(ref_or_topic=first_hash, edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt", "HEAD~1") == "modified"

    @async_test
    async def test_no_parse_refs_and_no_parse_topics_raises(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})

            await env.stage_file("a.txt", "mod")
            args = make_amend_args(
                ref_or_topic="HEAD",
                edit=False,
                parse_refs=False,
                parse_topics=False,
            )
            with pytest.raises(RevupUsageException, match="--no-parse-refs.*--no-parse-topics"):
                await amend.main(args, env.git_ctx)

    @async_test
    async def test_no_parse_refs_only_raises(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})

            # Topic parsing requires a remote base branch to exist
            await env.git_ctx.git("branch", "origin/main", "HEAD~1")

            await env.stage_file("a.txt", "mod")
            args = make_amend_args(
                ref_or_topic="HEAD",
                edit=False,
                parse_refs=False,
                parse_topics=True,
            )
            with pytest.raises(RevupUsageException, match="not a valid topic"):
                await amend.main(args, env.git_ctx)


class TestAmendEdit:
    @async_test
    async def test_edit_reword_commit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("original message", {"a.txt": "a"})

            env.git_ctx.editor = make_editor_script(env.tmp_dir, "reworded message")
            args = make_amend_args(edit=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            msg = await env.get_commit_message()
            assert msg.strip() == "reworded message"

    @async_test
    async def test_edit_empty_message_aborts(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("original message", {"a.txt": "a"})
            orig_hash = await env.get_commit_hash()

            env.git_ctx.editor = make_empty_editor_script(env.tmp_dir)
            args = make_amend_args(edit=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 1
            assert await env.get_commit_hash() == orig_hash

    @async_test
    async def test_edit_with_staged_changes(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("original", {"a.txt": "a"})

            await env.stage_file("a.txt", "modified_a")

            env.git_ctx.editor = make_editor_script(env.tmp_dir, "new message with changes")
            args = make_amend_args(edit=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            msg = await env.get_commit_message()
            assert msg.strip() == "new message with changes"
            assert await env.get_file_at_commit("a.txt") == "modified_a"

    @async_test
    async def test_edit_no_message_change_no_diff_is_noop(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("the message", {"a.txt": "a"})
            orig_hash = await env.get_commit_hash()

            env.git_ctx.editor = make_passthrough_editor_script(env.tmp_dir)
            args = make_amend_args(edit=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_hash() == orig_hash

    @async_test
    async def test_edit_reword_earlier_commit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            await env.commit("third", {"c.txt": "c"})

            env.git_ctx.editor = make_editor_script(env.tmp_dir, "reworded first")
            args = make_amend_args(ref_or_topic="HEAD~2", edit=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert (await env.get_commit_message("HEAD~2")).strip() == "reworded first"
            assert (await env.get_commit_message("HEAD~1")).strip() == "second"
            assert (await env.get_commit_message()).strip() == "third"


class TestAmendConflict:
    @async_test
    async def test_conflict_raises(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "line1\nline2\nline3\n"})
            await env.commit("second", {"a.txt": "line1\nchanged_by_second\nline3\n"})

            await env.stage_file("a.txt", "line1\nconflicting_change\nline3\n")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False)

            with pytest.raises(RevupConflictException):
                await amend.main(args, env.git_ctx)


class TestAmendMultipleFiles:
    @async_test
    async def test_amend_multiple_files_to_earlier_commit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a", "b.txt": "b"})
            await env.commit("second", {"c.txt": "c"})

            await env.stage_file("a.txt", "a_mod")
            await env.stage_file("b.txt", "b_mod")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt", "HEAD~1") == "a_mod"
            assert await env.get_file_at_commit("b.txt", "HEAD~1") == "b_mod"
            assert await env.get_file_at_commit("c.txt") == "c"

    @async_test
    async def test_amend_with_subdirectory_files(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"dir/sub.txt": "sub"})
            await env.commit("second", {"other.txt": "other"})

            await env.stage_file("dir/sub.txt", "sub_modified")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("dir/sub.txt", "HEAD~1") == "sub_modified"


class TestAmendDeepStack:
    @async_test
    async def test_amend_bottom_of_deep_stack(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("c1", {"f1.txt": "1"})
            await env.commit("c2", {"f2.txt": "2"})
            await env.commit("c3", {"f3.txt": "3"})
            await env.commit("c4", {"f4.txt": "4"})
            await env.commit("c5", {"f5.txt": "5"})

            await env.stage_file("f1.txt", "1_mod")
            args = make_amend_args(ref_or_topic="HEAD~4", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 6
            assert await env.get_file_at_commit("f1.txt", "HEAD~4") == "1_mod"
            for i in range(2, 6):
                assert await env.get_file_at_commit(f"f{i}.txt") is not None
            subjects = await env.get_log_subjects()
            assert subjects == ["c5", "c4", "c3", "c2", "c1", "root"]

    @async_test
    async def test_drop_from_deep_stack(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("c1", {"f1.txt": "1"})
            await env.commit("c2", {"f2.txt": "2"})
            await env.commit("c3", {"f3.txt": "3"})
            await env.commit("c4", {"f4.txt": "4"})

            args = make_amend_args(ref_or_topic="HEAD~2", drop=True, edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 4
            subjects = await env.get_log_subjects()
            assert subjects == ["c4", "c3", "c1", "root"]


class TestAmendTreePreservation:
    @async_test
    async def test_tree_unchanged_on_text_only_edit(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("original", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            orig_tree = await env.get_tree_hash()

            env.git_ctx.editor = make_editor_script(env.tmp_dir, "reworded")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=True)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_tree_hash() == orig_tree

    @async_test
    async def test_amend_changes_commit_hashes(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            orig_head = await env.get_commit_hash()

            await env.stage_file("a.txt", "modified")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            new_head = await env.get_commit_hash()
            assert new_head != orig_head


class TestAmendCommitterDate:
    @async_test
    async def test_amend_refreshes_committer_date_preserves_author(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            # Epoch 0 so any refresh is strictly greater
            await env.commit("first", {"a.txt": "a"}, committer_date="@0 +0000")
            orig_author = await env.get_author_date()

            await env.stage_file("a.txt", "modified")
            args = make_amend_args(edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_author_date() == orig_author
            assert int(await env.get_committer_date()) > 0

    @async_test
    async def test_amend_earlier_commit_refreshes_committer_date(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"}, committer_date="@0 +0000")
            await env.commit("second", {"b.txt": "b"})
            orig_author = await env.get_author_date("HEAD~1")
            top_committer = await env.get_committer_date("HEAD")

            await env.stage_file("a.txt", "modified")
            args = make_amend_args(ref_or_topic="HEAD~1", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_author_date("HEAD~1") == orig_author
            assert int(await env.get_committer_date("HEAD~1")) > 0
            # Recreated commit keeps its date
            assert await env.get_committer_date("HEAD") == top_committer

    @async_test
    async def test_last_touched_refreshes_committer_date(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            # Old but valid timestamp; the refresh must move committer date past it.
            await env.commit(
                "first\n\nTopic: alpha", {"a.txt": "v1"}, committer_date="@1000000000 +0000"
            )
            orig_author = await env.get_author_date("HEAD")

            await env.stage_file("a.txt", "v2")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            assert await env.get_author_date("HEAD") == orig_author
            assert int(await env.get_committer_date("HEAD")) > 1000000000


class TestAmendSingleCommit:
    @async_test
    async def test_amend_only_commit_above_root(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("only commit", {"a.txt": "a"})

            await env.stage_file("a.txt", "modified")
            args = make_amend_args(edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt") == "modified"
            assert await env.get_commit_count() == 2

    @async_test
    async def test_drop_only_commit_above_root(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("only", {"a.txt": "a"})

            args = make_amend_args(drop=True, edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_commit_count() == 1
            assert (await env.get_commit_message()).strip() == "root"


class TestAmendNonAncestor:
    @async_test
    async def test_non_ancestor_commit_raises(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})

            # Create a side branch with two commits (so side2~ = side1, not root)
            await env.git_ctx.git("checkout", "-b", "side")
            await env.commit("side1", {"s1.txt": "s1"})
            await env.commit("side2", {"s2.txt": "s2"})
            side_hash = await env.get_commit_hash()

            # Go back to main and add a commit that diverges
            await env.git_ctx.git("checkout", "main")
            await env.commit("main_commit", {"a.txt": "a"})
            await env.stage_file("a.txt", "mod")

            args = make_amend_args(ref_or_topic=side_hash, edit=False)
            with pytest.raises(RevupUsageException, match="not a first parent ancestor"):
                await amend.main(args, env.git_ctx)


class TestAmendLastTouched:
    @async_test
    async def test_amends_file_into_last_commit_that_touched_it(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "v1"})
            await env.commit("second\n\nTopic: beta", {"b.txt": "v1"})

            await env.stage_file("a.txt", "v2")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            # a.txt should be amended into "first" (HEAD~1), not HEAD
            content = await env.get_file_at_commit("a.txt", "HEAD~1")
            assert content == "v2"
            # b.txt should be unchanged
            content_b = await env.get_file_at_commit("b.txt", "HEAD")
            assert content_b == "v1"

    @async_test
    async def test_multiple_files_to_different_commits(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a1"})
            await env.commit("second\n\nTopic: beta", {"b.txt": "b1"})
            await env.commit("third\n\nTopic: gamma", {"c.txt": "c1"})

            await env.stage_file("a.txt", "a2")
            await env.stage_file("b.txt", "b2")
            await env.stage_file("c.txt", "c2")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            assert await env.get_file_at_commit("a.txt", "HEAD~2") == "a2"
            assert await env.get_file_at_commit("b.txt", "HEAD~1") == "b2"
            assert await env.get_file_at_commit("c.txt", "HEAD") == "c2"

    @async_test
    async def test_amended_change_propagates_forward(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "v1"})
            await env.commit("second\n\nTopic: beta", {"b.txt": "b1"})

            # a.txt is amended into "first"; the new content must carry through
            # to the rebuilt "second" as well, leaving no leftover staged change.
            await env.stage_file("a.txt", "v2")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            assert await env.get_file_at_commit("a.txt", "HEAD~1") == "v2"
            assert await env.get_file_at_commit("a.txt", "HEAD") == "v2"
            assert not await env.has_staged_changes()

    @async_test
    async def test_file_not_in_stack_remains_staged(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a1"})

            # Stage a file that no commit in the stack touched
            await env.stage_file("new.txt", "new content")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            # new.txt should still be staged
            assert await env.has_staged_changes()
            staged = await env.get_staged_files()
            assert "new.txt" in staged

    @async_test
    async def test_uses_most_recent_commit_for_file(self):
        """If multiple commits touch the same file, amend into the most recent one."""
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "v1"})
            await env.commit("second\n\nTopic: beta", {"a.txt": "v2"})

            await env.stage_file("a.txt", "v3")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            # Should go into "second" (HEAD), the most recent to touch a.txt
            assert await env.get_file_at_commit("a.txt", "HEAD") == "v3"
            # "first" keeps its original content
            assert await env.get_file_at_commit("a.txt", "HEAD~1") == "v1"

    @async_test
    async def test_preserves_commit_messages(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first msg\n\nTopic: alpha", {"a.txt": "a1"})
            await env.commit("second msg\n\nTopic: beta", {"b.txt": "b1"})

            await env.stage_file("a.txt", "a2")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            msg1 = await env.get_commit_message("HEAD~1")
            msg2 = await env.get_commit_message("HEAD")
            assert "first msg" in msg1
            assert "second msg" in msg2

    @async_test
    async def test_noop_when_no_staged_files(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a1"})

            original_hash = await env.get_commit_hash()
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            assert await env.get_commit_hash() == original_hash

    @async_test
    async def test_mutually_exclusive_with_ref_or_topic(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a1"})

            await env.stage_file("a.txt", "a2")
            args = make_amend_args(last_touched=True, parse_topics=True, ref_or_topic="HEAD")
            with pytest.raises(RevupUsageException, match="mutually exclusive"):
                await amend.main(args, env.git_ctx)

    @async_test
    async def test_mutually_exclusive_with_drop(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a1"})

            await env.stage_file("a.txt", "a2")
            args = make_amend_args(last_touched=True, parse_topics=True, drop=True)
            with pytest.raises(RevupUsageException, match="mutually exclusive"):
                await amend.main(args, env.git_ctx)

    @async_test
    async def test_requires_parse_topics(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a1"})

            await env.stage_file("a.txt", "a2")
            args = make_amend_args(last_touched=True, parse_topics=False)
            with pytest.raises(RevupUsageException, match="requires --parse-topics"):
                await amend.main(args, env.git_ctx)

    @async_test
    async def test_works_with_subdirectories(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"src/lib/a.txt": "a1"})
            await env.commit("second\n\nTopic: beta", {"src/lib/b.txt": "b1"})

            await env.stage_file("src/lib/a.txt", "a2")
            args = make_amend_args(last_touched=True, parse_topics=True)
            await amend.main(args, env.git_ctx)

            assert await env.get_file_at_commit("src/lib/a.txt", "HEAD~1") == "a2"
            assert await env.get_file_at_commit("src/lib/b.txt", "HEAD") == "b1"

    @async_test
    async def test_with_all_flag_stages_unstaged_changes(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a1"})

            # Modify without staging
            await env.write_file("a.txt", "a2")
            args = make_amend_args(last_touched=True, parse_topics=True, **{"all": True})
            await amend.main(args, env.git_ctx)

            assert await env.get_file_at_commit("a.txt", "HEAD") == "a2"
