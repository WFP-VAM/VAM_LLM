"""Executed in a fresh interpreter by the architecture acceptance test."""
import importlib.abc
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class NoCloudSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'google', 'vertexai', 'boto3', 'botocore', 'azure'}:
            raise AssertionError('Cloud SDK import forbidden in application workflow: ' + fullname)
# Installed namespace packages may be preloaded by site .pth files. Remove those empty namespaces first.
for name in list(sys.modules):
    if name == 'google' or name.startswith('google.'):
        del sys.modules[name]
sys.meta_path.insert(0, NoCloudSdk())

import pytest
from dataclasses import replace
from app.shared.llm import ModelProfile
from app.shared.llm import client

class NoInference:
    name = 'laboratory'
    def generate(self, *args):
        raise AssertionError('Workflow must inject its synthetic model')

def profiles(service, **kwargs):
    profile = ModelProfile(service=service, provider='laboratory', model='text-laboratory', location='local-lab',
                           temperature=0.2, timeout_seconds=600, max_output_tokens=50000, max_characters=1200000,
                           max_input_tokens=250000, attempts=2 if service == 'seasonal-outlook' else 1)
    if service == 'seasonal-outlook':
        return dict(evidence=replace(profile, model='image-laboratory', max_output_tokens=19000), report=profile)
    if service == 'mfi-drafter':
        return dict(text=profile, summary=replace(profile, timeout_seconds=180))
    return dict(text=profile)

class NeutralConfiguration:
    @pytest.fixture(autouse=True)
    def neutral_configuration(self, monkeypatch):
        monkeypatch.setattr(client, 'service_profiles', profiles)
        monkeypatch.setattr(client, 'default_provider', lambda *args, **kwargs: NoInference())

tests = [
    'tests/test_mfi_light_workflow.py::test_five_six_seven_calls_and_complete_report',
    'tests/test_mfi_light_workflow.py::test_http_and_streamlit_share_results_and_recovery_endpoints_are_gone',
    'tests/test_mfi_light_workflow.py::test_oversized_groups_split_without_repeating_successful_sections',
    'tests/test_seasonal_outlook.py::test_complete_workflow_and_exports',
    'tests/test_market_monitor_phase6.py::test_trend_prompt_and_output_are_role_aware',
    'tests/test_market_monitor_phase6.py::test_highlights_receives_context_and_exact_correction_flags',
    'tests/test_market_monitor_phase6.py::test_narrative_correction_updates_only_target_and_uses_adaptive_ranges',
    'tests/test_market_monitor_phase6.py::test_red_team_receives_basket_ground_truth_and_normalizes_flags',
]
code = pytest.main([*tests, '-q', '-p', 'no:cacheprovider', '--basetemp', sys.argv[1]], plugins=[NeutralConfiguration()])
assert not any(name.startswith('google.') or name.split('.')[0] in {'boto3', 'botocore', 'vertexai', 'azure'} for name in sys.modules)
if code == 0:
    print('CLOUD_FREE_WORKFLOWS_PASSED')
raise SystemExit(code)
