"""Node module_orchestrator: runs the enabled optional modules and drafts their sections."""
from __future__ import annotations

import logging
from typing import List

from app.shared.llm import LLMCallError

from ..i18n import t
from ..state import MarketReportState, _state_language
from ..runtime import llm_client
from ..qa import QA_MODULE_SECTIONS
from ..modules import AVAILABLE_MODULES

logger = logging.getLogger(__name__)


def node_module_orchestrator(state: MarketReportState) -> dict:
    """Nodo: Esegue i moduli opzionali."""
    logger.info("[ModuleOrchestrator] Running optional modules")
    
    enabled_modules = state.get("enabled_modules", [])
    llm = llm_client(state)
    trace = llm.tracer
    language = _state_language(state)
    targets = set(state.get("correction_targets") or [])
    correction_mode = bool(targets)

    if not correction_mode:
        operation_ids = {
            "exchange_rate": "market_monitor.exchange_rate_module.v1",
            "fuel_energy": "market_monitor.fuel_energy_module.v1",
            "livestock_animal_products": "market_monitor.livestock_module.v1",
            "labour_market": "market_monitor.labour_module.v1",
        }
        for module_id, operation in operation_ids.items():
            if module_id not in enabled_modules:
                trace.record_skip(
                    node="module_orchestrator",
                    operation=operation,
                    reason="optional_module_disabled",
                    artifact_type="module",
                    artifact_id=module_id,
                )

    if correction_mode and "GLOBAL" not in targets:
        targeted_modules = {
            module_id
            for section, module_id in QA_MODULE_SECTIONS.items()
            if section in targets
        }
        enabled_modules = [module_id for module_id in enabled_modules if module_id in targeted_modules]
    
    if not enabled_modules:
        return {
            "current_node": "module_orchestrator",
            "llm_diagnostics": trace.snapshot(),
        }
    
    module_sections = dict(state.get("module_sections") or {})
    updates = {}
    llm_calls = 0
    warnings: List[str] = []
    
    for module_id in enabled_modules:
        if module_id not in AVAILABLE_MODULES:
            logger.warning(f"Unknown module: {module_id}")
            continue

        if module_id == "exchange_rate":
            currency_code = str(state.get("currency_code") or "").strip().upper()
            if not currency_code or currency_code == "USD":
                warnings.append(t(language, "warning.skip_exchange_usd"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.exchange_rate_module.v1",
                    reason="currency_is_usd_or_missing",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        if module_id == "fuel_energy":
            fuel_data = state.get("fuel_energy_data") or {}
            if not fuel_data.get("available") or not fuel_data.get("series"):
                warnings.append(t(language, "warning.skip_fuel_missing"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.fuel_energy_module.v1",
                    reason="module_data_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        if module_id == "livestock_animal_products":
            animal_data = state.get("livestock_animal_products_data") or {}
            if not animal_data.get("available") or not animal_data.get("series"):
                warnings.append(t(language, "warning.skip_livestock_missing"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.livestock_module.v1",
                    reason="module_data_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        if module_id == "labour_market":
            labour_data = state.get("labour_market_data") or {}
            if not labour_data.get("available") or not labour_data.get("series"):
                warnings.append(t(language, "warning.skip_labour_missing"))
                trace.record_skip(
                    node="module_orchestrator",
                    operation="market_monitor.labour_module.v1",
                    reason="module_data_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                continue
        
        try:
            module_class = AVAILABLE_MODULES[module_id]
            module = module_class()
            
            if not module.validate_inputs(state):
                operation = {
                    "exchange_rate": "market_monitor.exchange_rate_module.v1",
                    "fuel_energy": "market_monitor.fuel_energy_module.v1",
                    "livestock_animal_products": "market_monitor.livestock_module.v1",
                    "labour_market": "market_monitor.labour_module.v1",
                }.get(module_id, f"market_monitor.{module_id}_module.v1")
                trace.record_skip(
                    node="module_orchestrator",
                    operation=operation,
                    reason="required_inputs_unavailable",
                    artifact_type="module",
                    artifact_id=module_id,
                )
                if module_id == "exchange_rate":
                    missing = [
                        f
                        for f in getattr(module, "required_inputs", [])
                        if f not in state or state[f] is None
                    ]
                    warnings.append(t(language, "warning.skip_exchange_required", missing=missing))
                    continue
                continue
            
            # Corrections reuse the immutable, already-fetched module inputs.
            if not correction_mode:
                data_update = module.fetch_data(state)
                updates.update(data_update)
                state.update(data_update)
            
            # Generate section
            output = module.generate_section(state, llm)
            module_sections[module_id] = output.get("narrative", "")
            llm_calls += 1
            
            logger.info(f"Module '{module_id}' completed successfully")
            
        except LLMCallError:
            raise
        except Exception as e:
            logger.error(f"Module '{module_id}' failed: {e}")
            if module_id == "exchange_rate":
                warnings.append(t(language, "warning.skip_exchange_source", error=e))
            elif module_id == "fuel_energy":
                warnings.append(t(language, "warning.skip_fuel_error", error=e))
            elif module_id == "livestock_animal_products":
                warnings.append(t(language, "warning.skip_livestock_error", error=e))
            elif module_id == "labour_market":
                warnings.append(t(language, "warning.skip_labour_error", error=e))
            continue
    
    updates["module_sections"] = module_sections
    if correction_mode:
        sections = dict(state.get("report_draft_sections") or {})
        for module_id, section_text in module_sections.items():
            sections[f"{module_id.upper()}_ANALYSIS"] = section_text
        updates["report_draft_sections"] = sections
    if warnings:
        updates["warnings"] = warnings
    updates["llm_calls"] = state.get("llm_calls", 0) + llm_calls
    updates["llm_diagnostics"] = trace.snapshot()
    updates["current_node"] = "module_orchestrator"
    
    return updates
