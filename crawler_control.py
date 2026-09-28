"""Cooperative finish: stop at request boundaries, then let finalizers save."""
import signal
import os
from pathlib import Path

_finishing = False
_finish_file = None


class FinishRequested(BaseException):
    pass


def install():
    global _finishing, _finish_file
    _finishing = False
    path = os.environ.get("GOODS_FINISH_FILE")
    _finish_file = Path(path) if path else None
    def finish(signum, frame):
        global _finishing
        _finishing = True
    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)


def checkpoint():
    if finishing():
        raise FinishRequested()


def finishing():
    return _finishing or (_finish_file is not None and _finish_file.exists())
