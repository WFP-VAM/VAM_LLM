"""Spawned, serial MFI figure worker. No pyplot or shared global figures."""
from __future__ import annotations
import base64
import io
import json
import queue
import re
import subprocess
import sys
import threading
from math import pi
from pathlib import Path

from .reliable_contracts import fingerprint

BLUE = "#0072BC"


def figure_jobs(state):
    profile = state["assessment_profile"]
    dimensions = profile.get("dimensions", [])
    markets = [{key: m.get(key) for key in ("market_name", "region", "admin1", "overall_mfi", "dimension_scores", "latitude", "longitude")}
               for m in state.get("markets_data", [])]
    jobs = []
    def add(figure_id, kind, data):
        jobs.append({"run_id": state.get("run_id") or "diagnostic-" + fingerprint(profile)[:16], "figure_id": figure_id, "kind": kind,
                     "analytical_fingerprint": fingerprint(data), "data": data})
    add("mfi_radar", "radar", {"names": [d["dimension"] for d in dimensions], "values": [d["statistics"]["mean"] for d in dimensions]})
    ordered = sorted(markets, key=lambda m: (m["overall_mfi"], m["market_name"].casefold()))
    add("market_score_ranking", "ranking", {"markets": ordered, "selected": profile.get("priority_market_names", [])})
    add("overview_table", "matrix", {"markets": markets, "dimensions": [d["dimension"] for d in dimensions]})
    for dim in dimensions:
        name = dim["dimension"]
        slug = re.sub(r"[^a-z0-9_]+", "_", name.lower().replace(" ", "_").replace("&", "and")).strip("_")
        add(f"dim_{slug}_bars", "dimension", {"name": name, "markets": markets,
            "ordered": dim.get("localized_patterns", {}).get("ordered_markets", []),
            "coverage": dim["statistics"].get("coverage"),
            "regions": {r["region"]: r["statistics"]["mean"] for r in dim.get("regional_summaries", [])}})
        if name not in profile.get("priority_dimension_names", []):
            continue
        if name != "Food Quality":
            rows = sorted([(m["display_name"], m["mean_normalized_value"]) for m in dim.get("subsections", []) if m.get("mean_normalized_value") is not None], key=lambda pair: (pair[1], pair[0]))
            if rows:
                add(f"priority_{slug}_subsections", "bars", {"rows": rows, "title": f"{name}: official subsection evidence", "unit": "Normalized subsection score (0-10)", "limit": 10, "color": BLUE})
        for kind in ("drivers", "items"):
            rows = [m for m in dim.get("drivers", []) if m.get("unfavorable_rate") is not None and (
                m.get("item_relevant") if kind == "items" else m.get("item_name") is None and m.get("weakness_rank") is not None)]
            rows.sort(key=lambda m: (-m["unfavorable_rate"], m["metric_id"]))
            if kind == "drivers":
                rows = rows[:8]
            if rows:
                add(f"priority_{slug}_{kind}", "bars", {"rows": [(m["display_name"], m["unfavorable_rate"] * 100) for m in rows],
                    "title": f"{name}: {'ranked explanatory' if kind == 'drivers' else 'relevant item'} evidence",
                    "unit": "Unfavorable rate (%)", "limit": 100, "color": "#F68B1F" if kind == "drivers" else "#8A2BE2"})
    located = [m for m in markets if m.get("latitude") is not None and m.get("longitude") is not None]
    from .map_basemap import valid_location
    if any(valid_location(m) for m in located):
        from .map_rendering import map_data
        add("geographic_map", "map", map_data(state))
    return jobs


def render(job):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import numpy as np
    data, kind = job["data"], job["kind"]
    names, values = [], []
    height = 8
    if kind in {"ranking", "dimension", "matrix"}:
        height = max(6, len(data["markets"]) * (0.35 if kind == "matrix" else 0.3))
    elif kind == "bars":
        height = max(4, len(data["rows"]) * .5)
    fig = Figure(figsize=(12 if kind == "matrix" else 10, height))
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111, projection="polar" if kind == "radar" else None) if kind != "map" else None
    metadata = None
    try:
        if kind == "radar":
            names, values = data["names"], data["values"]
            angles = [n / len(names) * 2 * pi for n in range(len(names))]
            ax.set_theta_offset(pi / 2)
            ax.set_theta_direction(-1)
            ax.set_xticks(angles, names, fontsize=8)
            ax.set_ylim(0, 10)
            ax.plot(angles + angles[:1], values + values[:1], color=BLUE, linewidth=2)
            ax.fill(angles + angles[:1], values + values[:1], color=BLUE, alpha=.25)
            ax.set_title("Average MFI dimension profile across assessed markets", pad=24)
        elif kind == "matrix":
            names = [m["market_name"] for m in data["markets"]]
            values = [[m["dimension_scores"].get(d) for d in data["dimensions"]] for m in data["markets"]]
            im = ax.imshow(np.array(values, dtype=float), cmap="Blues", aspect="auto", vmin=0, vmax=10)
            ax.set_xticks(range(len(data["dimensions"])), data["dimensions"], rotation=45, ha="right", fontsize=9)
            ax.set_yticks(range(len(names)), names, fontsize=8)
            for i, row in enumerate(values):
                for j, value in enumerate(row):
                    if value is not None:
                        ax.text(j, i, f"{value:.2f}", ha="center", va="center", fontsize=7, color="white" if value > 7 else "black")
            fig.colorbar(im, ax=ax, shrink=.5).set_label("MFI Score (0-10)")
            ax.set_title("Assessed-market MFI profile by dimension")
        elif kind == "map":
            from .map_rendering import draw_map
            metadata = draw_map(fig, data)
        else:
            if kind == "bars":
                names, values = map(list, zip(*data["rows"]))
                ax.barh(range(len(names)), values, color=data["color"])
                ax.set(xlim=(0, data["limit"]), xlabel=data["unit"], title=data["title"])
                if data["limit"] == 100:
                    ax.invert_yaxis()
            elif kind == "ranking":
                names = [m["market_name"] for m in data["markets"]]
                values = [m["overall_mfi"] for m in data["markets"]]
                ax.scatter(values, range(len(names)), c=["#F68B1F" if n in data["selected"] else BLUE for n in names], edgecolors="white")
                ax.set(xlim=(0, 10), xlabel="Stored MFI score (0-10)", title="Ordered assessed-market MFI scores")
            else:
                names = [m["name"] for m in data["ordered"]]
                values = [m["value"] for m in data["ordered"]]
                ax.barh(range(len(names)), values, color=BLUE, alpha=.75, edgecolor="white", linewidth=.5)
                regions = {m["market_name"]: m.get("region") or m.get("admin1") for m in data["markets"]}
                added = False
                for index, name in enumerate(names):
                    mean = data["regions"].get(regions[name])
                    if mean is not None:
                        ax.scatter(mean, index, marker="D", s=28, color="#F68B1F", edgecolor="black", linewidth=.3, label=None if added else "Regional mean", zorder=4)
                        added = True
                if added:
                    ax.legend(loc="lower right", fontsize=8)
                from .visualization import format_market_coverage, validate_dimension_chart_coverage
                coverage = validate_dimension_chart_coverage(data["coverage"], plotted_market_count=len(names), dimension=data["name"])
                ax.set(xlim=(0, 10), xlabel="Stored dimension score (0-10)", title=f"{data['name']} by assessed market\nCoverage: {format_market_coverage(coverage)}")
                for index, value in enumerate(values):
                    ax.text(value + .1, index, f"{value:.2f}", va="center", fontsize=7)
            ax.set_yticks(range(len(names)), names, fontsize=8)
            ax.grid(axis="x", alpha=.2)
        if kind != "map":
            fig.tight_layout()
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=150, bbox_inches=None if kind == "map" else "tight")
        return {key: job[key] for key in ("run_id", "figure_id", "analytical_fingerprint")} | {
            "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "metadata": metadata if metadata is not None else {"labels": names, "values": values, "title": ax.get_title()}}
    finally:
        fig.clear()


def _serve():
    """Dedicated module entry point, independent of Streamlit's __main__."""
    for line in sys.stdin:
        try:
            job = json.loads(line)
            response = {"result": render(job)}
        except Exception as exc:
            response = {"error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")
        sys.stdout.flush()


class RenderWorker:
    def __init__(self, timeout=120):
        self.timeout, self.process = timeout, None
        self.responses = None
        self.reader = None
        self.error_reader = None
        self.stderr_tail = ""

    def _start(self):
        # multiprocessing.spawn reimports the caller's __main__, which Streamlit
        # replaces with the page script. Spawn this module explicitly instead.
        self.responses = queue.Queue()
        self.stderr_tail = ""
        self.process = subprocess.Popen(
            [sys.executable, "-m", "app.services.mfi_drafter.render_worker"],
            cwd=Path(__file__).resolve().parents[3],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process, responses = self.process, self.responses

        def read_responses():
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except Exception as exc:
                responses.put({"error": f"Invalid figure worker response: {exc}"})
            finally:
                responses.put({"worker_exited": True})

        def read_errors():
            for line in process.stderr:
                self.stderr_tail = (self.stderr_tail + line)[-4096:]

        self.reader = threading.Thread(target=read_responses, daemon=True)
        self.error_reader = threading.Thread(target=read_errors, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for reader in (self.reader, self.error_reader):
                if reader:
                    reader.join(timeout=3)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
        self.process = self.responses = None

    def run(self, job):
        for attempt in range(2):
            try:
                if self.process is None:
                    self._start()
                self.process.stdin.write(json.dumps(job, ensure_ascii=True, allow_nan=False) + "\n")
                self.process.stdin.flush()
                try:
                    response = self.responses.get(timeout=self.timeout)
                except queue.Empty as exc:
                    raise TimeoutError(f"MFI figure {job['figure_id']} exceeded {self.timeout} seconds") from exc
                if response.get("worker_exited"):
                    self.error_reader.join(timeout=1)
                    raise RuntimeError(f"MFI figure worker exited: {self.stderr_tail.strip()}")
                if "error" in response:
                    raise RuntimeError(response["error"])
                result = response["result"]
                if any(result.get(key) != job[key] for key in ("run_id", "figure_id", "analytical_fingerprint")):
                    raise RuntimeError("MFI figure response identity mismatch")
                return result
            except Exception:
                self.close()
                if attempt:
                    raise


if __name__ == "__main__":
    _serve()
