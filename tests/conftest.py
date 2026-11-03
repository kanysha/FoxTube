from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        db_password=SecretStr("unit-test-secret"),
        db_host="127.0.0.1",
        db_port=1,
        public_origin="http://localhost:8080",
        media_root=tmp_path,
    )
    with TestClient(create_app(settings), base_url="http://localhost") as browser:
        yield browser
