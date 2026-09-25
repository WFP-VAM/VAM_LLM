"""Process set-up shared by the entry points: environment, logging and the Google Cloud project."""
import logging
import os

from dotenv import load_dotenv

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def load_environment() -> None:
    """Load a local .env file, if any, without overriding variables that are already set."""
    load_dotenv()


def configure_logging() -> None:
    """Send application logs at INFO and above to stderr, in the same format in every process.

    Streamlit configures only its own loggers, so the Streamlit process needs this as much as FastAPI.
    """
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger().setLevel(logging.INFO)


def resolve_project() -> str:
    """The Google Cloud project for Vertex AI: VERTEX_PROJECT_ID, the usual project variables, then ADC."""
    project_id = (os.getenv("VERTEX_PROJECT_ID") or "").strip()
    if project_id:
        return project_id

    for key in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCP_PROJECT"):
        candidate = (os.getenv(key) or "").strip()
        if candidate:
            return candidate

    try:
        import google.auth  # type: ignore

        _, inferred_project_id = google.auth.default()
        if inferred_project_id:
            return str(inferred_project_id).strip()
    except Exception:
        pass

    raise RuntimeError(
        "Missing Vertex project id. Set VERTEX_PROJECT_ID (recommended) or "
        "ensure GOOGLE_CLOUD_PROJECT is set."
    )
