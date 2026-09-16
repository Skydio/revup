"""Tests for logging backends, especially verbose logs to file."""

import logging
import os

import pytest
from rich.logging import RichHandler

from revup import logs, shell
from revup.version import REVUP_VERSION


@pytest.fixture(autouse=True)
def clean_logger():
    """Save and restore global logging state, since configure_logger mutates it."""
    root_logger = logging.getLogger()
    old_handlers = list(root_logger.handlers)
    old_level = root_logger.level
    root_logger.handlers = []
    yield
    for h in root_logger.handlers:
        h.close()
    root_logger.handlers = old_handlers
    root_logger.setLevel(old_level)


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "revup" / "logs"


def get_log_file_path():
    handler = logs.find_handler(logging.FileHandler)
    return handler.baseFilename if handler is not None else None


def read_log_file():
    path = get_log_file_path()
    assert path is not None, "No log file was created"
    with open(path, encoding="utf-8") as f:
        return f.read()


def get_console_handler():
    handler = logs.find_handler(RichHandler)
    assert handler is not None, "No console handler was installed"
    return handler


class TestFileLoggingDisabled:
    def test_no_file_created_by_default(self, log_dir):
        logs.configure_logger(debug=False, redactions={})
        logging.debug("shouldn't be logged anywhere")

        assert get_log_file_path() is None
        assert not log_dir.exists()

    def test_debug_logs_dropped_by_default(self, log_dir):
        logs.configure_logger(debug=False, redactions={})
        assert not logging.getLogger().isEnabledFor(logging.DEBUG)
        assert not shell.debug_enabled()

    def test_verbose_alone_writes_no_file(self, log_dir):
        logs.configure_logger(debug=True, redactions={})
        assert get_log_file_path() is None
        assert not log_dir.exists()


class TestFileLoggingEnabled:
    def test_debug_logs_go_to_file(self, log_dir):
        logs.configure_logger(debug=False, redactions={}, log_to_file=True)
        logging.debug("a verbose detail")
        logging.info("something normal")

        contents = read_log_file()
        assert "a verbose detail" in contents
        assert "something normal" in contents

    def test_console_still_hides_debug(self, log_dir):
        logs.configure_logger(debug=False, redactions={}, log_to_file=True)

        # The root logger must pass debug records for the file backend to see them,
        # but the console must not show them.
        assert logging.getLogger().isEnabledFor(logging.DEBUG)
        assert shell.debug_enabled()
        assert get_console_handler().level == logging.INFO

    def test_verbose_shows_debug_on_console(self, log_dir):
        logs.configure_logger(debug=True, redactions={}, log_to_file=True)
        assert get_console_handler().level == logging.DEBUG

    def test_file_is_in_xdg_state_home(self, log_dir):
        logs.configure_logger(debug=False, redactions={}, log_to_file=True)
        path = get_log_file_path()
        assert path is not None
        assert os.path.dirname(path) == str(log_dir)
        assert path.endswith(".log")

    def test_falls_back_to_local_state(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        logs.configure_logger(debug=False, redactions={}, log_to_file=True)

        assert os.path.dirname(get_log_file_path()) == str(
            tmp_path / ".local" / "state" / "revup" / "logs"
        )

    def test_command_line_is_recorded(self, log_dir):
        logs.configure_logger(debug=False, redactions={}, log_to_file=True)
        assert "revup {}".format(REVUP_VERSION) in read_log_file()

    def test_redactions_apply_to_file(self, log_dir):
        logs.configure_logger(debug=False, redactions={"secret_token": "<OAUTH>"}, log_to_file=True)
        logging.debug("using secret_token to connect")

        contents = read_log_file()
        assert "secret_token" not in contents
        assert "<OAUTH>" in contents

    def test_later_redactions_apply_to_file(self, log_dir):
        logs.configure_logger(debug=False, redactions={}, log_to_file=True)
        logs.redact({"found_later": "<OAUTH>"})
        logging.debug("using found_later to connect")

        contents = read_log_file()
        assert "found_later" not in contents
        assert "<OAUTH>" in contents

    def test_reconfigure_doesnt_add_second_file(self, log_dir):
        logs.configure_logger(debug=False, redactions={})
        logs.configure_logger(debug=False, redactions={}, log_to_file=True)
        logs.configure_logger(debug=False, redactions={}, log_to_file=True)

        assert len(list(log_dir.iterdir())) == 1
        assert len([h for h in logging.getLogger().handlers if isinstance(h, RichHandler)]) == 1

    def test_unwritable_dir_doesnt_raise(self, log_dir):
        # A file where the log directory should be makes makedirs fail.
        log_dir.parent.mkdir(parents=True)
        log_dir.write_text("not a directory")

        logs.configure_logger(debug=False, redactions={}, log_to_file=True)

        assert get_log_file_path() is None
        logging.info("still works")


class TestPruning:
    def make_logs(self, log_dir, count):
        """Create `count` log files whose names sort oldest to newest."""
        log_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, count + 1):
            (log_dir / "202601{:02d}T000000-1.log".format(i)).write_text("old")

    def test_prunes_to_keep_count(self, log_dir):
        self.make_logs(log_dir, 5)
        logs.prune_old_logs(str(log_dir), 2)

        names = sorted(f.name for f in log_dir.iterdir())
        assert names == ["20260104T000000-1.log", "20260105T000000-1.log"]

    def test_keeps_all_when_under_limit(self, log_dir):
        self.make_logs(log_dir, 2)
        logs.prune_old_logs(str(log_dir), 5)
        assert len(list(log_dir.iterdir())) == 2

    def test_ignores_other_files(self, log_dir):
        self.make_logs(log_dir, 3)
        (log_dir / "notes.txt").write_text("keep me")
        logs.prune_old_logs(str(log_dir), 1)

        names = sorted(f.name for f in log_dir.iterdir())
        assert names == ["20260103T000000-1.log", "notes.txt"]

    def test_missing_dir_doesnt_raise(self, log_dir):
        logs.prune_old_logs(str(log_dir / "nonexistent"), 1)

    def test_configure_keeps_twenty_runs_of_history(self, log_dir):
        self.make_logs(log_dir, 25)

        logs.configure_logger(debug=False, redactions={}, log_to_file=True)

        names = sorted(f.name for f in log_dir.iterdir())
        assert len(names) == 20
        # The oldest were deleted, and this run's file is one of the 20.
        assert names[0] == "20260107T000000-1.log"
        assert os.path.basename(get_log_file_path()) in names
