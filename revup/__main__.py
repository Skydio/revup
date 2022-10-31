import asyncio
import logging
import sys

from revup.revup import main
from revup.types import (
    RevupConflictException,
    RevupGithubException,
    RevupRequestException,
    RevupShellException,
    RevupUsageException,
)


def _main() -> None:
    try:
        # Exit code of 1 is reserved for exception-based exits
        sys.exit(asyncio.run(main()))
    except RevupUsageException as e:
        logging.error(str(e))
        sys.exit(2)
    except RevupConflictException as e:
        logging.error(str(e))
        sys.exit(3)
    except RevupShellException as e:
        logging.error(str(e))
        sys.exit(4)
    except RevupGithubException as e:
        for error in e.error_json:
            error_type = error["type"] if "type" in error else "Unknown Error"
            logging.error("{}: {}".format(error_type, error["message"]))

        logging.warning("{} operations failed!".format(len(e.error_json)))
        sys.exit(5)
    except RevupRequestException as e:
        logging.error(f"Request failed with response status {e.status}")
        logging.error(f"Response: {e.response}")
        sys.exit(6)


if __name__ == "__main__":
    _main()
