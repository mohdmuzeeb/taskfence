"""pytest plugin: `taskfence_latches` fixture (registered via the pytest11 entry point)."""

from __future__ import annotations

from typing import Iterator

import pytest

from .latches import Latches


@pytest.fixture
def taskfence_latches() -> Iterator[Latches]:
    with Latches() as l:
        yield l
