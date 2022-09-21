import configparser
import getpass
import os
import re


class Config:
    config: configparser.ConfigParser
    config_path: str

    def __init__(self, config_path: str, repo_config_path: str):
        self.config = configparser.ConfigParser()
        self.config_path = config_path
        self.repo_config_path = repo_config_path

    def read(self) -> None:
        write_back = False
        if not os.path.exists(self.config_path):
            write_back = True

        self.config.read(self.config_path)

        if not self.config.has_section("revup"):
            self.config.add_section("revup")
            write_back = True

        if not self.config.has_option("revup", "github_username"):
            github_username = input("GitHub username: ")
            # From https://www.npmjs.com/package/github-username-regex :
            # Github username may only contain alphanumeric characters or hyphens.
            # Github username cannot have multiple consecutive hyphens.
            # Github username cannot begin or end with a hyphen.
            # Maximum is 39 characters.
            if not re.match(r"^[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}$", github_username, re.I):
                raise ValueError(f"{github_username} is not a valid GitHub username")
            self.config.set("revup", "github_username", github_username)
            write_back = True

        if not self.config.has_option("revup", "github_oauth"):
            github_oauth = getpass.getpass(
                "GitHub OAuth token (make one at "
                "https://github.com/settings/tokens/new -- "
                'we need full "repo" permissions): '
            ).strip()
            self.config.set("revup", "github_oauth", github_oauth)
            write_back = True

        if write_back:
            # Ensure the file is created with secure permissions, due to containing credentials
            with os.fdopen(os.open(self.config_path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
                self.config.write(f)

        self.config.read(self.repo_config_path)

    def get_config(self) -> configparser.ConfigParser:
        return self.config
