import argparse

import pytest

from revup import amend
from revup.types import RevupConflictException, RevupUsageException
from tests.git_env import (
    GitTestEnvironment,
    async_test,
    make_editor_script,
    make_empty_editor_script,
    make_passthrough_editor_script,
)


def make_amend_args(**kwargs):
    defaults = {
        "ref_or_topic": None,
        "edit": False,
        "insert": False,
        "drop": False,
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

    @async_test
    async def test_noop_when_no_edit_flag(self):
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

    @async_test
    async def test_amend_head_explicit_ref(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})

            await env.stage_file("b.txt", "b_new")
            args = make_amend_args(ref_or_topic="HEAD", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("b.txt") == "b_new"
            assert await env.get_commit_count() == 3


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
    async def test_head_tilde_notation(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})
            await env.commit("third", {"c.txt": "c"})

            await env.stage_file("a.txt", "modified")
            args = make_amend_args(ref_or_topic="HEAD~2", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt", "HEAD~2") == "modified"

    @async_test
    async def test_head_caret_notation(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.commit("first", {"a.txt": "a"})
            await env.commit("second", {"b.txt": "b"})

            await env.stage_file("a.txt", "modified")
            args = make_amend_args(ref_or_topic="HEAD^", edit=False)
            ret = await amend.main(args, env.git_ctx)

            assert ret == 0
            assert await env.get_file_at_commit("a.txt", "HEAD^") == "modified"

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
