import argparse

import pytest
from fake_forge import FakeForge
from git_env import GitTestEnvironment, async_test

from revup.forge import PrInfo
from revup.topic_stack import (
    PrBodySource,
    PrStatus,
    PushStatus,
    TopicStack,
    format_remote_branch,
)
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
        "skip_empty_first_commit": False,
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
    env.git_ctx.clear_cache()
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
    await topics.create_commits(args.trim_tags, args.skip_empty_first_commit)
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


class TestUploadTeamReviewers:
    @async_test
    async def test_team_reviewer_kept_on_reviewer_tag(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nReviewer: myorg/backend", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            assert "myorg/backend" in topics.topics["alpha"].tags["reviewer"]

    @async_test
    async def test_team_assignee_raises(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nAssignee: myorg/team, realuser", {"a.txt": "a"})

            with pytest.raises(RevupUsageException):
                await run_upload_pipeline(env)

    @async_test
    async def test_r2a_does_not_copy_team_into_assignees(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha\nReviewer: myorg/team, user1", {"a.txt": "a"})

            topics = await run_upload_pipeline(env, auto_add_users="r2a")
            assert topics.topics["alpha"].tags["assignee"] == {"user1"}
            assert "myorg/team" in topics.topics["alpha"].tags["reviewer"]


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


class TestPrBodySource:
    @async_test
    async def test_first_commit_uses_first_commit_body(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("title\n\nfirst body\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("second\n\nsecond body\n\nTopic: alpha", {"b.txt": "b"})

            topics = await run_upload_pipeline(env)
            topic = topics.topics["alpha"]
            body, title = topics._get_pr_body_and_title(topic, PrBodySource.FIRST_COMMIT)

            assert title == "title"
            assert "first body" in body
            assert "second body" not in body

    @async_test
    async def test_squashed_merges_all_commit_bodies(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("title one\n\nbody one\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("title two\n\nbody two\n\nTopic: alpha", {"b.txt": "b"})

            topics = await run_upload_pipeline(env)
            topic = topics.topics["alpha"]
            body, title = topics._get_pr_body_and_title(topic, PrBodySource.SQUASHED)

            assert "body one" in body
            assert "body two" in body

    @async_test
    async def test_squashed_strips_tags(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit(
                "feat\n\nreal content\n\nTopic: alpha\nReviewer: alice", {"a.txt": "a"}
            )

            topics = await run_upload_pipeline(env)
            topic = topics.topics["alpha"]
            body, title = topics._get_pr_body_and_title(topic, PrBodySource.SQUASHED)

            assert "alice" not in body
            assert "Topic" not in body
            assert "real content" in body

    @async_test
    async def test_template_reads_github_template(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            # Create a PR template
            await env.write_file(".github/PULL_REQUEST_TEMPLATE.md", "## Summary\n\n## Test Plan\n")

            topics = await run_upload_pipeline(env)
            topic = topics.topics["alpha"]
            body, title = topics._get_pr_body_and_title(topic, PrBodySource.TEMPLATE)

            assert "## Summary" in body
            assert "## Test Plan" in body
            assert title == "feat"

    @async_test
    async def test_template_returns_empty_when_no_template(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nbody text\n\nTopic: alpha", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            topic = topics.topics["alpha"]
            body, title = topics._get_pr_body_and_title(topic, PrBodySource.TEMPLATE)

            assert body == ""


def make_pr_info(review, base_branch="main"):
    """Create a PrInfo that simulates an existing PR matching the review's current state."""
    return PrInfo(
        baseRef=base_branch,
        headRef=review.remote_head,
        baseRefOid=review.base_ref,
        headRefOid=review.new_commits[-1],
        body="",
        title="",
        state="OPEN",
    )


class TestRebaseDetection:
    @async_test
    async def test_identical_commits_detected_as_nochange(self):
        """When local and remote are byte-for-byte identical, push should be skipped entirely."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = make_pr_info(review)

            await topics.mark_rebases(skip_rebase=True)

            assert review.push_status == PushStatus.NOCHANGE
            assert review.is_pure_rebase

    @async_test
    async def test_reworded_commit_is_not_pure_rebase(self):
        """Same diff but different commit message should still be pushed."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("original title\n\nTopic: alpha", {"a.txt": "a"})

            # First "upload" — capture the remote state
            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Reword the commit (same diff, different message)
            await env.git_ctx.git("commit", "--amend", "-m", "new title\n\nTopic: alpha")

            # Re-run pipeline with the reworded commit
            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=True)

            assert not review.is_pure_rebase
            assert review.push_status == PushStatus.PUSHED

    @async_test
    async def test_new_content_always_pushed(self):
        """A commit with new file changes must always be pushed."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "v1"})

            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Amend with different content
            await env.stage_file("a.txt", "v2")
            await env.git_ctx.git("commit", "--amend", "-m", "feat\n\nTopic: alpha")

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=True)

            assert not review.is_pure_rebase
            assert review.push_status == PushStatus.PUSHED

    @async_test
    async def test_skip_rebase_skips_pure_rebase_on_moved_base(self):
        """With skip_rebase=True, a pure rebase on a moved-forward base is marked REBASE."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root = await env.get_commit_hash()
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Advance origin/main independently, then rebase local onto it
            await env.git_ctx.git("checkout", root)
            await env.commit("upstream change", {"upstream.txt": "u"})
            new_main = await env.get_commit_hash()
            await env.git_ctx.git("branch", "origin/main", new_main, "-f")
            await env.git_ctx.git("checkout", "main")
            await env.git_ctx.git("rebase", "origin/main")

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=True)

            assert review.is_pure_rebase
            assert review.push_status == PushStatus.REBASE

    @async_test
    async def test_force_rebase_pushes_despite_pure_rebase(self):
        """With skip_rebase=False (--rebase flag), even a pure rebase gets pushed."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root = await env.get_commit_hash()
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Advance and rebase
            await env.git_ctx.git("checkout", root)
            await env.commit("upstream", {"upstream.txt": "u"})
            new_main = await env.get_commit_hash()
            await env.git_ctx.git("branch", "origin/main", new_main, "-f")
            await env.git_ctx.git("checkout", "main")
            await env.git_ctx.git("rebase", "origin/main")

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=False)

            assert review.is_pure_rebase
            assert review.push_status == PushStatus.PUSHED

    @async_test
    async def test_merged_pr_with_new_content_becomes_new(self):
        """If a PR was merged but local has genuinely new changes, it should be re-created."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "v1"})

            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Amend with new content
            await env.stage_file("a.txt", "v2")
            await env.git_ctx.git("commit", "--amend", "-m", "feat\n\nTopic: alpha")

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="MERGED",
            )
            review.status = PrStatus.MERGED

            await topics.mark_rebases(skip_rebase=True)

            assert review.status == PrStatus.NEW

    @async_test
    async def test_child_forces_parent_rebase_to_push(self):
        """When a child topic has new content, its rebased parent must also be pushed."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            root = await env.get_commit_hash()
            await env.commit("parent\n\nTopic: parent", {"p.txt": "p"})
            await env.commit("child\n\nTopic: child\nRelative: parent", {"c.txt": "c1"})

            first = await run_upload_pipeline(env)
            parent_review = first.topics["parent"].reviews["origin/main"]
            child_review = first.topics["child"].reviews["origin/main"]
            parent_remote_head = parent_review.new_commits[-1]
            parent_remote_base = parent_review.base_ref
            child_remote_head = child_review.new_commits[-1]
            child_remote_base = child_review.base_ref

            # Advance origin/main independently, then rebase local onto it
            await env.git_ctx.git("checkout", root)
            await env.commit("upstream", {"u.txt": "u"})
            new_main = await env.get_commit_hash()
            await env.git_ctx.git("branch", "origin/main", new_main, "-f")
            await env.git_ctx.git("checkout", "main")
            await env.git_ctx.git("rebase", "origin/main")
            # Amend child with new content
            await env.stage_file("c.txt", "c2")
            await env.git_ctx.git(
                "commit", "--amend", "-m", "child\n\nTopic: child\nRelative: parent"
            )

            topics = await run_upload_pipeline(env)
            p_review = topics.topics["parent"].reviews["origin/main"]
            c_review = topics.topics["child"].reviews["origin/main"]

            p_review.pr_info = PrInfo(
                baseRef="main",
                headRef=p_review.remote_head,
                baseRefOid=parent_remote_base,
                headRefOid=parent_remote_head,
                body="",
                title="",
                state="OPEN",
            )
            c_review.pr_info = PrInfo(
                baseRef=p_review.remote_head,
                headRef=c_review.remote_head,
                baseRefOid=child_remote_base,
                headRefOid=child_remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=True)

            # Child has new content, so it must be pushed
            assert c_review.push_status == PushStatus.PUSHED
            # Parent would normally be REBASE, but child forces it to PUSHED
            assert p_review.push_status == PushStatus.PUSHED

    @async_test
    async def test_multi_commit_topic_partial_change_is_not_rebase(self):
        """Changing one commit in a multi-commit topic means it's not a rebase."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("c1\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("c2\n\nTopic: alpha", {"b.txt": "b"})

            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Amend only the second commit (HEAD) with new content
            await env.stage_file("b.txt", "b_new")
            await env.git_ctx.git("commit", "--amend", "-m", "c2\n\nTopic: alpha")

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=True)

            assert not review.is_pure_rebase
            assert review.push_status == PushStatus.PUSHED

    @async_test
    async def test_commit_count_change_is_not_rebase(self):
        """Adding a commit to a topic means it cannot be a rebase of the old state."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("c1\n\nTopic: alpha", {"a.txt": "a"})

            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Add a second commit to the same topic
            await env.commit("c2\n\nTopic: alpha", {"b.txt": "b"})

            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=True)

            assert not review.is_pure_rebase
            assert review.push_status == PushStatus.PUSHED

    @async_test
    async def test_noop_on_base_detected_as_rebase(self):
        """A commit whose patch is a no-op when applied to the base should be detected as rebase.

        This happens when a commit's diff depends on a preceding local commit but produces
        the same tree as the base when cherry-picked independently. Patch-id would not match
        the remote, but merge-tree correctly identifies the result as equivalent.
        """
        async with GitTestEnvironment() as env:
            # Root has a.txt so cherry-pick onto base won't conflict
            await env.commit("root", {"root.txt": "r", "a.txt": "original"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")

            # Local commit A modifies a.txt
            await env.commit("setup\n\nTopic: setup", {"a.txt": "modified"})
            # Local commit B reverts a.txt back — a real diff locally, but a no-op on base
            await env.commit("revert\n\nTopic: alpha", {"a.txt": "original"})

            # First upload: alpha's cherry-pick onto base (which has a.txt="original") produces
            # a commit whose tree matches the base tree.
            first = await run_upload_pipeline(env)
            first_review = first.topics["alpha"].reviews["origin/main"]
            remote_head = first_review.new_commits[-1]
            remote_base = first_review.base_ref

            # Second run with the same commits — should detect as rebase
            topics = await run_upload_pipeline(env)
            review = topics.topics["alpha"].reviews["origin/main"]
            review.pr_info = PrInfo(
                baseRef="main",
                headRef=review.remote_head,
                baseRefOid=remote_base,
                headRefOid=remote_head,
                body="",
                title="",
                state="OPEN",
            )

            await topics.mark_rebases(skip_rebase=True)

            assert review.is_pure_rebase


class TestSkipEmptyFirstCommit:
    @async_test
    async def test_empty_first_commit_skipped(self):
        """An empty first commit should be excluded from the cherry-picked branch."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            # Empty commit (no diff) used for PR title/body
            await env.git_ctx.git("commit", "--allow-empty", "-m", "PR description\n\nTopic: feat")
            # Real commit with changes
            await env.commit("implement\n\nTopic: feat", {"a.txt": "content"})

            topics = await run_upload_pipeline(env, skip_empty_first_commit=True)

            review = topics.topics["feat"].reviews["origin/main"]
            assert len(review.new_commits) == 1
            content = await get_file_at_ref(env, review.new_commits[-1], "a.txt")
            assert content == "content"

    @async_test
    async def test_empty_first_commit_kept_when_disabled(self):
        """With the flag off, an empty first commit is included normally."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("commit", "--allow-empty", "-m", "PR description\n\nTopic: feat")
            await env.commit("implement\n\nTopic: feat", {"a.txt": "content"})

            topics = await run_upload_pipeline(env, skip_empty_first_commit=False)

            review = topics.topics["feat"].reviews["origin/main"]
            assert len(review.new_commits) == 2

    @async_test
    async def test_nonempty_first_commit_not_skipped(self):
        """A first commit with actual changes should never be skipped."""
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.commit("first\n\nTopic: feat", {"a.txt": "v1"})
            await env.commit("second\n\nTopic: feat", {"a.txt": "v2"})

            topics = await run_upload_pipeline(env, skip_empty_first_commit=True)

            review = topics.topics["feat"].reviews["origin/main"]
            assert len(review.new_commits) == 2


# --- Forge integration tests (using FakeForge) ---


def make_forge_upload_args(**kwargs):
    defaults = {
        "topics": [],
        "base_branch": None,
        "relative_branch": None,
        "rebase": False,
        "skip_confirm": True,
        "dry_run": False,
        "push_only": False,
        "status": False,
        "update_pr_body": True,
        "create_local_branches": False,
        "review_graph": True,
        "trim_tags": False,
        "patchsets": False,
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
        "skip_empty_first_commit": False,
        "verbose": False,
        "force_reviewers": False,
        "pr_body_source": PrBodySource.FIRST_COMMIT,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


async def full_upload_pipeline(env, forge, **kwargs):
    """
    Run the full upload pipeline including forge query, create, and update.
    Mirrors upload.py's logic but skips push (no real remote).
    """
    env.git_ctx.clear_cache()
    args = make_forge_upload_args(**kwargs)
    topics = TopicStack(
        env.git_ctx,
        args.base_branch,
        args.relative_branch,
        forge,
        args.head,
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
    await topics.query()
    await topics.fetch_git_refs()
    await topics.mark_rebases(not args.rebase)
    await topics.create_commits(args.trim_tags, args.skip_empty_first_commit)
    topics.populate_update_info(args.update_pr_body, args.force_reviewers, args.pr_body_source)
    if args.review_graph:
        topics.populate_review_graph()
    await topics.create_prs()
    await topics.update_prs()
    return topics


class TestForgeCreatePrs:
    @async_test
    async def test_new_topic_creates_pr(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("feat title\n\nbody text\n\nTopic: alpha", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            assert len(forge.created_prs) == 1
            pr = forge.created_prs[0]
            assert pr.title == "feat title"
            assert "body text" in pr.body
            assert pr.baseRef == "main"

    @async_test
    async def test_multiple_topics_create_separate_prs(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("alpha\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("beta\n\nTopic: beta", {"b.txt": "b"})

            await full_upload_pipeline(env, forge)

            assert len(forge.created_prs) == 2
            titles = {pr.title for pr in forge.created_prs}
            assert titles == {"alpha", "beta"}

    @async_test
    async def test_relative_topic_targets_parent_branch(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("parent\n\nTopic: parent", {"a.txt": "a"})
            await env.commit("child\n\nTopic: child\nRelative: parent", {"b.txt": "b"})

            await full_upload_pipeline(env, forge)

            parent_pr = next(pr for pr in forge.created_prs if pr.title == "parent")
            child_pr = next(pr for pr in forge.created_prs if pr.title == "child")
            assert parent_pr.baseRef == "main"
            assert child_pr.baseRef == parent_pr.headRef


class TestForgeUpdatePrs:
    @async_test
    async def test_second_upload_updates_existing_pr(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "v1"})

            await full_upload_pipeline(env, forge)
            assert len(forge.created_prs) == 1
            forge.created_prs.clear()

            await env.stage_file("a.txt", "v2")
            await env.git_ctx.git("commit", "--amend", "-m", "feat updated\n\nTopic: alpha")

            await full_upload_pipeline(env, forge)

            assert len(forge.created_prs) == 0
            assert any(u.title == "feat updated" for u in forge.updated_prs)

    @async_test
    async def test_update_pr_body_false_skips_body_update(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("title\n\noriginal body\n\nTopic: alpha", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            await env.git_ctx.git("commit", "--amend", "-m", "title\n\nnew body\n\nTopic: alpha")
            forge.updated_prs.clear()

            await full_upload_pipeline(env, forge, update_pr_body=False)

            body_updates = [u for u in forge.updated_prs if u.body is not None]
            assert len(body_updates) == 0

    @async_test
    async def test_update_pr_body_tag_overrides_flag(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("title\n\nbody\n\nTopic: alpha\nUpdate-Pr-Body: false", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            await env.git_ctx.git(
                "commit",
                "--amend",
                "-m",
                "title\n\nnew body\n\nTopic: alpha\nUpdate-Pr-Body: false",
            )
            forge.updated_prs.clear()

            await full_upload_pipeline(env, forge, update_pr_body=True)

            body_updates = [u for u in forge.updated_prs if u.body is not None]
            assert len(body_updates) == 0


class TestForgeReviewers:
    @async_test
    async def test_reviewers_resolved_to_ids(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(users={"alice": ("id_alice", "alice-full")})
            await env.commit("feat\n\nTopic: alpha\nReviewer: alice", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            assert len(forge.updated_prs) > 0
            update = forge.updated_prs[0]
            assert "id_alice" in update.reviewer_ids

    @async_test
    async def test_unknown_reviewer_silently_skipped(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(users={"alice": ("id_alice", "alice-full")})
            await env.commit("feat\n\nTopic: alpha\nReviewer: alice, ghost", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            update = forge.updated_prs[0]
            assert "id_alice" in update.reviewer_ids
            assert len(update.reviewer_ids) == 1

    @async_test
    async def test_removed_reviewer_not_re_added(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(users={"bob": ("id_bob", "bob-full")})
            await env.commit("feat\n\nTopic: alpha\nReviewer: bob", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            # Simulate bob being removed on the forge
            pr = list(forge.prs.values())[0]
            pr.reviewers = set()
            pr.reviewer_ids = set()
            pr.removed_reviewers = {"bob-full"}
            pr.removed_reviewer_ids = {"id_bob"}
            forge.updated_prs.clear()

            await env.stage_file("a.txt", "a2")
            await env.git_ctx.git("commit", "--amend", "-m", "feat\n\nTopic: alpha\nReviewer: bob")

            await full_upload_pipeline(env, forge)

            reviewer_ids = set()
            for u in forge.updated_prs:
                reviewer_ids |= u.reviewer_ids
            assert "id_bob" not in reviewer_ids

    @async_test
    async def test_force_reviewers_re_adds_removed(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(users={"bob": ("id_bob", "bob-full")})
            await env.commit("feat\n\nTopic: alpha\nReviewer: bob", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            pr = list(forge.prs.values())[0]
            pr.reviewers = set()
            pr.reviewer_ids = set()
            pr.removed_reviewers = {"bob-full"}
            pr.removed_reviewer_ids = {"id_bob"}
            forge.updated_prs.clear()

            await env.stage_file("a.txt", "a2")
            await env.git_ctx.git("commit", "--amend", "-m", "feat\n\nTopic: alpha\nReviewer: bob")

            await full_upload_pipeline(env, forge, force_reviewers=True)

            reviewer_ids = set()
            for u in forge.updated_prs:
                reviewer_ids |= u.reviewer_ids
            assert "id_bob" in reviewer_ids

    @async_test
    async def test_team_reviewer_resolved(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(teams={"myorg/backend": ("team_1", {"alice", "bob"})})
            await env.commit("feat\n\nTopic: alpha\nReviewer: myorg/backend", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            update = forge.updated_prs[0]
            assert "team_1" in update.reviewer_team_ids

    @async_test
    async def test_team_not_re_requested_if_member_reviewing(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(teams={"myorg/backend": ("team_1", {"alice", "bob"})})
            await env.commit("feat\n\nTopic: alpha\nReviewer: myorg/backend", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            # Simulate: team resolved, alice is now a reviewer
            pr = list(forge.prs.values())[0]
            pr.reviewers = {"alice"}
            pr.reviewer_ids = {"id_alice"}
            pr.reviewer_teams = set()
            pr.reviewer_team_ids = set()
            forge.updated_prs.clear()

            await env.stage_file("a.txt", "a2")
            await env.git_ctx.git(
                "commit",
                "--amend",
                "-m",
                "feat\n\nTopic: alpha\nReviewer: myorg/backend",
            )

            await full_upload_pipeline(env, forge)

            team_ids = set()
            for u in forge.updated_prs:
                team_ids |= u.reviewer_team_ids
            assert "team_1" not in team_ids


class TestForgeLabels:
    @async_test
    async def test_labels_resolved_to_ids(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(labels={"bug": "label_1", "main": "label_main"})
            await env.commit("feat\n\nTopic: alpha\nLabel: bug", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            update = forge.updated_prs[0]
            assert "label_1" in update.label_ids

    @async_test
    async def test_base_branch_label_auto_added(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(labels={"main": "label_main"})
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            update = forge.updated_prs[0]
            assert "label_main" in update.label_ids

    @async_test
    async def test_draft_label_creates_draft_pr(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("feat\n\nTopic: alpha\nLabel: draft", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            pr = forge.created_prs[0]
            assert pr.is_draft is True


class TestForgeReviewGraph:
    @async_test
    async def test_review_graph_comment_created_for_chain(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("parent\n\nTopic: parent", {"a.txt": "a"})
            await env.commit("child\n\nTopic: child\nRelative: parent", {"b.txt": "b"})

            await full_upload_pipeline(env, forge, review_graph=True)

            comments = []
            for u in forge.updated_prs:
                comments.extend(u.comments)
            graph_comments = [c for c in comments if "Reviews in this chain" in c.text]
            assert len(graph_comments) >= 2

    @async_test
    async def test_no_review_graph_when_disabled(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("parent\n\nTopic: parent", {"a.txt": "a"})
            await env.commit("child\n\nTopic: child\nRelative: parent", {"b.txt": "b"})

            await full_upload_pipeline(env, forge, review_graph=False)

            comments = []
            for u in forge.updated_prs:
                comments.extend(u.comments)
            graph_comments = [c for c in comments if "Reviews in this chain" in c.text]
            assert len(graph_comments) == 0


class TestForgePrBodySource:
    @async_test
    async def test_squashed_body_on_create(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("title\n\nfirst body\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("second\n\nsecond body\n\nTopic: alpha", {"b.txt": "b"})

            await full_upload_pipeline(env, forge, pr_body_source=PrBodySource.SQUASHED)

            pr = forge.created_prs[0]
            assert "first body" in pr.body
            assert "second body" in pr.body

    @async_test
    async def test_template_body_on_create(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.write_file(".github/PULL_REQUEST_TEMPLATE.md", "## Summary\n\n## Tests\n")
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            await full_upload_pipeline(env, forge, pr_body_source=PrBodySource.TEMPLATE)

            pr = forge.created_prs[0]
            assert "## Summary" in pr.body


class TestForgeMergedPr:
    @async_test
    async def test_merged_pr_with_same_content_stays_merged(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            await full_upload_pipeline(env, forge, review_graph=False)

            pr = list(forge.prs.values())[0]
            pr.state = "MERGED"
            forge.created_prs.clear()

            topics = await full_upload_pipeline(env, forge, review_graph=False)

            assert len(forge.created_prs) == 0
            review = topics.topics["alpha"].reviews["origin/main"]
            assert review.status == PrStatus.MERGED

    @async_test
    async def test_merged_pr_with_new_content_creates_new(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "v1"})

            await full_upload_pipeline(env, forge, review_graph=False)

            pr = list(forge.prs.values())[0]
            pr.state = "MERGED"
            forge.created_prs.clear()

            await env.stage_file("a.txt", "v2")
            await env.git_ctx.git("commit", "--amend", "-m", "feat\n\nTopic: alpha")

            await full_upload_pipeline(env, forge, review_graph=False)

            assert len(forge.created_prs) == 1


class TestForgeNumReviewsChanged:
    @async_test
    async def test_no_change_means_zero(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            await full_upload_pipeline(env, forge, review_graph=False)
            forge.created_prs.clear()
            forge.updated_prs.clear()

            topics = await full_upload_pipeline(env, forge, review_graph=False)
            assert topics.num_reviews_changed() == 0

    @async_test
    async def test_new_topic_counted(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge()
            await env.commit("feat\n\nTopic: alpha", {"a.txt": "a"})

            topics = await full_upload_pipeline(env, forge, review_graph=False)
            assert topics.num_reviews_changed() == 1


class TestForgeAssignees:
    @async_test
    async def test_assignees_resolved(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(users={"bob": ("id_bob", "bob-full")})
            await env.commit("feat\n\nTopic: alpha\nAssignee: bob", {"a.txt": "a"})

            await full_upload_pipeline(env, forge)

            update = forge.updated_prs[0]
            assert "id_bob" in update.assignee_ids

    @async_test
    async def test_auto_add_users_r2a(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(users={"alice": ("id_alice", "alice-full")})
            await env.commit("feat\n\nTopic: alpha\nReviewer: alice", {"a.txt": "a"})

            await full_upload_pipeline(env, forge, auto_add_users="r2a")

            update = forge.updated_prs[0]
            assert "id_alice" in update.assignee_ids
            assert "id_alice" in update.reviewer_ids

    @async_test
    async def test_auto_add_users_a2r(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            forge = FakeForge(users={"bob": ("id_bob", "bob-full")})
            await env.commit("feat\n\nTopic: alpha\nAssignee: bob", {"a.txt": "a"})

            await full_upload_pipeline(env, forge, auto_add_users="a2r")

            update = forge.updated_prs[0]
            assert "id_bob" in update.reviewer_ids
            assert "id_bob" in update.assignee_ids
