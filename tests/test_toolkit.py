import argparse
import logging

import pytest

from git_env import GitTestEnvironment, async_test

from revup import toolkit
from revup.core_types import RevupUsageException


async def setup_repo(env):
    await env.commit("root", {"a.txt": "a", "b.txt": "b", "c.txt": "c"})
    await env.git_ctx.git("branch", "origin/main", "HEAD")


def chain(*names):
    return [("\u3000" * depth) + "└" + name for depth, name in enumerate(names)]


async def make_remote_branch(env, name, files):
    """Create origin/<name> with one extra commit over the current HEAD, leaving HEAD alone."""
    head = await env.get_commit_hash()
    await env.commit(f"{name} work", files)
    await env.git_ctx.git("branch", f"origin/{name}", "HEAD")
    await env.git_ctx.git("reset", "--hard", head)


async def run_optimize_stack(
    env, capsys, ignore_existing=False, write=False, force_write=False
):
    args = argparse.Namespace(
        toolkit_cmd="optimize-stack",
        base_branch="main",
        ignore_existing=ignore_existing,
        write=write,
        force_write=force_write,
    )
    ret = await toolkit.main(args, env.git_ctx)
    return ret, capsys.readouterr().out.splitlines()


class TestOptimizeStack:
    @async_test
    async def test_independent_topics_become_separate_trees(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("one") + chain("two")

    @async_test
    async def test_dependent_topic_chains_onto_earlier_tree(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("one", "two")

    @async_test
    async def test_ignore_existing_drops_relative_tags(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})

            ret, out = await run_optimize_stack(env, capsys, ignore_existing=True)
            assert ret == 0
            assert out == chain("one") + chain("two")

    @async_test
    async def test_topic_needing_two_trees_merges_them_in_stack_order(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})
            await env.commit("three\n\nTopic: three", {"c.txt": "c1"})
            await env.commit("four\n\nTopic: four", {"a.txt": "a2", "c.txt": "c2"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("one", "three", "four") + chain("two")

    @async_test
    async def test_later_commit_of_placed_topic_pulls_in_other_tree(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})
            await env.commit("one again\n\nTopic: one", {"b.txt": "b2"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("one", "two")

    @async_test
    async def test_dependency_on_untopiced_commit_is_reported(self, capsys, caplog):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("no topic", {"a.txt": "a1"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a2"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})

            with caplog.at_level(logging.ERROR):
                ret, out = await run_optimize_stack(env, capsys)
            assert ret == 1
            assert out == chain("two")
            assert "Topic one can't be placed" in caplog.text
            assert "a.txt" in caplog.text
            assert "1 commit(s) without a topic" in caplog.text


class TestOptimizeStackRespectExisting:
    @async_test
    async def test_relative_tag_keeps_topic_in_relatives_tree(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("one", "two")

    @async_test
    async def test_other_topic_may_sit_between_relative_pair(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("three\n\nTopic: three\nRelative: one", {"b.txt": "b1"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("one", "two", "three")

    @async_test
    async def test_relative_to_topic_outside_stack_is_ignored(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: merged", {"b.txt": "b1"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("one") + chain("two")

    @async_test
    async def test_relative_appearing_later_in_stack_is_reported(self, capsys, caplog):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})

            with caplog.at_level(logging.ERROR):
                ret, out = await run_optimize_stack(env, capsys)
            assert ret == 1
            assert out == chain("one")
            assert "relative topic one appears later in the stack" in caplog.text


class TestOptimizeStackWrite:
    @async_test
    async def test_write_tags_first_commit_of_each_dependent_topic(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("two again\n\nTopic: two", {"a.txt": "a3"})
            await env.commit("three\n\nTopic: three", {"b.txt": "b1"})
            tree_before = await env.get_tree_hash()

            ret, out = await run_optimize_stack(env, capsys, write=True)
            assert ret == 0
            assert out == chain("one", "two") + chain("three")
            assert await env.get_tree_hash() == tree_before
            assert await env.get_commit_message("HEAD~3") == "one\n\nTopic: one"
            assert (
                await env.get_commit_message("HEAD~2")
                == "two\n\nTopic: two\nOptimized-Relative: one"
            )
            assert await env.get_commit_message("HEAD~1") == "two again\n\nTopic: two"
            assert await env.get_commit_message("HEAD") == "three\n\nTopic: three"
            assert not await env.has_staged_changes()

            head = await env.get_commit_hash()
            ret, out = await run_optimize_stack(env, capsys, write=True)
            assert ret == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_write_inserts_into_chain_and_removes_stale_tags(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("three\n\nTopic: three\nRelative: one", {"b.txt": "b1"})
            await env.commit("four\n\nTopic: four\nOptimized-Relative: one", {"c.txt": "c1"})

            ret, out = await run_optimize_stack(env, capsys, write=True)
            assert ret == 0
            assert out == chain("one", "two", "three") + chain("four")
            # two was inserted between one and three, so three now points at two
            assert (
                await env.get_commit_message("HEAD~1")
                == "three\n\nTopic: three\nRelative: one\nOptimized-Relative: two"
            )
            assert await env.get_commit_message("HEAD") == "four\n\nTopic: four"

    @async_test
    async def test_write_skips_tag_matching_existing_relative(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"a.txt": "a2"})
            head = await env.get_commit_hash()

            ret, out = await run_optimize_stack(env, capsys, write=True)
            assert ret == 0
            assert out == chain("one", "two")
            assert await env.get_commit_hash() == head

    @async_test
    async def test_write_leaves_failed_topic_alone(self, capsys, caplog):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("no topic", {"a.txt": "a1"})
            await env.commit("one\n\nTopic: one\nOptimized-Relative: stale", {"a.txt": "a2"})
            head = await env.get_commit_hash()

            with caplog.at_level(logging.ERROR):
                ret, out = await run_optimize_stack(env, capsys, write=True)
            assert ret == 1
            assert await env.get_commit_hash() == head


class TestOptimizeStackBranches:
    @async_test
    async def test_topic_targeting_other_branch_is_skipped(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await make_remote_branch(env, "release", {"d.txt": "d"})
            await env.commit("one\n\nTopic: one\nBranch: release", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nBranch: main", {"b.txt": "b1"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == chain("two")

    @async_test
    async def test_relative_branch_topic_starts_tree_on_that_branch(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await make_remote_branch(env, "feature", {"a.txt": "f"})
            await env.commit("one\n\nTopic: one\nRelative-Branch: feature", {"b.txt": "b1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b2"})
            await env.commit("three\n\nTopic: three", {"c.txt": "c1"})

            ret, out = await run_optimize_stack(env, capsys)
            assert ret == 0
            assert out == ["feature:", *chain("one", "two")] + chain("three")

    @async_test
    async def test_relative_branch_topic_cannot_use_base_branch_trees(self, capsys, caplog):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await make_remote_branch(env, "feature", {"d.txt": "d"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative-Branch: feature", {"a.txt": "a2"})

            with caplog.at_level(logging.ERROR):
                ret, out = await run_optimize_stack(env, capsys)
            assert ret == 1
            assert out == chain("one")
            assert "Topic two can't be placed" in caplog.text


class TestOptimizeStackForceWrite:
    @async_test
    async def test_force_write_replaces_all_relative_tags(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("three\n\nTopic: three\nRelative: one", {"b.txt": "b1"})
            await env.commit("four\n\nTopic: four\nOptimized-Relative: one", {"c.txt": "c1"})

            ret, out = await run_optimize_stack(env, capsys, force_write=True)
            assert ret == 0
            assert out == chain("one", "two", "three") + chain("four")
            assert await env.get_commit_message("HEAD~3") == "one\n\nTopic: one"
            assert await env.get_commit_message("HEAD~2") == "two\n\nTopic: two\nRelative: one"
            assert await env.get_commit_message("HEAD~1") == "three\n\nTopic: three\nRelative: two"
            assert await env.get_commit_message("HEAD") == "four\n\nTopic: four"

    @async_test
    async def test_write_and_force_write_are_exclusive(self, capsys):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})

            with pytest.raises(RevupUsageException, match="mutually exclusive"):
                await run_optimize_stack(env, capsys, write=True, force_write=True)
