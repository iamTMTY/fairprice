"""FairPrice. Settings come from the environment, or from a .env file next to pyproject.toml (see .env.example).

Loaded here, before any module reads its settings; variables already set in the environment win.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

if not os.environ.get("FAIRPRICE_NO_DOTENV"):  # the tests set it, so they never see your settings or keys
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
