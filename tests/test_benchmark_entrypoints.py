"""Old benchmark commands share the production calibration command."""

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("module_name", ["full_benchmark", "workflow.benchmark_local_models"])
def test_legacy_benchmark_import_has_no_inference_and_main_delegates(monkeypatch, capsys, module_name):
    main = Mock(return_value=0)
    monkeypatch.setitem(sys.modules, "workflow.calibrate", SimpleNamespace(main=main))
    module = importlib.import_module(module_name)
    main.assert_not_called()
    assert module.main() == 0
    main.assert_called_once_with()
    assert "deprecated" in capsys.readouterr().err
