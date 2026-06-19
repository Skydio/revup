"""Tests for the config command and Config class."""

import argparse
import configparser
import os
import tempfile

import pytest

from revup.config import Config, RevupArgParser, collect_known_keys, config_main
from revup.revup import repo_tool_config_from_git_dir
from revup.types import RevupUsageException


def make_parsers():
    """Create a minimal set of parsers mimicking revup's real structure."""
    revup_parser = RevupArgParser(prog="revup")
    revup_parser.add_argument("--forge-url", default="github.com")
    revup_parser.add_argument("--forge-oauth", default="")
    revup_parser.add_argument("--verbose", action="store_true")

    upload_parser = RevupArgParser(prog="revup upload")
    upload_parser.add_argument("--auto-topic", action="store_true")
    upload_parser.add_argument("--remote-name", default="origin")
    upload_parser.add_argument("--strategy", choices=["merge", "rebase"], default="merge")

    return [revup_parser, upload_parser]


def make_config_args(flag, value=None, repo=False, delete=False):
    return argparse.Namespace(flag=[flag], value=value, repo=repo, delete=delete)


class TestConfigSetAndRead:
    def test_set_value_creates_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()

            conf.set_value("revup", "forge_url", "github.enterprise.com")
            conf.write()

            conf2 = Config(path)
            conf2.read()
            assert conf2.config.get("revup", "forge_url") == "github.enterprise.com"

    def test_set_value_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            conf.set_value("revup", "key", "val")
            conf.write()

            conf2 = Config(path)
            conf2.read()
            conf2.set_value("revup", "key", "val")
            assert conf2.dirty is False

    def test_delete_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            conf.set_value("upload", "remote_name", "upstream")
            conf.write()

            conf2 = Config(path)
            conf2.read()
            conf2.set_value("upload", "remote_name", None)
            conf2.write()

            conf3 = Config(path)
            conf3.read()
            assert not conf3.config.has_option("upload", "remote_name")

    def test_delete_nonexistent_key_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            conf.set_value("revup", "nonexistent", None)
            assert conf.dirty is False

    def test_write_creates_file_with_secure_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            conf.set_value("revup", "forge_oauth", "secret123")
            conf.write()

            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600

    def test_repo_config_overrides_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_path = os.path.join(tmp, "user_config")
            repo_path = os.path.join(tmp, "repo_config")

            user_conf = Config(user_path)
            user_conf.read()
            user_conf.set_value("revup", "forge_url", "user.github.com")
            user_conf.write()

            repo_conf = Config(repo_path)
            repo_conf.read()
            repo_conf.set_value("revup", "forge_url", "repo.github.com")
            repo_conf.write()

            # Reading repo then user means user wins (ConfigParser last-read-wins)
            combined = Config(user_path, repo_config_path=repo_path)
            combined.read()
            # repo is read first, then user overrides
            assert combined.config.get("revup", "forge_url") == "user.github.com"


class TestConfigMain:
    def test_set_simple_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("forge_url", "custom.github.com")
            ret = config_main(conf, args, parsers)

            assert ret == 0
            written = Config(path)
            written.read()
            assert written.config.get("revup", "forge_url") == "custom.github.com"

    def test_set_dotted_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("upload.remote_name", "upstream")
            ret = config_main(conf, args, parsers)

            assert ret == 0
            written = Config(path)
            written.read()
            assert written.config.get("upload", "remote_name") == "upstream"

    def test_set_boolean_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("upload.auto_topic", "true")
            ret = config_main(conf, args, parsers)

            assert ret == 0
            written = Config(path)
            written.read()
            assert written.config.get("upload", "auto_topic") == "true"

    def test_delete_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            conf.set_value("revup", "forge_url", "old.com")
            conf.write()

            conf2 = Config(path)
            conf2.read()
            parsers = make_parsers()

            args = make_config_args("forge_url", delete=True)
            ret = config_main(conf2, args, parsers)

            assert ret == 0
            written = Config(path)
            written.read()
            assert not written.config.has_option("revup", "forge_url")

    def test_invalid_command_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("nonexistent_cmd.key", "val")
            with pytest.raises(RevupUsageException, match="Invalid command section"):
                config_main(conf, args, parsers)

    def test_invalid_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("upload.nonexistent_key", "val")
            with pytest.raises(RevupUsageException, match="Invalid option key"):
                config_main(conf, args, parsers)

    def test_too_many_dots_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("a.b.c", "val")
            with pytest.raises(RevupUsageException, match="Invalid flag argument"):
                config_main(conf, args, parsers)

    def test_delete_with_value_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("forge_url", value="x", delete=True)
            with pytest.raises(RevupUsageException, match="Can't provide a value"):
                config_main(conf, args, parsers)

    def test_invalid_boolean_value_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("verbose", "notabool")
            with pytest.raises(ValueError, match="not a valid override for boolean flag"):
                config_main(conf, args, parsers)

    def test_invalid_choice_value_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("upload.strategy", "squash")
            with pytest.raises(ValueError, match="not one of the choices"):
                config_main(conf, args, parsers)

    def test_hyphen_to_underscore_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config")
            conf = Config(path)
            conf.read()
            parsers = make_parsers()

            args = make_config_args("forge-url", "normalized.com")
            ret = config_main(conf, args, parsers)

            assert ret == 0
            written = Config(path)
            written.read()
            assert written.config.get("revup", "forge_url") == "normalized.com"


class TestApplyToParsers:
    def test_applies_config_values_as_defaults(self):
        parsers = make_parsers()
        config = configparser.ConfigParser()
        config.read_string("[upload]\nremote_name = upstream\n")

        parsers[1].set_defaults_from_config(config)
        args = parsers[1].parse_args([])
        assert args.remote_name == "upstream"

    def test_boolean_config_applied(self):
        parsers = make_parsers()
        config = configparser.ConfigParser()
        config.read_string("[upload]\nauto_topic = true\n")

        parsers[1].set_defaults_from_config(config)
        args = parsers[1].parse_args([])
        assert args.auto_topic is True


class TestCollectKnownKeys:
    def test_collects_all_parser_keys(self):
        parsers = make_parsers()
        known = collect_known_keys(parsers)

        assert "revup" in known
        assert "upload" in known
        assert "forge_url" in known["revup"]
        assert "forge_oauth" in known["revup"]
        assert "auto_topic" in known["upload"]
        assert "remote_name" in known["upload"]


class TestRepoToolDetection:
    def test_git_dir_under_repo_returns_checkout_root_config(self):
        # .git is a symlink straight into .repo/projects.
        git_dir = "/work/checkout/.repo/projects/foo/bar.git"
        assert repo_tool_config_from_git_dir(git_dir) == "/work/checkout/.revupconfig"

    def test_objects_under_project_objects_returns_checkout_root_config(self):
        # .git is a dir of symlinks whose objects resolve into project-objects.
        objects = "/work/checkout/.repo/project-objects/foo-bar.git/objects"
        assert repo_tool_config_from_git_dir(objects) == "/work/checkout/.revupconfig"

    def test_non_repo_git_dir_returns_none(self):
        assert repo_tool_config_from_git_dir("/home/me/project/.git") is None

    def test_repo_substring_in_name_is_not_matched(self):
        # A directory merely containing the text ".repo" is not a path component.
        assert repo_tool_config_from_git_dir("/home/me/my.repository/.git") is None


def write_main_branch(path, main_branch):
    conf = Config(path)
    conf.read()
    conf.set_value("revup", "main_branch", main_branch)
    conf.write()


class TestRepoToolConfigPrecedence:
    def test_repo_config_overrides_repo_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = os.path.join(tmp, "repo_config")
            shared_path = os.path.join(tmp, "shared_config")
            write_main_branch(repo_path, "repo-branch")
            write_main_branch(shared_path, "shared-branch")

            conf = Config("", repo_config_path=repo_path, repo_tool_config_path=shared_path)
            conf.read()
            assert conf.config.get("revup", "main_branch") == "repo-branch"

    def test_repo_tool_used_when_repo_has_no_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = os.path.join(tmp, "repo_config")
            shared_path = os.path.join(tmp, "shared_config")
            write_main_branch(shared_path, "shared-branch")

            conf = Config("", repo_config_path=repo_path, repo_tool_config_path=shared_path)
            conf.read()
            assert conf.config.get("revup", "main_branch") == "shared-branch"

    def test_user_config_overrides_repo_and_repo_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_path = os.path.join(tmp, "user_config")
            repo_path = os.path.join(tmp, "repo_config")
            shared_path = os.path.join(tmp, "shared_config")
            write_main_branch(user_path, "user-branch")
            write_main_branch(repo_path, "repo-branch")
            write_main_branch(shared_path, "shared-branch")

            conf = Config(user_path, repo_config_path=repo_path, repo_tool_config_path=shared_path)
            conf.read()
            assert conf.config.get("revup", "main_branch") == "user-branch"

    def test_no_repo_tool_config_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = os.path.join(tmp, "repo_config")
            write_main_branch(repo_path, "repo-branch")

            conf = Config("", repo_config_path=repo_path, repo_tool_config_path=None)
            conf.read()
            assert conf.config.get("revup", "main_branch") == "repo-branch"
