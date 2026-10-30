import argparse
import logging

from git_env import GitTestEnvironment, async_test

from revup import toolkit


async def setup_repo(env):
    await env.commit("root", {"a.txt": "a", "b.txt": "b", "c.txt": "c"})
    await env.git_ctx.git("branch", "origin/main", "HEAD")


def chain(*names):
    return [("\u3000" * depth) + "└" + name for depth, name in enumerate(names)]


async def run_optimize_stack(env, capsys, ignore_existing=False):
    args = argparse.Namespace(
        toolkit_cmd="optimize-stack", base_branch=None, ignore_existing=ignore_existing
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
