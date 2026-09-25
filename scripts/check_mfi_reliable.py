"""Run the MFI regression gate (the MFI tests plus the shared ones they rely on) on Windows or Linux."""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
tests = sorted(str(path.relative_to(root)) for path in (root / "tests").glob("test_mfi*.py"))
tests += ["tests/test_llm_observability.py", "tests/test_llm_observability_integration.py",
          "tests/test_live_async_visibility.py", "tests/test_streamlit_shared_timeouts.py",
          "tests/test_llm_runtime_config.py", "tests/test_streamlit_report_delivery.py"]
(root / ".tmp").mkdir(exist_ok=True)
base = tempfile.mkdtemp(prefix="mfi-check-", dir=root / ".tmp")
print(f"Running MFI checks with {sys.executable}; Python {sys.version}", flush=True)
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider",
                                 f"--basetemp={base}", "--tb=short", *sys.argv[1:]], cwd=root))
