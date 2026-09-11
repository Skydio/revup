import asyncio
import builtins
import configparser
import io
import sys
from unittest import mock

import pytest

from revup import revup


def mock_revup(args, user_input) -> str:
    # The first arg is always the program path.
    args = ["revup"] + args

    # We want to ensure that no connection to the forge is established
    mock.patch("revup.revup.forge_connection")
    # User input mocks the user typing things into the terminal.
    user_input = list(user_input) if isinstance(user_input, list) else [user_input]
    with mock.patch.object(sys, "argv", args):
        with mock.patch.object(builtins, "input", lambda x: user_input.pop(0)):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                revup_parser, all_parsers = revup.build_parser()
                asyncio.run(revup.main(revup_parser, all_parsers))
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


def test_trim_tags_values():
    revup_parser, all_parsers = revup.build_parser()
    assert revup_parser.parse_args(["upload"]).trim_tags == "false"
    assert revup_parser.parse_args(["upload", "--trim-tags=true"]).trim_tags == "true"
    assert (
        revup_parser.parse_args(["upload", "--trim-tags=nonidentifying"]).trim_tags
        == "nonidentifying"
    )
    with pytest.raises(SystemExit):
        revup_parser.parse_args(["upload", "--trim-tags=bogus"])
    # The value is mandatory, so a topic name can never be mistaken for it
    with pytest.raises(SystemExit):
        revup_parser.parse_args(["upload", "--trim-tags"])

    # Config accepts the same values
    upload_parser = next(p for p in all_parsers if p.get_command() == "upload")
    config = configparser.ConfigParser()
    config.read_string("[upload]\ntrim_tags = nonidentifying\n")
    upload_parser.set_defaults_from_config(config)
    assert upload_parser.parse_args([]).trim_tags == "nonidentifying"
