"""
Where the application keeps its data (config.py), and the pre-demo smoke test.

- LOGIDOC_DATA_DIR moves the documents, every database and the extraction
  store; LOGIDOC_CHROMA_DIR moves the vector index. Without them nothing
  changes: data/ and chroma_db/ in the project, as before.
- scripts/smoke_test.py starts the real server on temporary folders and walks
  the main workflow without touching data/ or chroma_db/. Here the model
  service is made unreachable, so the walk is deterministic: the upload step
  is a warning and every other step passes.

config.py is read once per process, so each case runs in a fresh Python.

Run from the project root:
    python -m pytest tests/test_config_locations.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOCATIONS = """
import json, config
places = config.ingestion_locations(config.RAW_DATA_DIR)
print(json.dumps({name: str(value) for name, value in {
    "data": config.DATA_DIR, "raw": config.RAW_DATA_DIR, "chroma": config.CHROMA_DIR,
    "reviews": config.REVIEW_DB_PATH, "history": config.HISTORY_DB_PATH, "auth": config.AUTH_DB_PATH,
    "register": places["register"], "extractions": places["extractions"],
    "quarantine": places["quarantine"], "archive": places["archive"],
}.items()}))
"""


def locations(**overrides):

    env = {key: value for key, value in os.environ.items() if not key.startswith("LOGIDOC_")}
    env.update(overrides)
    reply = subprocess.run([sys.executable, "-c", LOCATIONS], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=True)

    return {name: Path(value) for name, value in json.loads(reply.stdout).items()}


def test_without_overrides_everything_stays_in_the_project():

    found = locations()

    assert found["data"] == PROJECT_ROOT / "data" and found["raw"] == PROJECT_ROOT / "data" / "raw"
    assert found["chroma"] == PROJECT_ROOT / "chroma_db"
    assert found["reviews"] == PROJECT_ROOT / "data" / "audit_reviews.sqlite3"
    assert found["extractions"] == PROJECT_ROOT / "data" / "extractions.sqlite3"


def test_the_data_and_index_folders_can_be_moved(tmp_path):

    found = locations(LOGIDOC_DATA_DIR=str(tmp_path / "data"), LOGIDOC_CHROMA_DIR=str(tmp_path / "index"))

    assert found["chroma"] == tmp_path / "index"

    for name in ("raw", "reviews", "history", "auth", "register", "extractions", "quarantine", "archive"):
        assert tmp_path / "data" in found[name].parents, name

    assert not (tmp_path / "data").exists()          # reading the configuration creates nothing


def test_a_database_override_still_wins_over_the_data_folder(tmp_path):

    found = locations(LOGIDOC_DATA_DIR=str(tmp_path / "data"), LOGIDOC_REVIEW_DB=str(tmp_path / "elsewhere.sqlite3"))

    assert found["reviews"] == tmp_path / "elsewhere.sqlite3"
    assert found["history"].parent == tmp_path / "data"


def test_the_smoke_test_walks_the_workflow_on_temporary_folders_without_the_model_service():

    env = {key: value for key, value in os.environ.items() if not key.startswith("LOGIDOC_")}
    env["OLLAMA_HOST"] = "http://127.0.0.1:9"            # nothing listens there: the model service is unusable

    reply = subprocess.run([sys.executable, "scripts/smoke_test.py"], cwd=PROJECT_ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=600)

    assert reply.returncode == 0, reply.stdout + reply.stderr
    assert "RESULT: PASS" in reply.stdout and " 0 failed" in reply.stdout
    assert "WARN  upload 6 sample PDFs" in reply.stdout                     # no model: reported, not hidden
    assert "PASS  audit of the two sample shipments" in reply.stdout
    assert "PASS  assistant (deterministic, cited)" in reply.stdout
    assert "PASS  graceful stop" in reply.stdout
