# PYTHON_ARGCOMPLETE_OK
import asyncio
import logging
import sys

from revup.core_types import (
    RevupConflictException,
    RevupForgeException,
    RevupRequestException,
    RevupShellException,
    RevupUsageException,
)
from revup.revup import build_parser, main


def _main() -> None:
    try:
        # Exit code of 1 is reserved for exception-based exits.
        # Note: on Windows, asyncio.run() doesn't work properly due to this issue:
        # https://stackoverflow.com/questions/63860576/asyncio-event-loop-is-closed-when-using-asyncio-run
        # Since revup makes use of subprocess, we can't use WindowsSelectorEventLoopPolicy.
        # Instead, we can manually create the event loop and prevent the RuntimeError on shutdown.
        revup_parser, all_parsers = build_parser()
        loop = asyncio.new_event_loop()
        task = loop.create_task(main(revup_parser, all_parsers))
        try:
            sys.exit(loop.run_until_complete(task))
        except KeyboardInterrupt:
            task.cancel()
            try:
                loop.run_until_complete(task)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            # Exit code of 130 is the shell convention for death by sigint.
            logging.error("Interrupted")
            sys.exit(130)
        finally:
            loop.close()
    except RevupUsageException as e:
        logging.error(str(e))
        sys.exit(2)
    except RevupConflictException as e:
        logging.error(e.message)
        sys.exit(3)
    except RevupShellException as e:
        logging.error(str(e))
        sys.exit(4)
    except RevupForgeException as e:
        logging.error(f"Forge Exception: {e.type}: {e.message}")
        sys.exit(5)
    except RevupRequestException as e:
        logging.error(f"Request failed with response status {e.status}")
        logging.error(f"Response: {e.response}")
        sys.exit(6)


if __name__ == "__main__":
    _main()
