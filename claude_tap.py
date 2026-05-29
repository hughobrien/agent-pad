"""claude-tap: Steam Controller -> tmux keystroke bridge."""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("claude-tap")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("claude-tap starting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
