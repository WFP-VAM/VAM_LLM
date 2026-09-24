"""Operational release controls for MFI Drafter 2.0.

The current build contains one methodology implementation only.  Version 1 is
the previous deployment revision and is never imported or executed here.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from .schemas import MFIReleaseControl


MFI_DRAFTER_ANALYSIS_VERSION_ENV = "MFI_DRAFTER_ANALYSIS_VERSION"
MFI_DRAFTER_DISABLED_CODE = "mfi_drafter_analysis_v2_disabled"
MFI_DRAFTER_SERVICE_NAME = "mfi-drafter"


class MFIAnalysisVersionDisabled(RuntimeError):
    """Raised when this 2.0-only build is not explicitly enabled."""

    status_code = 503
    code = MFI_DRAFTER_DISABLED_CODE

    def __init__(self, control: MFIReleaseControl):
        self.control = control
        if control.configuration_status == "invalid":
            message = (
                f"{MFI_DRAFTER_ANALYSIS_VERSION_ENV} must be exactly '2' to "
                "enable MFI Drafter 2.0; the configured value is invalid."
            )
        else:
            message = (
                "MFI Drafter 2.0 report generation is disabled. Set "
                f"{MFI_DRAFTER_ANALYSIS_VERSION_ENV}=2 in the pilot or "
                "release deployment."
            )
        super().__init__(message)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "release_control": self.control.model_dump(),
        }


def mfi_release_control(
    environ: Optional[Mapping[str, str]] = None,
) -> MFIReleaseControl:
    """Resolve the fail-closed deployment gate and auditable revision metadata."""

    source = os.environ if environ is None else environ
    raw_value = source.get(MFI_DRAFTER_ANALYSIS_VERSION_ENV)
    normalized = str(raw_value or "").strip()
    if normalized == "2":
        analysis_version = "2"
        enabled = True
        configuration_status = "configured"
    elif normalized in {"", "1"}:
        analysis_version = "1"
        enabled = False
        configuration_status = (
            "default_disabled" if normalized == "" else "configured"
        )
    else:
        analysis_version = normalized
        enabled = False
        configuration_status = "invalid"

    deployment_revision = (
        str(source.get("K_REVISION") or source.get("REVISION_ID") or "").strip()
        or None
    )
    deployment_service = (
        str(source.get("K_SERVICE") or MFI_DRAFTER_SERVICE_NAME).strip()
        or MFI_DRAFTER_SERVICE_NAME
    )
    return MFIReleaseControl(
        analysis_version=analysis_version,
        enabled=enabled,
        configuration_status=configuration_status,
        service_name=deployment_service,
        deployment_revision=deployment_revision,
    )


def require_mfi_analysis_v2(
    control: Optional[MFIReleaseControl] = None,
) -> MFIReleaseControl:
    """Return an enabled immutable snapshot or fail before any report work."""

    resolved = control or mfi_release_control()
    if not resolved.enabled or resolved.analysis_version != "2":
        raise MFIAnalysisVersionDisabled(resolved)
    return resolved
