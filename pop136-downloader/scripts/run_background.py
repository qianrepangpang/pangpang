from __future__ import annotations

import os
import threading
from pathlib import Path

from pop136_core import Pop136Engine


TARGET = Path(r"H:\POP136原图")
PROFILE = Path(r"H:\POP136浏览器登录状态")
PID_FILE = TARGET / "_runner.pid"
RUNNER_LOG = TARGET / "_runner_log.txt"


def append_runner_log(message: str) -> None:
    with RUNNER_LOG.open("a", encoding="utf-8") as output:
        output.write(message + "\n")


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    append_runner_log(f"background app runner started pid={os.getpid()}")
    try:
        engine = Pop136Engine(
            TARGET,
            PROFILE,
            "all",
            append_runner_log,
            threading.Event(),
        )
        engine.run()
    except Exception as error:
        append_runner_log(f"background app runner stopped with error: {error}")
        raise
    finally:
        PID_FILE.unlink(missing_ok=True)
        append_runner_log("background app runner stopped")


if __name__ == "__main__":
    main()
