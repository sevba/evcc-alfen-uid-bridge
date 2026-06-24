import logging
import signal
import sys

from bridge.config import Config
from bridge.orchestrator import Orchestrator


def main():
    config = Config.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config.log_startup()

    orchestrator = Orchestrator(config)

    def _shutdown(sig, frame):
        orchestrator.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    orchestrator.run()
    log.info("bridge: exited cleanly")


log = logging.getLogger("bridge")

if __name__ == "__main__":
    main()
