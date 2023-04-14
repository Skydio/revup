from dataclasses import dataclass
from typing import Dict, List, NewType

# A bunch of commonly used type definitions.

# Represents a git commit, actually a commit-ish. Use "git rev-parse" to get the full hash.
GitCommitHash = NewType("GitCommitHash", str)

# Represents a git tree, actually a tree-ish. Use "git rev-parse" to get the full hash.
GitTreeHash = NewType("GitTreeHash", str)


@dataclass
class CommitHeader:
    """
    Represents the information extracted from `git rev-list --header`
    """

    tree: GitTreeHash
    parents: List[GitCommitHash]
    author_name: str = ""
    author_email: str = ""
    author_date: str = ""
    committer_name: str = ""
    committer_email: str = ""
    committer_date: str = ""
    commit_msg: str = ""
    title: str = ""
    commit_id: GitCommitHash = GitCommitHash("")


@dataclass
class GitConflict:
    type: str
    message: str
    paths: List[str]


# A conflict has appeared while doing a git operation. The higher level command
# will catch this so it can either handle it or re-raise.
class GitConflictException(Exception):
    def __init__(self, tree: GitTreeHash):
        self.tree = tree
        self.conflicts: List[GitConflict] = []


# Incorrect arguments or other usage error.
class RevupUsageException(Exception):
    pass


# An underlying GitConflictException happened but can't be handled.
class RevupConflictException(Exception):
    def __init__(
        self,
        commit: CommitHeader,
        parent: GitCommitHash,
        advice: str,
        commit_src: str = "",
        parent_src: str = "",
    ):
        self.message = (
            f'Failed to cherry-pick commit: "{commit.title}" ({commit.commit_id[:8]})'
            f"{commit_src} to new parent ({parent[:8]}){parent_src}.\n{advice}"
        )


class RevupShellException(Exception):
    pass


class RevupGithubException(Exception):
    def __init__(self, error_json: Dict):
        super().__init__()
        self.error_json = error_json
        messages = []
        self.types = []
        for error in self.error_json:
            self.types.append(error["type"] if "type" in error else "Unknown")
            messages.append(error["message"])

        self.type = " ".join(self.types) if self.types else "None"
        self.message = "\n".join(messages)


class RevupRequestException(Exception):
    def __init__(self, status: int, response: Dict):
        super().__init__()
        self.status = status
        self.response = response
