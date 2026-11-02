import argparse

import pytest
from git_env import GitTestEnvironment, async_test

from revup import optimize_stack
from revup.core_types import RevupUsageException


async def setup_repo(env):
    await env.commit("root", {"a.txt": "a", "b.txt": "b", "c.txt": "c"})
    await env.git_ctx.git("branch", "origin/main", "HEAD")


async def make_remote_branch(env, name, files):
    """Create origin/<name> with one extra commit over the current HEAD, leaving HEAD alone."""
    head = await env.get_commit_hash()
    await env.commit(f"{name} work", files)
    await env.git_ctx.git("branch", f"origin/{name}", "HEAD")
    await env.git_ctx.git("reset", "--hard", head)


async def run_optimize_stack(
    env, ignore_existing=False, write=False, force_write=False, auto_topic=False
):
    args = argparse.Namespace(
        base_branch="main",
        relative_branch=None,
        auto_topic=auto_topic,
        ignore_existing=ignore_existing,
        write=write,
        force_write=force_write,
    )
    return await optimize_stack.main(args, env.git_ctx)


async def stack_messages(env, count):
    """Messages of the last count commits, oldest first."""
    return [await env.get_commit_message(f"HEAD~{i}") for i in reversed(range(count))]


class TestOptimizeStack:
    @async_test
    async def test_independent_topics_become_separate_trees(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_dependent_topic_chains_onto_earlier_tree(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})

            assert await run_optimize_stack(env, write=True) == 0
            assert await stack_messages(env, 2) == [
                "one\n\nTopic: one",
                "two\n\nTopic: two\nOptimized-Relative: one",
            ]

    @async_test
    async def test_ignore_existing_drops_relative_tags(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})

            assert await run_optimize_stack(env, ignore_existing=True, write=True) == 0
            assert await env.get_commit_message() == "two\n\nTopic: two"

    @async_test
    async def test_ignore_existing_write_replaces_relative_with_optimized(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})
            await env.commit("three\n\nTopic: three", {"a.txt": "a2"})

            assert await run_optimize_stack(env, ignore_existing=True, write=True) == 0
            assert await stack_messages(env, 2) == [
                "two\n\nTopic: two",
                "three\n\nTopic: three\nOptimized-Relative: one",
            ]

    @async_test
    async def test_topic_needing_two_trees_merges_them_in_stack_order(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})
            await env.commit("three\n\nTopic: three", {"c.txt": "c1"})
            await env.commit("four\n\nTopic: four", {"a.txt": "a2", "c.txt": "c2"})

            assert await run_optimize_stack(env, write=True) == 0
            assert await stack_messages(env, 4) == [
                "one\n\nTopic: one",
                "two\n\nTopic: two",
                "three\n\nTopic: three\nOptimized-Relative: one",
                "four\n\nTopic: four\nOptimized-Relative: three",
            ]

    @async_test
    async def test_topic_depending_on_later_interleaved_topic_fails(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})
            await env.commit("one again\n\nTopic: one", {"b.txt": "b2"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 1
            assert await env.get_commit_hash() == head

    @async_test
    async def test_failed_topic_leaves_no_trace_in_the_forest(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})
            await env.commit("one again\n\nTopic: one", {"b.txt": "b2"})
            await env.commit("three\n\nTopic: three", {"a.txt": "a2"})

            assert await run_optimize_stack(env, write=True) == 1
            assert await stack_messages(env, 4) == [
                "one\n\nTopic: one",
                "two\n\nTopic: two",
                "one again\n\nTopic: one",
                "three\n\nTopic: three",
            ]

    @async_test
    async def test_failure_prevents_writing_earlier_topics(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("no topic", {"c.txt": "c1"})
            await env.commit("three\n\nTopic: three", {"c.txt": "c2"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 1
            assert await env.get_commit_hash() == head

    @async_test
    async def test_mutually_interleaved_topics_fail_with_ignore_existing(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"a.txt": "a2"})
            await env.commit("one again\n\nTopic: one", {"a.txt": "a3"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, ignore_existing=True, write=True) == 1
            assert await env.get_commit_hash() == head

    @async_test
    async def test_dependency_on_untopiced_commit_fails(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("no topic", {"a.txt": "a1"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a2"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b1"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 1
            assert await env.get_commit_hash() == head

    @async_test
    async def test_commit_with_two_topics_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one, two", {"a.txt": "a1"})

            with pytest.raises(RevupUsageException, match="more than one topic"):
                await run_optimize_stack(env)


class TestOptimizeStackRespectExisting:
    @async_test
    async def test_relative_tag_keeps_topic_in_relatives_tree(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_other_topic_may_sit_between_relative_pair(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("three\n\nTopic: three\nRelative: one", {"b.txt": "b1"})

            assert await run_optimize_stack(env, write=True) == 0
            assert await stack_messages(env, 2) == [
                "two\n\nTopic: two\nOptimized-Relative: one",
                "three\n\nTopic: three\nRelative: one\nOptimized-Relative: two",
            ]

    @async_test
    async def test_relative_to_topic_outside_stack_is_ignored(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: merged", {"b.txt": "b1"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_relative_appearing_later_in_stack_is_placed_first(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_relative_cycle_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one\nRelative: two", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})

            with pytest.raises(RevupUsageException, match="cycle"):
                await run_optimize_stack(env)

    @async_test
    async def test_relative_to_skipped_topic_fails(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await make_remote_branch(env, "release", {"d.txt": "d"})
            await env.commit("one\n\nTopic: one\nBranch: release", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"b.txt": "b1"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 1
            assert await env.get_commit_hash() == head


class TestOptimizeStackWrite:
    @async_test
    async def test_write_tags_first_commit_of_each_dependent_topic(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("two again\n\nTopic: two", {"a.txt": "a3"})
            await env.commit("three\n\nTopic: three", {"b.txt": "b1"})
            tree_before = await env.get_tree_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_tree_hash() == tree_before
            assert await stack_messages(env, 4) == [
                "one\n\nTopic: one",
                "two\n\nTopic: two\nOptimized-Relative: one",
                "two again\n\nTopic: two",
                "three\n\nTopic: three",
            ]
            assert not await env.has_staged_changes()

            head = await env.get_commit_hash()
            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_write_inserts_into_chain_and_removes_stale_tags(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("three\n\nTopic: three\nRelative: one", {"b.txt": "b1"})
            await env.commit("four\n\nTopic: four\nOptimized-Relative: one", {"c.txt": "c1"})

            assert await run_optimize_stack(env, write=True) == 0
            assert await stack_messages(env, 2) == [
                "three\n\nTopic: three\nRelative: one\nOptimized-Relative: two",
                "four\n\nTopic: four",
            ]

    @async_test
    async def test_write_skips_tag_matching_existing_relative(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative: one", {"a.txt": "a2"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_write_skips_relative_tag_on_later_commit_of_topic(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("two again\n\nTopic: two\nRelative: one", {"a.txt": "a3"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_write_strips_stale_tags_from_every_commit_but_tags_only_first(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nOptimized-Relative: stale", {"a.txt": "a2"})
            await env.commit("two again\n\nTopic: two\nOptimized-Relative: stale", {"a.txt": "a3"})

            assert await run_optimize_stack(env, write=True) == 0
            assert await stack_messages(env, 2) == [
                "two\n\nTopic: two\nOptimized-Relative: one",
                "two again\n\nTopic: two",
            ]

    @async_test
    async def test_write_leaves_failed_topic_alone(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("no topic", {"a.txt": "a1"})
            await env.commit("one\n\nTopic: one\nOptimized-Relative: stale", {"a.txt": "a2"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 1
            assert await env.get_commit_hash() == head

    @async_test
    async def test_dry_run_leaves_stack_alone(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env) == 0
            assert await env.get_commit_hash() == head


class TestOptimizeStackBranches:
    @async_test
    async def test_topic_targeting_other_branch_is_skipped(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await make_remote_branch(env, "release", {"d.txt": "d"})
            await env.commit("one\n\nTopic: one\nBranch: release", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nBranch: main", {"b.txt": "b1"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_relative_branch_topic_starts_tree_on_that_branch(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await make_remote_branch(env, "feature", {"a.txt": "f"})
            await env.commit("one\n\nTopic: one\nRelative-Branch: feature", {"b.txt": "b1"})
            await env.commit("two\n\nTopic: two", {"b.txt": "b2"})
            await env.commit("three\n\nTopic: three", {"c.txt": "c1"})

            assert await run_optimize_stack(env, write=True) == 0
            assert await stack_messages(env, 3) == [
                "one\n\nTopic: one\nRelative-Branch: feature",
                "two\n\nTopic: two\nOptimized-Relative: one",
                "three\n\nTopic: three",
            ]

    @async_test
    async def test_relative_branch_topic_cannot_use_base_branch_trees(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await make_remote_branch(env, "feature", {"d.txt": "d"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two\nRelative-Branch: feature", {"a.txt": "a2"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, write=True) == 1
            assert await env.get_commit_hash() == head


class TestOptimizeStackForceWrite:
    @async_test
    async def test_force_write_replaces_all_relative_tags(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit("two\n\nTopic: two", {"a.txt": "a2"})
            await env.commit("three\n\nTopic: three\nRelative: one", {"b.txt": "b1"})
            await env.commit("four\n\nTopic: four\nOptimized-Relative: one", {"c.txt": "c1"})

            assert await run_optimize_stack(env, force_write=True) == 0
            assert await stack_messages(env, 4) == [
                "one\n\nTopic: one",
                "two\n\nTopic: two\nRelative: one",
                "three\n\nTopic: three\nRelative: two",
                "four\n\nTopic: four",
            ]

    @async_test
    async def test_force_write_drops_stale_optimized_tag_when_relative_unchanged(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})
            await env.commit(
                "two\n\nTopic: two\nRelative: one\nOptimized-Relative: one", {"a.txt": "a2"}
            )

            assert await run_optimize_stack(env, force_write=True) == 0
            assert await env.get_commit_message() == "two\n\nTopic: two\nRelative: one"

    @async_test
    async def test_write_and_force_write_are_exclusive(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one\n\nTopic: one", {"a.txt": "a1"})

            with pytest.raises(RevupUsageException, match="mutually exclusive"):
                await run_optimize_stack(env, write=True, force_write=True)


class TestOptimizeStackAutoTopic:
    @async_test
    async def test_auto_topic_dry_run_leaves_topicless_commits_alone(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("Fix the widget", {"a.txt": "a1"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a2"})
            head = await env.get_commit_hash()

            assert await run_optimize_stack(env, auto_topic=True) == 0
            assert await env.get_commit_hash() == head

    @async_test
    async def test_auto_topic_rejects_invalid_generated_name(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("Fix (the) widget", {"a.txt": "a1"})

            with pytest.raises(RevupUsageException, match="Invalid characters"):
                await run_optimize_stack(env, auto_topic=True)

    @async_test
    async def test_auto_topic_write_adds_topic_tags(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("Fix the widget", {"a.txt": "a1"})
            await env.commit("one\n\nTopic: one", {"a.txt": "a2"})
            await env.commit("Tweak docs", {"b.txt": "b1"})

            assert await run_optimize_stack(env, auto_topic=True, write=True) == 0
            assert await stack_messages(env, 3) == [
                "Fix the widget\n\nTopic: fix_the_widget",
                "one\n\nTopic: one\nOptimized-Relative: fix_the_widget",
                "Tweak docs\n\nTopic: tweak_docs",
            ]
