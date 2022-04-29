from abc import ABCMeta, abstractmethod
from typing import Any


class GitHubEndpoint(metaclass=ABCMeta):
    @abstractmethod
    async def graphql(self, query: str, require_success: bool = True, **kwargs: Any) -> Any:
        """
        Args:
            query: string GraphQL query to execute
            **kwargs: values for variables in the graphql query

        Returns: parsed JSON response
        """
        pass
