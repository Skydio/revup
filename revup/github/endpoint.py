import asyncio
import datetime
import json
import logging
import time
from typing import Any, Optional, Tuple, Union

from aiohttp import ClientSession, ContentTypeError

from revup.core_types import RevupForgeException, RevupRequestException
from revup.github.graphql import GraphqlResponse

# HTTP statuses worth retrying: gateway/timeout (5xx) and secondary-rate-limit (403).
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
SECONDARY_LIMIT_STATUS = 403
# Cap how long we'll auto-sleep waiting for a rate limit to reset. A longer wait
# (primary budget exhausted) is surfaced to the user instead of hanging silently.
MAX_BACKOFF_SECONDS = 60.0


def _backoff_delay(headers: Any, attempt: int, base_delay: float) -> float:
    """Seconds to wait before retrying, driven by GitHub's rate-limit headers.

    Precedence: an explicit Retry-After, then waiting out an exhausted budget
    (remaining == 0) until its reset, else plain exponential backoff.
    """
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    remaining = headers.get("x-ratelimit-remaining")
    reset = headers.get("x-ratelimit-reset")
    if remaining == "0" and reset is not None:
        try:
            return max(0.0, int(reset) - time.time())
        except ValueError:
            pass
    return base_delay * (2**attempt)


class GitHubEndpoint:
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
        # Rate-limit budget from the most recent response, for end-of-run reporting.
        self.last_ratelimit_remaining: Optional[str] = None
        self.last_ratelimit_reset: Optional[str] = None

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def _post(self, query: str, kwargs: Any) -> Tuple[int, Any, Any]:
        """POST a GraphQL request. Returns (status, headers, body).

        No policy: never raises for HTTP status or GraphQL errors. body is the
        parsed JSON, or None if the response wasn't JSON.
        """
        if self.session is None:
            self.session = ClientSession()

        headers = {}
        if self.oauth_token:
            headers["Authorization"] = "bearer {}".format(self.oauth_token)

        logging.debug("# POST {}".format(self.graphql_endpoint))
        logging.debug("Request GraphQL query:\n{}".format(query))
        logging.debug("Request GraphQL variables:\n{}".format(json.dumps(kwargs, indent=1)))

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
            reset = resp.headers.get("x-ratelimit-reset")
            reset_str = (
                datetime.datetime.fromtimestamp(int(reset)).isoformat()
                if reset is not None
                else "Unknown"
            )
            self.last_ratelimit_remaining = resp.headers.get("x-ratelimit-remaining")
            self.last_ratelimit_reset = reset_str
            logging.debug(
                "Ratelimit: {} remaining, resets at {}".format(
                    self.last_ratelimit_remaining, reset_str
                )
            )
            try:
                body = await resp.json()
                logging.debug("Response JSON:\n{}".format(json.dumps(body, indent=1)))
            except (ValueError, ContentTypeError):
                logging.warning("Response body:\n{}".format(await resp.text()))
                body = None
            return resp.status, resp.headers, body

    async def graphql(
        self,
        query: str,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
        **kwargs: Any,
    ) -> GraphqlResponse:
        """Execute a GraphQL request, retrying transient failures with backoff.

        Returns a GraphqlResponse carrying partial data and per-field errors — a
        200 with field errors is NOT raised, so callers can salvage what resolved
        and re-transact the rest. Raises RevupForgeException for a request error
        (200 with no data) and RevupRequestException for a non-retryable HTTP error.
        """
        for attempt in range(max_retries):
            status, headers, body = await self._post(query, kwargs)

            if status == 200:
                if body is None:
                    raise RevupRequestException(status, {})
                # A request error (bad query, unresolvable variable) returns no data
                # and can't be salvaged or retried; fail loudly.
                if body.get("data") is None and "errors" in body:
                    raise RevupForgeException(body["errors"])
                return GraphqlResponse.parse(body)

            retryable = status in TRANSIENT_STATUSES or status == SECONDARY_LIMIT_STATUS
            if not retryable or attempt >= max_retries - 1:
                raise RevupRequestException(status, body if body is not None else {})

            delay = min(_backoff_delay(headers, attempt, base_delay), MAX_BACKOFF_SECONDS)
            logging.warning(
                "GitHub returned {}, retrying in {}s (attempt {}/{})".format(
                    status, delay, attempt + 1, max_retries
                )
            )
            await asyncio.sleep(delay)

        raise RevupRequestException(status, body if body is not None else {})
