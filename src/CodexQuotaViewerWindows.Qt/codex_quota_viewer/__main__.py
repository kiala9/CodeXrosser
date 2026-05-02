import sys

from .qt_app import run_app
from .task_worker import main as run_worker


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cqv-worker":
        raise SystemExit(run_worker(sys.argv[2:]))
    raise SystemExit(run_app())
