"""Market Monitor service package."""

from importlib import import_module

__all__ = [
    "router",
    "run_report_generation",
    "build_graph",
    "create_initial_state",
    "AVAILABLE_MODULES",
    "CURRENCY_SYMBOLS",
    "ModuleOutput",
    "Document",
    "Event",
    "TrendAnalysis",
    "SkepticFlag",
    "DataStatistics",
    "GenerateReportInput",
    "GenerateReportOutput",
    "ReportStatusOutput",
]


def __getattr__(name: str):
    if name == "router":
        from .router import router

        return router
    if name == "run_report_generation":
        return getattr(import_module(".service", __name__), name)
    if name == "build_graph":
        return getattr(import_module(".graph", __name__), name)
    if name == "create_initial_state":
        return getattr(import_module(".state", __name__), name)
    if name in {"AVAILABLE_MODULES", "CURRENCY_SYMBOLS"}:
        return getattr(import_module(".modules", __name__), name)
    if name in {
        "ModuleOutput",
        "Document",
        "Event",
        "TrendAnalysis",
        "SkepticFlag",
        "DataStatistics",
        "GenerateReportInput",
        "GenerateReportOutput",
        "ReportStatusOutput",
    }:
        return getattr(import_module(".schemas", __name__), name)
    raise AttributeError(name)
