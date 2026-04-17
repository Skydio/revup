import os

import pytest

from revup import revup as revup_mod
from tests.git_env import GitTestEnvironment, async_test

COMPLETIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(revup_mod.__file__)), "completions")


class TestCompleteShellOutput:
    @async_test
    async def test_bash_script_output(self):
        import io
        from unittest.mock import patch

        with patch("sys.argv", ["revup", "_complete", "--shell", "bash"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = await revup_mod.main()

        output = mock_out.getvalue()
        assert ret == 0
        assert "complete -F _revup revup" in output
        assert "_revup()" in output

    @async_test
    async def test_zsh_script_output(self):
        import io
        from unittest.mock import patch

        with patch("sys.argv", ["revup", "_complete", "--shell", "zsh"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = await revup_mod.main()

        output = mock_out.getvalue()
        assert ret == 0
        assert "#compdef revup" in output
        assert "_revup()" in output

    @async_test
    async def test_fish_script_output(self):
        import io
        from unittest.mock import patch

        with patch("sys.argv", ["revup", "_complete", "--shell", "fish"]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = await revup_mod.main()

        output = mock_out.getvalue()
        assert ret == 0
        assert "complete -c revup" in output
        assert "__revup_topics" in output


class TestCompletionScriptContent:
    def _read_script(self, name):
        with open(os.path.join(COMPLETIONS_DIR, name)) as f:
            return f.read()

    def test_bash_uses_list_topics(self):
        script = self._read_script("revup.bash")
        assert "revup toolkit list-topics" in script

    def test_zsh_uses_list_topics(self):
        script = self._read_script("revup.zsh")
        assert "revup toolkit list-topics" in script

    def test_fish_uses_list_topics(self):
        script = self._read_script("revup.fish")
        assert "revup toolkit list-topics" in script

    def test_bash_completes_amend_flags(self):
        script = self._read_script("revup.bash")
        for flag in [
            "--no-edit",
            "--insert",
            "--drop",
            "--all",
            "--no-parse-topics",
            "--no-parse-refs",
        ]:
            assert flag in script

    def test_bash_completes_subcommands(self):
        script = self._read_script("revup.bash")
        for cmd in ["upload", "amend", "commit", "restack", "cherry-pick", "config", "toolkit"]:
            assert cmd in script

    def test_zsh_completes_amend_flags(self):
        script = self._read_script("revup.zsh")
        for flag in [
            "--no-edit",
            "--insert",
            "--drop",
            "--all",
            "--no-parse-topics",
            "--no-parse-refs",
        ]:
            assert flag in script

    def test_fish_completes_amend_flags(self):
        script = self._read_script("revup.fish")
        for flag in ["no-edit", "insert", "drop"]:
            assert flag in script

    def test_completion_scripts_exist(self):
        for name in ["revup.bash", "revup.zsh", "revup.fish"]:
            assert os.path.isfile(os.path.join(COMPLETIONS_DIR, name))


class TestListTopicsForCompletions:
    @async_test
    async def test_list_topics_outputs_topic_names(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")

            await env.commit("first\n\nTopic: alpha", {"a.txt": "a"})
            await env.commit("second\n\nTopic: beta", {"b.txt": "b"})

            from revup.topic_stack import TopicStack

            topics = TopicStack(env.git_ctx, None, None)
            await topics.populate_topics()
            names = list(topics.topics.keys())

            assert "alpha" in names
            assert "beta" in names

    @async_test
    async def test_list_topics_empty_when_no_topics(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")

            await env.commit("no topic here", {"a.txt": "a"})

            from revup.topic_stack import TopicStack

            topics = TopicStack(env.git_ctx, None, None)
            await topics.populate_topics()

            assert len(topics.topics) == 0

    @async_test
    async def test_list_topics_multiple_commits_same_topic(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("branch", "origin/main", "HEAD")

            await env.commit("first\n\nTopic: feature", {"a.txt": "a"})
            await env.commit("second\n\nTopic: feature", {"b.txt": "b"})

            from revup.topic_stack import TopicStack

            topics = TopicStack(env.git_ctx, None, None)
            await topics.populate_topics()
            names = list(topics.topics.keys())

            assert names == ["feature"]
            assert len(topics.topics["feature"].original_commits) == 2
