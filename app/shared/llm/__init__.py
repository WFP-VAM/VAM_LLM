"""The app's one way to call a model: an LLMClient per drafter profile, traced the same way for every drafter.

Provider adapters and their schema compilers own SDK access; application services use neutral requests.
"""
from .client import describe_llm_config, LLMClient, create_llm_client, default_provider
from .errors import ProviderError, EMPTY, INVALID_JSON, LLMCallError, is_transient
from .profiles import ModelProfile, market_monitor_profile
from .protocol import FilePart, LLMRequest, LLMResponse, LLMResult, RequestMeasurement
from .schema import TRUNCATED_FINISH_REASONS, inspect_json_structure, parse_json_object
from .settings import (
    LLMRuntimeConfig,
    LLMRuntimeConfigurationError,
    LLMRuntimeStatus,
    llm_runtime_config,
    llm_runtime_status,
)
from .tracing import (
    LLMCallDiagnostic,
    LLMObservabilityConfig,
    LLMRunDiagnostics,
    Tracer,
    current_tracer,
    log_llm_run_summary,
    observability_config,
    tracing_run,
)

__all__ = [
    "EMPTY",
    "FilePart",
    "INVALID_JSON",
    "LLMCallDiagnostic",
    "LLMCallError",
    "LLMClient",
    "LLMObservabilityConfig",
    "LLMRequest",
    "LLMResponse",
    "LLMResult",
    "LLMRunDiagnostics",
    "LLMRuntimeConfig",
    "LLMRuntimeConfigurationError",
    "LLMRuntimeStatus",
    "ModelProfile",
    "TRUNCATED_FINISH_REASONS",
    "Tracer",
    "create_llm_client",
    "describe_llm_config",
    "RequestMeasurement",
    "ProviderError",
    "current_tracer",
    "default_provider",
    "inspect_json_structure",
    "is_transient",
    "llm_runtime_config",
    "llm_runtime_status",
    "log_llm_run_summary",
    "market_monitor_profile",
    "observability_config",
    "parse_json_object",
    "tracing_run",
]
