"""MFI Drafter service package."""

from importlib import import_module

__all__ = [
    "router",
    "run_mfi_report_generation",
    "build_graph",
    "create_initial_state",
    "DIMENSION_DESCRIPTIONS",
    "MFI_DIMENSIONS",
    "RISK_COLORS",
    "get_risk_level",
    "Document",
    "MFIMarketData",
    "MFIDimensionScore",
    "DimensionFinding",
    "SkepticFlag",
    "SurveyMetadata",
    "GenerateMFIReportOutput",
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
    "MFIGenerationDiagnostics",
    "MFIContextRetrieverStatus",
    "MFIContextStatus",
    "build_assessment_profile",
]


def __getattr__(name: str):
    if name == "run_mfi_report_generation":
        return getattr(import_module(".light_service", __name__), name)
    if name == "router":
        from .router import router

        return router
    if name == "DIMENSION_DESCRIPTIONS":
        return getattr(import_module(".methodology", __name__), name)
    if name in {
        "run_mfi_report_generation",
        "build_graph",
        "create_initial_state",
    }:
        return getattr(import_module(".graph", __name__), name)
    if name == "build_assessment_profile":
        return getattr(import_module(".analysis", __name__), name)
    if name in {
        "MFI_DIMENSIONS",
        "RISK_COLORS",
        "get_risk_level",
        "Document",
        "MFIMarketData",
        "MFIDimensionScore",
        "DimensionFinding",
        "SkepticFlag",
        "SurveyMetadata",
        "GenerateMFIReportOutput",
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
        "MFIGenerationDiagnostics",
        "MFIContextRetrieverStatus",
        "MFIContextStatus",
    }:
        return getattr(import_module(".schemas", __name__), name)
    raise AttributeError(name)
