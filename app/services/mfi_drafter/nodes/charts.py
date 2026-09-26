"""Node charts: the charts and maps, rendered in a worker process while the drafts are written."""
from __future__ import annotations


def render_figures(base):
    from ..render_worker import RenderWorker, figure_jobs
    worker = RenderWorker()
    images, metadata = {}, {}
    try:
        for job in figure_jobs(base):
            result = worker.run(job)
            images[job["figure_id"]], metadata[job["figure_id"]] = result["image"], result["metadata"]
    finally:
        worker.close()
    return {"visualizations": images, "figure_metadata": metadata}
