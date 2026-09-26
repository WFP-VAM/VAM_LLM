"""HTTP entry point: `uvicorn main:app`. The application itself is `app.api`, which the Streamlit pages also use."""
from app.shared.config import configure_logging, load_environment

load_environment()
configure_logging()

from app.api import app  # noqa: E402

__all__ = ["app"]
