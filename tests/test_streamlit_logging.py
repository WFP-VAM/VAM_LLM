"""The Streamlit process emits application logs; Streamlit itself configures only its own loggers."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pages_emit_application_info_logs():
    code = (
        "import logging, streamlit_shared\n"
        "logging.getLogger('app.services.example').info('application log line')\n"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300)

    assert completed.returncode == 0, completed.stderr
    assert "app.services.example - INFO - application log line" in completed.stderr
