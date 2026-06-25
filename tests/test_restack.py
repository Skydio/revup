from git_env import GitTestEnvironment, async_test

from revup.restack import restack
from revup.topic_stack import TopicStack


async def setup_repo(env):
    await env.commit("root", {"root.txt": "r"})
    await env.git_ctx.git("branch", "origin/main", "HEAD")


async def run_restack(env, topicless_last=False, squash=False):
    topics = TopicStack(env.git_ctx, None, None)
    await topics.populate_topics()
    await topics.populate_reviews()
    await restack(topics, topicless_last, squash)
    return topics


class TestRestackBasic:
    @async_test
    async def test_groups_interleaved_topic_commits(self):
        """Commits from the same topic should be grouped together."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a1\n\nTopic: alpha", {"a.txt": "a1"})
            await env.commit("b1\n\nTopic: beta", {"b.txt": "b1"})
            await env.commit("a2\n\nTopic: alpha", {"a.txt": "a2"})

            await run_restack(env)
            subjects = await env.get_log_subjects()

            # Topics are grouped: alpha's commits together, beta's together.
            # Topological order puts alpha first (bottom), beta on top.
            assert subjects == ["b1", "a2", "a1", "root"]
            assert await env.get_file_at_commit("a.txt") == "a2"
            assert await env.get_file_at_commit("b.txt") == "b1"

    @async_test
    async def test_noop_when_already_grouped(self):
        """Restack should be idempotent when topics are already contiguous."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a1\n\nTopic: alpha", {"a.txt": "a1"})
            await env.commit("a2\n\nTopic: alpha", {"a.txt": "a2"})
            await env.commit("b1\n\nTopic: beta", {"b.txt": "b1"})
            tree_before = await env.get_tree_hash()

            await run_restack(env)
            tree_after = await env.get_tree_hash()

            assert tree_before == tree_after


class TestRestackTopiclessLast:
    @async_test
    async def test_topicless_commits_placed_last(self):
        """With --topicless-last, non-topic commits go to the top of the stack."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("no topic", {"x.txt": "x"})
            await env.commit("a1\n\nTopic: alpha", {"a.txt": "a"})

            await run_restack(env, topicless_last=True)
            subjects = await env.get_log_subjects()

            assert subjects == ["no topic", "a1", "root"]

    @async_test
    async def test_topicless_commits_placed_first_by_default(self):
        """Without the flag, topicless commits come before topic commits."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a1\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("no topic", {"x.txt": "x"})

            await run_restack(env, topicless_last=False)
            subjects = await env.get_log_subjects()

            assert subjects == ["a1", "no topic", "root"]


class TestRestackDropsEmpty:
    @async_test
    async def test_drops_empty_topic(self):
        """A topic consisting entirely of empty commits should be dropped."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("real\n\nTopic: alpha", {"a.txt": "a"})
            # Empty commit — same tree as parent
            await env.commit("empty\n\nTopic: empty_topic")

            await run_restack(env)
            subjects = await env.get_log_subjects()

            assert "empty" not in subjects
            assert "real" in subjects

    @async_test
    async def test_keeps_empty_commit_in_nonempty_topic(self):
        """An empty commit inside a topic with real changes should be kept."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("real change\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("empty followup\n\nTopic: alpha")

            await run_restack(env)
            subjects = await env.get_log_subjects()

            assert subjects == ["empty followup", "real change", "root"]


class TestRestackRelativeTopics:
    @async_test
    async def test_relative_topic_ordered_after_parent(self):
        """A topic relative to another must appear after its parent."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("child\n\nTopic: child\nRelative: parent", {"c.txt": "c"})
            await env.commit("parent\n\nTopic: parent", {"p.txt": "p"})

            await run_restack(env)
            subjects = await env.get_log_subjects()

            parent_idx = subjects.index("parent")
            child_idx = subjects.index("child")
            assert child_idx < parent_idx  # child is higher (more recent) in the stack


class TestRestackSquash:
    @async_test
    async def test_squash_multi_commit_topic(self):
        """--squash should collapse multi-commit topics into one commit."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a1\n\nTopic: alpha", {"a.txt": "a1"})
            await env.commit("a2\n\nTopic: alpha", {"a.txt": "a2"})
            await env.commit("a3\n\nTopic: alpha", {"b.txt": "b"})

            await run_restack(env, squash=True)

            assert await env.get_commit_count() == 2
            assert await env.get_file_at_commit("a.txt") == "a2"
            assert await env.get_file_at_commit("b.txt") == "b"

    @async_test
    async def test_squash_merges_messages(self):
        """Squashed commit should contain body text from all original commits."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("first title\n\nbody one\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("second title\n\nbody two\n\nTopic: alpha", {"b.txt": "b"})

            await run_restack(env, squash=True)
            msg = await env.get_commit_message()

            assert "first title" in msg
            assert "body one" in msg
            assert "second title" in msg
            assert "body two" in msg

    @async_test
    async def test_squash_deduplicates_tags(self):
        """Duplicate reviewers/labels across commits should appear once."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit(
                "c1\n\nTopic: alpha\nReviewer: alice, bob",
                {"a.txt": "a"},
            )
            await env.commit(
                "c2\n\nTopic: alpha\nReviewer: bob, carol",
                {"b.txt": "b"},
            )

            await run_restack(env, squash=True)
            msg = await env.get_commit_message()

            assert msg.count("alice") == 1
            assert msg.count("bob") == 1
            assert msg.count("carol") == 1

    @async_test
    async def test_squash_leaves_single_commit_topics_alone(self):
        """Topics with only one commit shouldn't be affected by squash."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("b\n\nTopic: beta", {"b.txt": "b"})
            orig_count = await env.get_commit_count()

            await run_restack(env, squash=True)

            assert await env.get_commit_count() == orig_count

    @async_test
    async def test_squash_preserves_topicless_commits(self):
        """Topicless commits should not be squashed together."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("no topic 1", {"x.txt": "x"})
            await env.commit("no topic 2", {"y.txt": "y"})
            await env.commit("a1\n\nTopic: alpha", {"a.txt": "a1"})
            await env.commit("a2\n\nTopic: alpha", {"a.txt": "a2"})

            await run_restack(env, squash=True)

            # 1 root + 2 topicless + 1 squashed alpha = 4
            assert await env.get_commit_count() == 4

    @async_test
    async def test_squash_with_multiple_topics(self):
        """Each topic should be independently squashed."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a1\n\nTopic: alpha", {"a.txt": "a1"})
            await env.commit("a2\n\nTopic: alpha", {"a.txt": "a2"})
            await env.commit("b1\n\nTopic: beta", {"b.txt": "b1"})
            await env.commit("b2\n\nTopic: beta", {"b.txt": "b2"})

            await run_restack(env, squash=True)

            # 1 root + 1 squashed alpha + 1 squashed beta = 3
            assert await env.get_commit_count() == 3
            assert await env.get_file_at_commit("a.txt") == "a2"
            assert await env.get_file_at_commit("b.txt") == "b2"
