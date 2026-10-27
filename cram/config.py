import os
from pathlib import Path
from dotenv import load_dotenv

# Load local .env if present
load_dotenv(override=False)

BASE_DIR = Path(__file__).resolve().parent.parent

class Config:
    def __init__(self):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model_name = os.getenv("CRAM_MODEL", "gemini-3.5-flash-lite").strip()
        self.db_path = Path(os.getenv("CRAM_DB_PATH", str(BASE_DIR / "cram_memory.db")))
        self.max_retries = int(os.getenv("CRAM_MAX_RETRIES", "3"))
        self.request_timeout = float(os.getenv("CRAM_TIMEOUT", "30.0"))

    def validate_api_key(self) -> None:
        if not self.gemini_api_key:
            raise ValueError(
                "GEMINI_API_KEY environment variable is not set. "
                "Please configure GEMINI_API_KEY in your environment or in a .env file."
            )

config = Config()
