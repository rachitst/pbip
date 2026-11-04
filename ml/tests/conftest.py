"""Fixtures shared by the fraud-model test modules."""

from pathlib import Path

import pytest
from pbip_datagen.__main__ import _export_fraud
from pbip_datagen.generator import GeneratorConfig
from pbip_ml.fraud.train import run


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The hybrid fraud model trained once on seed 42 (600 customers, 4 months), read-only.

    Training takes about 5 s on an idle 2-CPU laptop and was repeated, identically, by
    test_fraud_compiled_forest.py and test_fraud_inference.py; tests that damage or relabel the
    files copy them first.
    """
    data, out = tmp_path_factory.mktemp("data"), tmp_path_factory.mktemp("model")
    _export_fraud(GeneratorConfig(seed=42, customers=600, months=4), data)
    run(data, out)
    return out
