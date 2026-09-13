"""Fixtures shared by both suites.

At the repository root rather than under ``engine/tests`` because
``service/tests`` needs the same thing: ``testpaths`` covers both, and pytest
only reads a conftest at or above the test it is collecting.

The only thing here is the datasheet downloader. ``read_datasheet`` fetches a
``pdf_url`` itself now -- Gemini does not fetch arbitrary URLs, so the bytes
have to travel in the request -- which means any test that hands the pipeline a
datasheet URL would otherwise reach for DNS. The suite is offline by contract,
so those tests take ``offline_pdf_fetch`` and get a real, tiny PDF back.

It is deliberately not autouse. ``test_grounding.py`` exercises the genuine
``fetch_pdf`` against a local HTTP server, and a fixture that silently replaced
the network everywhere would gut exactly the tests that are supposed to use it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from silkscreen.agents import grounding

FIXTURE_PDF = (
    Path(__file__).resolve().parent
    / "engine" / "tests" / "fixtures" / "tiny_datasheet.pdf"
)


@pytest.fixture
def offline_pdf_fetch(monkeypatch):
    """Serve ``fixtures/tiny_datasheet.pdf`` in place of any download.

    Yields the list of URLs asked for, so a test can assert what the pipeline
    tried to fetch as well as that it never left the machine.
    """
    pdf = FIXTURE_PDF.read_bytes()
    asked: list[str] = []

    def fake_fetch(url: str, **kwargs: object) -> bytes:
        asked.append(url)
        return pdf

    monkeypatch.setattr(grounding, "fetch_pdf", fake_fetch)
    return asked
