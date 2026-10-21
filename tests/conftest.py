import os
from uuid import uuid4

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client


@pytest.fixture
def firestore_db(monkeypatch):
    host = os.getenv("FIRESTORE_EMULATOR_HOST")
    if not host:
        pytest.skip("FIRESTORE_EMULATOR_HOST is required; never test against the cloud")
    if host not in {"127.0.0.1:8080", "localhost:8080"}:
        pytest.fail("Integration tests must use the local Firestore emulator on port 8080")
    project = "demo-stock-assistant"
    return Client(project=project, credentials=AnonymousCredentials())


@pytest.fixture
def owner_uid():
    return "test-" + uuid4().hex
