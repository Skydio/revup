import asyncio
import logging
import sys

from revup.revup import main
from revup.types import RevupConflictException, RevupUsageException


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


if __name__ == "__main__":
    _main()
