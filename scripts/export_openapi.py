"""Generate the job API contract from the server's Pydantic models."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot import jobs_openapi  # noqa: E402

if __name__ == "__main__":
    output = ROOT / "frontend" / "openapi.json"
    output.write_text(json.dumps(jobs_openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
