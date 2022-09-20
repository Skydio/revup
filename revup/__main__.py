import asyncio
import logging
import sys

from revup.revup import main
from revup.types import (
    RevupConflictException,
    RevupGithubException,
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
            logging.error("{}: {}".format(error["type"], error["message"]))

        logging.warning("{} operations failed!".format(len(e.error_json)))
        sys.exit(5)


if __name__ == "__main__":
    _main()
