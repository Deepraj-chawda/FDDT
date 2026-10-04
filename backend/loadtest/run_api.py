"""Run the real API with the external services stubbed (see loadtest/stubs.py).

    python -m loadtest.run_api [port]
"""
import os
import sys

from loadtest import stubs

stubs.install()

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("LOADTEST_API_HOST", "127.0.0.1"), port=int(sys.argv[1]) if len(sys.argv) > 1 else 8100, log_level="warning")
