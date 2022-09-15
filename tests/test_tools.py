import asyncio
import builtins
import dataclasses
import io
import sys
from unittest import mock

import pytest

from revup import revup


def mock_revup(args, user_input) -> str:
    # The first arg is always the program path.
    args = ["revup"] + args

    # We want to ensure that no connection to github is established (as we are
    # not actually githubbing)!
    mock.patch("revup.revup.github_connection")
    # User input mocks the user typing things into the terminal.
    user_input = list(user_input) if isinstance(user_input, list) else [user_input]
    with mock.patch.object(sys, "argv", args):
        with mock.patch.object(builtins, "input", lambda x: user_input.pop(0)):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                # Ensure we finish main.
                loop = asyncio.get_event_loop()
                coroutine = revup.main()
                loop.run_until_complete(coroutine)
                output = sys.stdout.getvalue()

    # This is the stdout output.
    return output


def test_help_menu(mocker):
    # Test the default help menu.
    help_action = mocker.patch("revup.revup.HelpAction", wraps=revup.HelpAction)
    with pytest.raises(SystemExit):
        mock_revup(["-h"], [])
    help_action.assert_called()

    # Test the help menu stops code flow where expected.
    help_action.reset_mock()
    upload_main = mocker.patch("revup.upload.main")
    with pytest.raises(SystemExit):
        mock_revup(["upload", "-h"], [])
    help_action.assert_called()
    upload_main.assert_not_called()
