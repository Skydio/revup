from typing import NewType

# A bunch of commonly used type definitions.

# Represents a git commit, actually a commit-ish. Use "git rev-parse" to get the full hash.
GitCommitHash = NewType("GitCommitHash", str)

# Represents a git tree, actually a tree-ish. Use "git rev-parse" to get the full hash.
GitTreeHash = NewType("GitTreeHash", str)


# A conflict has appeared while doing a git operation. The higher level command
# will catch this so it can deliver case specific advice to the user.
class GitConflictException(Exception):
    pass


# Incorrect arguments or other usage error.
class RevupUsageException(Exception):
    pass


class RevupConflictException(Exception):
    pass
