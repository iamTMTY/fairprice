"""FairPrice. Settings come from the environment, or from a .env file next to pyproject.toml (see .env.example).

Loaded here, before any module reads its settings; variables already set in the environment win.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
