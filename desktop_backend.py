"""Frozen local API entry point for the BioAgri desktop application."""

from multiprocessing import freeze_support
import os
from pathlib import Path
import sys
import traceback

import uvicorn

from api_v020 import app


def main():
    freeze_support()
    null_stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = null_stream
    if sys.stderr is None:
        sys.stderr = null_stream
    try:
        uvicorn.run(app, host="127.0.0.1", port=47831, log_config=None, access_log=False)
    except BaseException:
        log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "BioAgri Process Simulator"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "backend-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
