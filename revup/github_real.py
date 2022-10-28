import json
import logging
import time
from typing import Any, Optional, Tuple, Union

from aiohttp import ClientSession

from revup import github
from revup.types import RevupGithubException, RevupRequestException


class RealGitHubEndpoint(github.GitHubEndpoint):
    """
    A class representing a GitHub endpoint we can send queries to.
    It supports both GraphQL and REST interfaces.
    """

    # Url of the configured github site.
    github_url: str

    # The URL of the GraphQL endpoint to connect to
    graphql_endpoint: str

    # The string OAuth token to authenticate to the GraphQL server with
    oauth_token: str

    # The URL of a proxy to use for these connections
    proxy: Optional[str]

    # The certificate bundle to be used to verify the connection.
    # Passed to http as 'verify'.
    verify: Optional[str]

    # Client side certificate to use when connecitng.
    # Passed to http as 'cert'.
    cert: Optional[Union[str, Tuple[str, str]]]

    session: Optional[ClientSession] = None

    def __init__(
        self,
        oauth_token: str,
        github_url: str,
        proxy: Optional[str] = None,
    ):
        self.github_url = github_url
        self.oauth_token = oauth_token
        self.proxy = proxy
        self.graphql_endpoint = f"https://api.{github_url}/graphql"

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def graphql(self, query: str, **kwargs: Any) -> Any:
        if self.session is None:
            self.session = ClientSession()

        start_time = time.time()
        headers = {}
        if self.oauth_token:
            headers["Authorization"] = "bearer {}".format(self.oauth_token)

        logging.debug("# POST {}".format(self.graphql_endpoint))
        logging.debug("Request GraphQL query:\n{}".format(query))
        logging.debug("Request GraphQL variables:\n{}".format(json.dumps(kwargs, indent=1)))

        async with self.session.post(
            self.graphql_endpoint,
            json={"query": query, "variables": kwargs},
            headers=headers,
            proxy=self.proxy,
        ) as resp:
            logging.debug(
                "Response status: {} took {}".format(resp.status, time.time() - start_time)
            )
            try:
                r = await resp.json()
            except ValueError:
                logging.debug("Response body:\n{}".format(await resp.text()))
                raise
            else:
                pretty_json = json.dumps(r, indent=1)
                logging.debug("Response JSON:\n{}".format(pretty_json))

            if "errors" in r:
                raise RevupGithubException(r["errors"])

            if resp.status != 200:
                raise RevupRequestException(resp.status, r)

        return r
