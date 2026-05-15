from pathlib import Path
import os

PRINTER_HOST = os.getenv("AD5X_HOST", "192.168.1.172")
PRINTER_PORT = int(os.getenv("AD5X_PORT", "8899"))
UPDATE_INTERVAL = float(os.getenv("AD5X_UPDATE_INTERVAL", "2.0"))

SERVER_HOST = os.getenv("AD5X_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("AD5X_SERVER_PORT", "8000"))

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
WORKSPACE_DIR = PROJECT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

TEMP_LOG_CSV = DATA_DIR / "temperature_log.csv"
TEMP_LOG_JSON = DATA_DIR / "temperature_latest.json"

MESH_SOURCE_FILE = Path(
	os.getenv("AD5X_MESH_SOURCE", str(WORKSPACE_DIR / "Settings export" / "printer.txt"))
)
