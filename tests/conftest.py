"""Global test setup: environment variables must be set before any app import."""

import os
import tempfile

from cryptography.fernet import Fernet

_TMP = tempfile.mkdtemp(prefix="insql-test-")

os.environ["SECRET_KEY"] = "test-secret-key-with-at-least-32-bytes-of-entropy"
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "metadata.db")
os.environ["TURSO_ORG"] = "testorg"
os.environ["TURSO_PLATFORM_TOKEN"] = "test-platform-token"
os.environ["TURSO_ENC_KEY"] = Fernet.generate_key().decode()
os.environ["TURSO_GROUP"] = "default"
os.environ["TURSO_LOCATION"] = "aws-ap-south-1"
os.environ["TURSO_MAX_DATABASES"] = "100"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["USAGE_SYNC_INTERVAL_SECONDS"] = "3600"
os.environ.pop("TURSO_LIVE", None)

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def metadata_tables():
    from turso.models import create_tables

    create_tables()
