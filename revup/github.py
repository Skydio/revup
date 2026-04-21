from abc import ABCMeta, abstractmethod
from typing import Any

# Number of PRs to pack into a single batched GraphQL request. GitHub's documented
# 500k-node cap isn't what we hit in practice; their undocumented "other resource
# limits" threshold is tighter and has no published number. 5 was chosen empirically
# by finding a value that works for a real-world 19-PR stack where update mutations
# (up to 8 sub-mutations per PR) are the tightest bottleneck.
DEFAULT_BATCH_SIZE = 5


class GitHubEndpoint(metaclass=ABCMeta):
    batch_size: int

    @abstractmethod
    async def graphql(self, query: str, **kwargs: Any) -> Any:
        """
        Args:
            query: string GraphQL query to execute
            **kwargs: values for variables in the graphql query

        Returns: parsed JSON response
        """
        pass
