import argparse

import pytest
from git_env import GitTestEnvironment, async_test

from revup.topic_stack import TopicStack, format_remote_branch
from revup.types import RevupConflictException, RevupUsageException


def make_upload_args(**kwargs):
    defaults = {
        "topics": [],
        "base_branch": None,
        "relative_branch": None,
        "rebase": False,
        "skip_confirm": True,
        "dry_run": True,
        "push_only": False,
        "status": False,
        "update_pr_body": True,
        "create_local_branches": False,
        "review_graph": True,
        "trim_tags": False,
        "patchsets": True,
        "self_authored_only": False,
        "labels": None,
        "auto_add_users": "no",
        "user_aliases": "",
        "uploader": "",
        "branch_format": "user+branch",
        "pre_upload": None,
        "relative_chain": False,
        "auto_topic": False,
        "head": "HEAD",
        "verbose": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


async def setup_repo(env):
    """Create root commit and origin/main branch."""
    await env.commit("root", {"root.txt": "r"})
    await env.git_ctx.git("branch", "origin/main", "HEAD")


async def run_upload_pipeline(env, **kwargs):
    """Run the full upload dry-run pipeline and return the TopicStack for inspection."""
    args = make_upload_args(**kwargs)
    topics = TopicStack(
        env.git_ctx,
        args.base_branch,
        args.relative_branch,
        head=args.head,
    )
    await topics.populate_topics(
        auto_topic=args.auto_topic,
        trim_tags=args.trim_tags,
        raise_on_invalid=True,
    )
    await topics.populate_reviews(
        force_relative_chain=args.relative_chain,
        labels=args.labels,
        user_aliases=args.user_aliases,
        auto_add_users=args.auto_add_users,
        self_authored_only=args.self_authored_only,
        limit_topics=args.topics if args.topics else None,
    )
    await topics.populate_relative_reviews(
        args.uploader if args.uploader else env.git_ctx.author,
        branch_format=args.branch_format,
    )
    await topics.create_commits(args.trim_tags)
    return topics


async def get_file_at_ref(env, ref, path):
    return await env.git_ctx.git_stdout("show", f"{ref}:{path}")


async def get_commit_msg_at_ref(env, ref):
    return await env.git_ctx.git_stdout("log", "-1", "--format=%B", ref)


async def get_parent(env, ref):
    return await env.git_ctx.git_stdout("rev-parse", f"{ref}^")


class TestUploadSingleTopic:
    @async_test
    async def test_single_topic_creates_commit_on_base(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root_hash = await env.get_commit_hash()
            await env.commit("feature\n\nTopic: feat", {"a.txt": "hello"})

            topics = await run_upload_pipeline(env)

            review = topics.topics["feat"].reviews["origin/main"]
            assert len(review.new_commits) == 1
            assert review.base_ref == root_hash
            content = await get_file_at_ref(env, review.new_commits[-1], "a.txt")
            assert content == "hello"

    @async_test
    async def test_single_topic_multiple_commits_preserves_order(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root_hash = await env.get_commit_hash()
            await env.commit("first\n\nTopic: feat", {"a.txt": "v1"})
            await env.commit("second\n\nTopic: feat", {"a.txt": "v2"})

            topics = await run_upload_pipeline(env)

            review = topics.topics["feat"].reviews["origin/main"]
            assert len(review.new_commits) == 2
            assert review.base_ref == root_hash
            content = await get_file_at_ref(env, review.new_commits[-1], "a.txt")
            assert content == "v2"
            content_first = await get_file_at_ref(env, review.new_commits[0], "a.txt")
            assert content_first == "v1"

    @async_test
    async def test_remote_head_uses_branch_format(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feature\n\nTopic: feat", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            review = topics.topics["feat"].reviews["origin/main"]
            assert review.remote_head == "test/revup/main/feat"


class TestUploadMultipleTopics:
    @async_test
    async def test_two_topics_cherry_picked_independently_to_base(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root_hash = await env.get_commit_hash()
            await env.commit("alpha\n\nTopic: alpha", {"a.txt": "alpha-content"})
            await env.commit("beta\n\nTopic: beta", {"b.txt": "beta-content"})

            topics = await run_upload_pipeline(env)

            alpha_review = topics.topics["alpha"].reviews["origin/main"]
            beta_review = topics.topics["beta"].reviews["origin/main"]

            assert alpha_review.base_ref == root_hash
            assert beta_review.base_ref == root_hash

            alpha_content = await get_file_at_ref(env, alpha_review.new_commits[-1], "a.txt")
            assert alpha_content == "alpha-content"

            beta_content = await get_file_at_ref(env, beta_review.new_commits[-1], "b.txt")
            assert beta_content == "beta-content"

            # beta's cherry-pick should NOT contain alpha's file (since they're independent)
            with pytest.raises(Exception):
                await get_file_at_ref(env, beta_review.new_commits[-1], "a.txt")


class TestUploadLimitTopics:
    @async_test
    async def test_limit_filters_to_named_topic(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("b\n\nTopic: beta", {"b.txt": "b"})

            topics = await run_upload_pipeline(env, topics=["alpha"])

            assert "alpha" in topics.topics
            assert "beta" not in topics.topics
            review = topics.topics["alpha"].reviews["origin/main"]
            assert len(review.new_commits) == 1


class TestUploadRelativeTopics:
    @async_test
    async def test_child_topic_based_on_parent_commit(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("base\n\nTopic: parent-feat", {"a.txt": "a"})
            await env.commit("child\n\nTopic: child-feat\nRelative: parent-feat", {"b.txt": "b"})

            topics = await run_upload_pipeline(env)

            parent_review = topics.topics["parent-feat"].reviews["origin/main"]
            child_review = topics.topics["child-feat"].reviews["origin/main"]

            # Child's base_ref is the parent topic's head commit
            assert child_review.base_ref == parent_review.new_commits[-1]

            # Child branch contains both files
            a_content = await get_file_at_ref(env, child_review.new_commits[-1], "a.txt")
            assert a_content == "a"
            b_content = await get_file_at_ref(env, child_review.new_commits[-1], "b.txt")
            assert b_content == "b"

    @async_test
    async def test_three_level_chain_base_refs(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: first", {"a.txt": "a"})
            await env.commit("b\n\nTopic: second\nRelative: first", {"b.txt": "b"})
            await env.commit("c\n\nTopic: third\nRelative: second", {"c.txt": "c"})

            topics = await run_upload_pipeline(env)

            first = topics.topics["first"].reviews["origin/main"]
            second = topics.topics["second"].reviews["origin/main"]
            third = topics.topics["third"].reviews["origin/main"]

            assert second.base_ref == first.new_commits[-1]
            assert third.base_ref == second.new_commits[-1]

            # Third's head should have all three files
            for fname, expected in [("a.txt", "a"), ("b.txt", "b"), ("c.txt", "c")]:
                content = await get_file_at_ref(env, third.new_commits[-1], fname)
                assert content == expected

    @async_test
    async def test_nonexistent_relative_treated_as_merged(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root_hash = await env.get_commit_hash()
            await env.commit("a\n\nTopic: child\nRelative: gone", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)

            # "gone" not found, so child falls back to base branch
            review = topics.topics["child"].reviews["origin/main"]
            assert topics.topics["child"].relative_topic is None
            assert review.base_ref == root_hash

    @async_test
    async def test_multiple_relative_tags_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: base1", {"a.txt": "a"})
            await env.commit("b\n\nTopic: base2", {"b.txt": "b"})
            await env.commit("c\n\nTopic: child\nRelative: base1\nRelative: base2", {"c.txt": "c"})

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)

    @async_test
    async def test_relative_cycle_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha\nRelative: beta", {"a.txt": "a"})
            await env.commit("b\n\nTopic: beta\nRelative: alpha", {"b.txt": "b"})

            with pytest.raises(RevupUsageException, match="cycle"):
                await run_upload_pipeline(env)


class TestUploadRelativeChainFlag:
    @async_test
    async def test_relative_chain_links_topics_in_order(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("b\n\nTopic: beta", {"b.txt": "b"})

            topics = await run_upload_pipeline(env, relative_chain=True)

            assert topics.topics["beta"].relative_topic is not None
            assert topics.topics["beta"].relative_topic.name == "alpha"

            alpha_review = topics.topics["alpha"].reviews["origin/main"]
            beta_review = topics.topics["beta"].reviews["origin/main"]
            assert beta_review.base_ref == alpha_review.new_commits[-1]

            # beta branch includes both files
            a_content = await get_file_at_ref(env, beta_review.new_commits[-1], "a.txt")
            assert a_content == "a"
            b_content = await get_file_at_ref(env, beta_review.new_commits[-1], "b.txt")
            assert b_content == "b"


class TestUploadAutoTopic:
    @async_test
    async def test_auto_topic_name_from_title(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("fix login bug", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, auto_topic=True)
            assert "fix_login_bug" in topics.topics
            review = topics.topics["fix_login_bug"].reviews["origin/main"]
            assert len(review.new_commits) == 1

    @async_test
    async def test_auto_topic_truncates_to_five_words(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("one two three four five six seven", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, auto_topic=True)
            name = list(topics.topics.keys())[0]
            assert name == "one_two_three_four_five"

    @async_test
    async def test_auto_topic_strips_brackets_and_colons(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("[feat]: add thing", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, auto_topic=True)
            name = list(topics.topics.keys())[0]
            assert "[" not in name
            assert "]" not in name
            assert ":" not in name


class TestUploadTrimTags:
    @async_test
    async def test_trim_tags_strips_tags_from_created_commit(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit(
                "feat title\n\nBody text\n\nTopic: alpha\nReviewer: user1",
                {"a.txt": "a"},
            )

            topics = await run_upload_pipeline(env, trim_tags=True)

            review = topics.topics["alpha"].reviews["origin/main"]
            msg = await get_commit_msg_at_ref(env, review.new_commits[-1])
            assert "Topic:" not in msg
            assert "Reviewer:" not in msg
            assert "feat title" in msg
            assert "Body text" in msg

    @async_test
    async def test_trim_tags_creates_new_commit_id(self):
        """Trimming tags changes the commit message, so the commit hash must differ."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nReviewer: user1", {"a.txt": "a"})
            original_hash = await env.get_commit_hash()

            topics = await run_upload_pipeline(env, trim_tags=True)
            review = topics.topics["alpha"].reviews["origin/main"]
            assert review.new_commits[-1] != original_hash


class TestUploadTagParsing:
    @async_test
    async def test_branch_tag_creates_review_for_that_branch(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("branch", "origin/release-1.0", "HEAD")
            await env.commit("feat\n\nTopic: alpha\nBranch: release-1.0", {"a.txt": "hello"})

            topics = await run_upload_pipeline(env)
            assert "origin/release-1.0" in topics.topics["alpha"].reviews
            review = topics.topics["alpha"].reviews["origin/release-1.0"]
            content = await get_file_at_ref(env, review.new_commits[-1], "a.txt")
            assert content == "hello"

    @async_test
    async def test_uploader_tag_affects_remote_head(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nUploader: custom-user", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            assert review.remote_head == "custom-user/revup/main/alpha"

    @async_test
    async def test_multiple_uploaders_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit(
                "feat\n\nTopic: alpha\nUploader: user1\nUploader: user2", {"a.txt": "a"}
            )

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)

    @async_test
    async def test_plural_tag_forms(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit(
                "feat\n\nTopic: alpha\nReviewers: user1, user2\nLabels: bug",
                {"a.txt": "a"},
            )

            topics = await run_upload_pipeline(env)
            assert topics.topics["alpha"].tags["reviewer"] == {"user1", "user2"}
            assert topics.topics["alpha"].tags["label"] == {"bug"}

    @async_test
    async def test_case_insensitive_tag_names(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nREVIEWER: user1", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            assert topics.topics["alpha"].tags["reviewer"] == {"user1"}

    @async_test
    async def test_tags_merged_across_commits_in_same_topic(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha\nReviewer: user1", {"a.txt": "a"})
            await env.commit("b\n\nTopic: alpha\nReviewer: user2", {"b.txt": "b"})

            topics = await run_upload_pipeline(env)
            assert topics.topics["alpha"].tags["reviewer"] == {"user1", "user2"}


class TestUploadBranchFormat:
    @async_test
    async def test_all_format_strategies(self):
        assert format_remote_branch("u", "main", "t", "user+branch") == "u/revup/main/t"
        assert format_remote_branch("u", "main", "t", "user") == "u/revup/t"
        assert format_remote_branch("u", "main", "t", "branch") == "revup/main/t"
        assert format_remote_branch("u", "main", "t", "none") == "revup/t"

    @async_test
    async def test_branch_format_tag_overrides_default(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nBranch-Format: none", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, branch_format="user+branch")
            review = topics.topics["alpha"].reviews["origin/main"]
            assert review.remote_head == "revup/alpha"

    @async_test
    async def test_invalid_branch_format_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nBranch-Format: invalid", {"a.txt": "a"})

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)

    @async_test
    async def test_multiple_branches_need_branch_in_format(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("branch", "origin/develop", "HEAD")
            await env.commit(
                "feat\n\nTopic: alpha\nBranch: main, develop\nBranch-Format: user",
                {"a.txt": "a"},
            )

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)


class TestUploadDraftLabel:
    @async_test
    async def test_draft_label_marks_review_as_draft(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nLabel: draft", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            assert review.is_draft is True
            # "draft" is removed from the label set
            assert "draft" not in topics.topics["alpha"].tags["label"]


class TestUploadAutoAddUsers:
    @async_test
    async def test_r2a_copies_reviewers_to_assignees(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nReviewer: user1", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, auto_add_users="r2a")
            assert "user1" in topics.topics["alpha"].tags["assignee"]
            assert "user1" in topics.topics["alpha"].tags["reviewer"]

    @async_test
    async def test_both_copies_bidirectionally(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nReviewer: rev1\nAssignee: asn1", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, auto_add_users="both")
            assert "rev1" in topics.topics["alpha"].tags["assignee"]
            assert "asn1" in topics.topics["alpha"].tags["reviewer"]


class TestUploadUserAliases:
    @async_test
    async def test_alias_replaces_reviewer_name(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nReviewer: short", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, user_aliases="short:longusername")
            assert "longusername" in topics.topics["alpha"].tags["reviewer"]
            assert "short" not in topics.topics["alpha"].tags["reviewer"]


class TestUploadSelfAuthoredOnly:
    @async_test
    async def test_skips_other_authors_topic(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            ts = env._next_timestamp()
            await env.write_file("a.txt", "a")
            await env.git_ctx.git("add", "a.txt")
            await env.git_ctx.git(
                "commit",
                "-m",
                "feat\n\nTopic: foreign",
                "--allow-empty",
                env={
                    "GIT_AUTHOR_NAME": "Other",
                    "GIT_AUTHOR_EMAIL": "other@example.com",
                    "GIT_AUTHOR_DATE": ts,
                    "GIT_COMMITTER_NAME": "Other",
                    "GIT_COMMITTER_EMAIL": "other@example.com",
                    "GIT_COMMITTER_DATE": ts,
                },
            )
            env.commit_count += 1

            topics = await run_upload_pipeline(env, self_authored_only=True)
            assert "foreign" not in topics.topics

    @async_test
    async def test_explicit_topic_bypasses_self_authored(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            ts = env._next_timestamp()
            await env.write_file("a.txt", "a")
            await env.git_ctx.git("add", "a.txt")
            await env.git_ctx.git(
                "commit",
                "-m",
                "feat\n\nTopic: foreign",
                "--allow-empty",
                env={
                    "GIT_AUTHOR_NAME": "Other",
                    "GIT_AUTHOR_EMAIL": "other@example.com",
                    "GIT_AUTHOR_DATE": ts,
                    "GIT_COMMITTER_NAME": "Other",
                    "GIT_COMMITTER_EMAIL": "other@example.com",
                    "GIT_COMMITTER_DATE": ts,
                },
            )
            env.commit_count += 1

            topics = await run_upload_pipeline(env, self_authored_only=True, topics=["foreign"])
            assert "foreign" in topics.topics
            review = topics.topics["foreign"].reviews["origin/main"]
            assert len(review.new_commits) == 1


class TestUploadCommitLabels:
    @async_test
    async def test_colon_prefix_label(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("bugfix: fix issue\n\nTopic: alpha", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            assert "bugfix" in topics.topics["alpha"].tags["label"]

    @async_test
    async def test_bracket_prefix_label(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("[feature] add thing\n\nTopic: alpha", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            assert "feature" in topics.topics["alpha"].tags["label"]

    @async_test
    async def test_labels_flag_adds_to_all_topics(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("b\n\nTopic: beta", {"b.txt": "b"})

            topics = await run_upload_pipeline(env, labels="urgent,p0")
            for name in ["alpha", "beta"]:
                assert "urgent" in topics.topics[name].tags["label"]
                assert "p0" in topics.topics[name].tags["label"]


class TestUploadTopicValidation:
    @async_test
    async def test_multiple_topics_per_commit_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nTopic: beta", {"a.txt": "a"})

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)

    @async_test
    async def test_invalid_topic_name_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: bad name with spaces", {"a.txt": "a"})

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)


class TestUploadCherryPickConflicts:
    @async_test
    async def test_same_file_different_topics_conflicts(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha", {"shared.txt": "content-a"})
            await env.commit("b\n\nTopic: beta", {"shared.txt": "content-b"})

            with pytest.raises(RevupConflictException):
                await run_upload_pipeline(env)

    @async_test
    async def test_relative_topic_resolves_same_file_conflict(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha", {"shared.txt": "content-a"})
            await env.commit("b\n\nTopic: beta\nRelative: alpha", {"shared.txt": "content-b"})

            topics = await run_upload_pipeline(env)

            alpha = topics.topics["alpha"].reviews["origin/main"]
            beta = topics.topics["beta"].reviews["origin/main"]

            alpha_content = await get_file_at_ref(env, alpha.new_commits[-1], "shared.txt")
            assert alpha_content == "content-a"

            beta_content = await get_file_at_ref(env, beta.new_commits[-1], "shared.txt")
            assert beta_content == "content-b"


class TestUploadBaseBranch:
    @async_test
    async def test_explicit_base_branch(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root_hash = await env.get_commit_hash()
            await env.git_ctx.git("branch", "origin/develop", "HEAD")
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, base_branch="develop")
            assert "origin/develop" in topics.topics["alpha"].reviews
            review = topics.topics["alpha"].reviews["origin/develop"]
            assert review.base_ref == root_hash
            assert review.remote_base == "develop"

    @async_test
    async def test_branch_tag_overrides_default_base(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("branch", "origin/release-1.0", "HEAD")
            root_hash = await env.get_commit_hash()
            await env.commit("feat\n\nTopic: alpha\nBranch: release-1.0", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            assert "origin/release-1.0" in topics.topics["alpha"].reviews
            assert "origin/main" not in topics.topics["alpha"].reviews
            review = topics.topics["alpha"].reviews["origin/release-1.0"]
            assert review.base_ref == root_hash


class TestUploadRelativeBranch:
    @async_test
    async def test_explicit_relative_branch_affects_fork_point(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("on main", {"a.txt": "a"})
            new_main = await env.get_commit_hash()
            await env.git_ctx.git("branch", "origin/main", new_main, "-f")
            await env.commit("diverge\n\nTopic: alpha", {"b.txt": "b"})

            topics = await run_upload_pipeline(env, relative_branch="main")
            review = topics.topics["alpha"].reviews["origin/main"]
            assert review.base_ref == new_main
            content = await get_file_at_ref(env, review.new_commits[-1], "b.txt")
            assert content == "b"

    @async_test
    async def test_relative_branch_tag_sets_review_field(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("branch", "origin/staging", "HEAD")
            await env.commit("feat\n\nTopic: alpha\nRelative-Branch: staging", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            assert review.relative_branch == "origin/staging"


class TestUploadHead:
    @async_test
    async def test_custom_head_limits_commits(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("first\n\nTopic: alpha", {"a.txt": "a"})
            target = await env.get_commit_hash()
            await env.commit("second\n\nTopic: beta", {"b.txt": "b"})

            topics = await run_upload_pipeline(env, head=str(target))
            assert "alpha" in topics.topics
            assert "beta" not in topics.topics
            review = topics.topics["alpha"].reviews["origin/main"]
            content = await get_file_at_ref(env, review.new_commits[-1], "a.txt")
            assert content == "a"


class TestUploadUpdatePrBody:
    @async_test
    async def test_invalid_update_pr_body_tag_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nUpdate-Pr-Body: maybe", {"a.txt": "a"})

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)


class TestUploadTopologicalOrder:
    @async_test
    async def test_topological_order_respects_relative_chain(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("a\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("b\n\nTopic: beta\nRelative: alpha", {"b.txt": "b"})
            await env.commit("c\n\nTopic: gamma\nRelative: beta", {"c.txt": "c"})

            topics = await run_upload_pipeline(env)
            order = [name for name, _ in topics.topological_topics()]
            assert order.index("alpha") < order.index("beta")
            assert order.index("beta") < order.index("gamma")


class TestUploadMultipleBranches:
    @async_test
    async def test_topic_with_two_branches_creates_two_reviews(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root_hash = await env.get_commit_hash()
            await env.git_ctx.git("branch", "origin/develop", "HEAD")
            await env.commit("feat\n\nTopic: alpha\nBranch: main, develop", {"a.txt": "hello"})

            topics = await run_upload_pipeline(env)

            assert "origin/main" in topics.topics["alpha"].reviews
            assert "origin/develop" in topics.topics["alpha"].reviews

            for branch in ["origin/main", "origin/develop"]:
                review = topics.topics["alpha"].reviews[branch]
                assert review.base_ref == root_hash
                content = await get_file_at_ref(env, review.new_commits[-1], "a.txt")
                assert content == "hello"
