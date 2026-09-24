"""MFI Drafter service package."""

from importlib import import_module

_SCHEMA_EXPORTS = (
    "MFI_DIMENSIONS",
    "get_risk_level",
    "LightMFIReportOutput",
    "MFIReportStatusOutput",
    "MFIMetric",
    "MFIMetricSummary",
    "MFIMethodologyWarning",
    "MFIExcludedMarketRecord",
    "MFIAnalysisConfig",
    "MFICoverageSummary",
    "MFIStatisticalSummary",
    "MFIAnalyzedMetric",
    "MFILocalizedPatterns",
    "MFIDimensionProfile",
    "MFIMarketProfile",
    "MFIMetricLedgerEntry",
    "MFIDeterministicTables",
    "MFIAssessmentProfile",
    "MFIReleaseControl",
    "MFIContextRetrieverStatus",
    "MFIContextStatus",
)

__all__ = [
    "router",
    "run_mfi_report_generation",
    "build_graph",
    "DIMENSION_DESCRIPTIONS",
    "build_assessment_profile",
    *_SCHEMA_EXPORTS,
]


def __getattr__(name: str):
    if name == "run_mfi_report_generation":
        return getattr(import_module(".light_service", __name__), name)
    if name == "router":
        from .router import router

        return router
    if name == "build_graph":
        return getattr(import_module(".light_graph", __name__), name)
    if name == "DIMENSION_DESCRIPTIONS":
        return getattr(import_module(".methodology", __name__), name)
    if name == "build_assessment_profile":
        return getattr(import_module(".analysis", __name__), name)
    if name in _SCHEMA_EXPORTS:
        return getattr(import_module(".schemas", __name__), name)
    raise AttributeError(name)
