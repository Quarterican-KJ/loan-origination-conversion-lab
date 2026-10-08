from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from loan_lab.db import create_db_engine
from loan_lab.models import Base


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()
