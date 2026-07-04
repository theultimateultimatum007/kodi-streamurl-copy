# -*- coding: utf-8 -*-
"""Script entry point (run from Programs / key mapping)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "resources", "lib"))

from main import run  # noqa: E402

if __name__ == "__main__":
    run()
