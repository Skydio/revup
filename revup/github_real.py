import asyncio
import datetime
import json
import logging
import time
from typing import Any, Optional, Tuple, Union

from aiohttp import ClientSession, ContentTypeError

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

    async def _retry_with_backoff(
        self, attempt: int, max_retries: int, base_delay: float, message: str
    ) -> bool:
        """Sleep with exponential backoff if retries remain. Returns True to retry."""
        if attempt >= max_retries - 1:
            return False
        delay = base_delay * (2**attempt)
        logging.warning(
            "{}, retrying in {}s (attempt {}/{})".format(message, delay, attempt + 1, max_retries)
        )
        await asyncio.sleep(delay)
        return True

    async def graphql(self, query: str, **kwargs: Any) -> Any:
        if self.session is None:
            self.session = ClientSession()

        # Retry config: 3 attempts with exponential backoff (1s, 2s, 4s)
        max_retries = 3
        base_delay = 1.0
        transient_statuses = {500, 502, 503, 504}
        retryable_graphql_errors = {"RESOURCE_LIMITS_EXCEEDED"}

        headers = {}
        if self.oauth_token:
            headers["Authorization"] = "bearer {}".format(self.oauth_token)

        logging.debug("# POST {}".format(self.graphql_endpoint))
        logging.debug("Request GraphQL query:\n{}".format(query))
        logging.debug("Request GraphQL variables:\n{}".format(json.dumps(kwargs, indent=1)))

        for attempt in range(max_retries):
            start_time = time.time()
            async with self.session.post(
                self.graphql_endpoint,
                json={"query": query, "variables": kwargs},
                headers=headers,
                proxy=self.proxy,
            ) as resp:
                logging.debug(
                    "Response status: {} took {}".format(resp.status, time.time() - start_time)
                )
                ratelimit_reset = resp.headers.get("x-ratelimit-reset")
                if ratelimit_reset is not None:
                    reset_timestamp = datetime.datetime.fromtimestamp(
                        int(ratelimit_reset)
                    ).isoformat()
                else:
                    reset_timestamp = "Unknown"
                logging.debug(
                    "Ratelimit: {} remaining, resets at {}".format(
                        resp.headers.get("x-ratelimit-remaining"),
                        reset_timestamp,
                    )
                )

                if resp.status in transient_statuses:
                    msg = "GitHub returned {}".format(resp.status)
                    if await self._retry_with_backoff(attempt, max_retries, base_delay, msg):
                        continue
                    logging.warning("Response body:\n{}".format(await resp.text()))
                    raise RevupRequestException(resp.status, {})

                try:
                    r = await resp.json()
                except (ValueError, ContentTypeError):
                    logging.warning("Response body:\n{}".format(await resp.text()))
                    raise
                else:
                    pretty_json = json.dumps(r, indent=1)
                    logging.debug("Response JSON:\n{}".format(pretty_json))

                if "errors" in r:
                    error_types = {err.get("type", "Unknown") for err in r["errors"]}
                    # TODO: For RESOURCE_LIMITS_EXCEEDED, use x-ratelimit-reset header
                    # instead of exponential backoff - either wait until reset time or
                    # fail immediately if the wait would be too long.
                    if error_types & retryable_graphql_errors:
                        matched = ", ".join(error_types & retryable_graphql_errors)
                        msg = "GitHub GraphQL error ({})".format(matched)
                        if await self._retry_with_backoff(attempt, max_retries, base_delay, msg):
                            continue
                    raise RevupGithubException(r["errors"])

                if resp.status != 200:
                    raise RevupRequestException(resp.status, r)

                return r
