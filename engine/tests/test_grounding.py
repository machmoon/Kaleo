import base64
import http.server
import io
import ipaddress
import os
import socket
import threading
from pathlib import Path

import pytest
from pypdf import PdfWriter
from silkscreen.agents import grounding
from silkscreen.agents.retrieval import GeminiEmbedder, HashEmbedder
from silkscreen.agents.review import Finding, Severity

FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "tiny_datasheet.pdf"

FIXTURE_PAGE_TEXTS = [
    "SILK1117 voltage regulator datasheet. General description. Output current "
    "1A maximum.",
    "Pin definitions. Pin 1 GND ground. Pin 2 VOUT output. Pin 3 VIN input supply. "
    "VIN requires 100nF decoupling capacitor close to the pin.",
    "Application notes. Place a 22uF tantalum capacitor on VOUT for stability. "
    "Dropout voltage is 1.3V at full load.",
]

GROUNDING_PAGES = [
    "front matter",
    "VDDA requires 100nF decoupling capacitor close to pin 9",
    "appendix",
]


def _normalized_text(text: str) -> str:
    return " ".join(text.split())


def _url(server: http.server.HTTPServer, path: str) -> str:
    return f"http://127.0.0.1:{server.server_port}{path}"


def _corroboration_finding(citation: str) -> Finding:
    return Finding(
        severity=Severity.MARGINAL,
        title="VDDA decoupling missing",
        detail="No 100nF capacitor on VDDA",
        citation=citation,
    )


@pytest.fixture
def fixture_pdf_bytes() -> bytes:
    return FIXTURE_PDF.read_bytes()


@pytest.fixture
def bypass_ssrf_guard_for_localhost(monkeypatch):
    monkeypatch.setattr(
        grounding,
        "_resolved_addresses",
        lambda host, port, *, url: [ipaddress.ip_address("93.184.216.34")],
    )


@pytest.fixture
def start_server():
    servers = []

    def start(handler_cls):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def _respond(handler: http.server.BaseHTTPRequestHandler, status: int,
             body: bytes = b"", headers: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _handler_returning(body: bytes, status: int = 200):
    class Handler(_QuietHandler):
        def do_GET(self) -> None:
            _respond(self, status, body, {"Content-Type": "application/pdf"})

    return Handler


class _RedirectOnceHandler(_QuietHandler):
    final_body = b"final body after redirect"

    def do_GET(self) -> None:
        if self.path == "/start":
            _respond(self, 302, headers={"Location": "/final.pdf"})
        else:
            _respond(self, 200, self.final_body, {"Content-Type": "application/pdf"})


class _RedirectToFtpHandler(_QuietHandler):
    def do_GET(self) -> None:
        _respond(self, 302, headers={"Location": "ftp://evil.example/x.pdf"})


class _RedirectLoopHandler(_QuietHandler):
    def do_GET(self) -> None:
        _respond(self, 302, headers={"Location": "/loop"})


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value: dict) -> None:
        self.data[key] = value


class _RaisingStore:
    def get(self, key: str):
        raise RuntimeError("boom")

    def put(self, key: str, value: dict) -> None:
        pass


class _RecordingEmbedder:
    dimension = 5

    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        self.calls.append((len(texts), query))
        return [[float(len(t))] * self.dimension for t in texts]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x.pdf",
        "file:///etc/passwd",
        "data:application/pdf;base64,AAAA",
    ],
)
def test_fetch_pdf_rejects_disallowed_schemes(url):
    with pytest.raises(ValueError):
        grounding.fetch_pdf(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.pdf",
        "http://[::1]/x.pdf",
        "http://10.0.0.5/x.pdf",
        "http://192.168.1.1/x.pdf",
        "http://169.254.169.254/latest",
        "http://localhost/x.pdf",
    ],
)
def test_fetch_pdf_rejects_non_global_hosts(url):
    with pytest.raises(ValueError):
        grounding.fetch_pdf(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://[64:ff9b::a9fe:a9fe]/x.pdf",
        "http://[::ffff:169.254.169.254]/x.pdf",
        "http://[2002:a9fe:a9fe::]/x.pdf",
        "http://[fec0::1]/x.pdf",
    ],
)
def test_validate_url_rejects_ipv6_embedding_a_private_target(url):
    with pytest.raises(ValueError):
        grounding._validate_url(url)


def test_validate_url_allows_a_global_ipv6_literal():
    grounding._validate_url("http://[2606:4700:4700::1111]/x.pdf")


def test_fetch_pdf_rejects_missing_host():
    with pytest.raises(ValueError):
        grounding.fetch_pdf("http://")


def test_fetch_pdf_rejects_bad_port():
    with pytest.raises(ValueError):
        grounding.fetch_pdf("http://h:99999/x")


def test_fetch_pdf_returns_exact_response_body(
    start_server, bypass_ssrf_guard_for_localhost
):
    body = b"%PDF-1.4 minimal pdf bytes"
    server = start_server(_handler_returning(body))
    assert grounding.fetch_pdf(_url(server, "/x.pdf")) == body


def test_fetch_pdf_follows_a_relative_redirect(
    start_server, bypass_ssrf_guard_for_localhost
):
    server = start_server(_RedirectOnceHandler)
    result = grounding.fetch_pdf(_url(server, "/start"))
    assert result == _RedirectOnceHandler.final_body


def test_fetch_pdf_rejects_a_redirect_to_a_disallowed_scheme(
    start_server, bypass_ssrf_guard_for_localhost
):
    server = start_server(_RedirectToFtpHandler)
    with pytest.raises(ValueError):
        grounding.fetch_pdf(_url(server, "/x.pdf"))


def test_fetch_pdf_caps_redirects(start_server, bypass_ssrf_guard_for_localhost):
    server = start_server(_RedirectLoopHandler)
    with pytest.raises(grounding.GroundingError, match="more than"):
        grounding.fetch_pdf(_url(server, "/loop"))


def test_fetch_pdf_rejects_an_oversized_body(
    start_server, bypass_ssrf_guard_for_localhost
):
    server = start_server(_handler_returning(b"A" * 200_000))
    with pytest.raises(grounding.GroundingError, match="exceeds"):
        grounding.fetch_pdf(_url(server, "/big.pdf"), max_bytes=100_000)


def test_fetch_pdf_reports_http_errors(start_server, bypass_ssrf_guard_for_localhost):
    server = start_server(_handler_returning(b"nope", status=404))
    with pytest.raises(grounding.GroundingError, match="HTTP 404"):
        grounding.fetch_pdf(_url(server, "/missing.pdf"))


def test_fetch_pdf_reports_connection_refused(bypass_ssrf_guard_for_localhost):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(grounding.GroundingError):
        grounding.fetch_pdf(f"http://127.0.0.1:{port}/x.pdf")


def test_extract_pages_returns_three_pages_with_expected_text(fixture_pdf_bytes):
    pages = grounding.extract_pages(fixture_pdf_bytes)
    assert len(pages) == 3
    assert "SILK1117" in _normalized_text(pages[0])
    assert "100nF decoupling capacitor" in _normalized_text(pages[1])
    assert "22uF tantalum" in _normalized_text(pages[2])


def test_extract_pages_rejects_garbage_bytes():
    with pytest.raises(grounding.GroundingError):
        grounding.extract_pages(b"garbage")


def test_extract_pages_rejects_empty_bytes():
    with pytest.raises(grounding.GroundingError):
        grounding.extract_pages(b"")


def test_extract_pages_rejects_an_encrypted_pdf():
    writer = PdfWriter()
    writer.add_blank_page(72, 72)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(grounding.GroundingError, match="encrypted"):
        grounding.extract_pages(buf.getvalue())


def test_extract_pages_rejects_too_many_pages(monkeypatch):
    import pypdf

    class _FakePage:
        def extract_text(self):
            return "x"

    class _FakeReader:
        is_encrypted = False

        def __init__(self, *a, **kw):
            self.pages = [_FakePage()] * (grounding._MAX_PAGES + 1)

    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)
    with pytest.raises(grounding.GroundingError, match="more than"):
        grounding.extract_pages(b"whatever")


def test_extract_pages_rejects_too_much_text(monkeypatch):
    import pypdf

    class _FakePage:
        def extract_text(self):
            return "a" * 3_000_000

    class _FakeReader:
        is_encrypted = False

        def __init__(self, *a, **kw):
            self.pages = [_FakePage(), _FakePage()]

    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)
    with pytest.raises(grounding.GroundingError, match="character limit"):
        grounding.extract_pages(b"whatever")


def test_pages_for_part_with_data_matches_extract_pages(fixture_pdf_bytes):
    assert grounding.pages_for_part(data=fixture_pdf_bytes) == grounding.extract_pages(
        fixture_pdf_bytes
    )


def test_pages_for_part_needs_a_url_or_data():
    with pytest.raises(ValueError):
        grounding.pages_for_part()


def test_batching_embedder_splits_and_preserves_order():
    inner = _RecordingEmbedder()
    embedder = grounding.BatchingEmbedder(inner, batch_size=2)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    out = embedder.embed(texts, query=True)
    assert inner.calls == [(2, True), (2, True), (1, True)]
    assert out == [[float(len(t))] * 5 for t in texts]


def test_batching_embedder_forwards_query_false_by_default():
    inner = _RecordingEmbedder()
    embedder = grounding.BatchingEmbedder(inner, batch_size=3)
    embedder.embed(["a", "b", "c", "d"])
    assert all(query is False for _, query in inner.calls)


def test_batching_embedder_empty_input():
    embedder = grounding.BatchingEmbedder(_RecordingEmbedder())
    assert embedder.embed([]) == []


def test_batching_embedder_rejects_non_positive_batch_size():
    with pytest.raises(ValueError):
        grounding.BatchingEmbedder(_RecordingEmbedder(), batch_size=0)


def test_batching_embedder_proxies_dimension():
    inner = _RecordingEmbedder()
    embedder = grounding.BatchingEmbedder(inner, batch_size=4)
    assert embedder.dimension == inner.dimension


def test_store_and_load_pages_round_trip():
    store = FakeStore()
    pages = ["hello world", "second page text"]
    shards = grounding.store_pages(store, "SILK1117", pages)
    assert shards == 1
    assert list(store.data.keys()) == ["SILK1117#pages0"]
    assert grounding.load_pages(store, "SILK1117") == pages


def test_store_pages_sanitizes_hash_in_part_name():
    store = FakeStore()
    grounding.store_pages(store, "BIG#PART", ["x"])
    assert list(store.data.keys()) == ["BIG%23PART#pages0"]


def test_store_pages_distinct_keys_for_hash_and_underscore():
    store = FakeStore()
    grounding.store_pages(store, "A#B", ["hash pages"])
    grounding.store_pages(store, "A_B", ["underscore pages"])
    assert set(store.data.keys()) == {"A%23B#pages0", "A_B#pages0"}
    assert grounding._page_key("A#B", 0) != grounding._page_key("A_B", 0)
    assert grounding.load_pages(store, "A#B") == ["hash pages"]
    assert grounding.load_pages(store, "A_B") == ["underscore pages"]


def test_store_pages_shards_large_payloads_and_round_trips():
    store = FakeStore()
    pages = [f"page {i} " + "lorem ipsum dolor sit amet " * 50 for i in range(30)]
    shards = grounding.store_pages(store, "P", pages, shard_bytes=100)
    assert shards > 1
    for index in range(shards):
        record = store.data[f"P#pages{index}"]
        assert len(base64.b64decode(record["data"])) <= 100
    assert grounding.load_pages(store, "P") == pages


def test_store_and_load_pages_round_trip_unicode():
    store = FakeStore()
    pages = ["100 µF ±5% 温度範囲"]
    grounding.store_pages(store, "U", pages)
    assert grounding.load_pages(store, "U") == pages


def test_load_pages_missing_part_returns_none():
    assert grounding.load_pages(FakeStore(), "NOPE") is None


def test_load_pages_returns_none_when_a_middle_shard_is_deleted():
    store = FakeStore()
    pages = [f"page {i} " + "lorem ipsum dolor sit amet " * 50 for i in range(30)]
    shards = grounding.store_pages(store, "M", pages, shard_bytes=100)
    assert shards > 2
    del store.data["M#pages1"]
    assert grounding.load_pages(store, "M") is None


def test_load_pages_returns_none_for_corrupted_base64():
    store = FakeStore()
    grounding.store_pages(store, "C", ["hello"])
    store.data["C#pages0"]["data"] = "not valid base64!!"
    assert grounding.load_pages(store, "C") is None


def test_load_pages_returns_none_for_wrong_format_version():
    store = FakeStore()
    grounding.store_pages(store, "V", ["hello"])
    store.data["V#pages0"]["v"] = 999
    assert grounding.load_pages(store, "V") is None


def test_load_pages_returns_none_when_store_get_raises():
    assert grounding.load_pages(_RaisingStore(), "X") is None


def test_load_pages_rejects_an_absurd_shard_count_without_looping():
    class _CountingStore:
        def __init__(self) -> None:
            self.calls = 0
            self.rec0 = {
                "v": 1,
                "shard": 0,
                "of": 10_000,
                "data": base64.b64encode(b"x").decode("ascii"),
            }

        def get(self, key: str):
            self.calls += 1
            return self.rec0 if key.endswith("pages0") else None

        def put(self, key: str, value: dict) -> None:
            pass

    store = _CountingStore()
    assert grounding.load_pages(store, "X") is None
    assert store.calls <= 2


def test_load_pages_rejects_oversized_compressed_payload():
    store = FakeStore()
    big = base64.b64encode(b"A" * (9 * 1024 * 1024)).decode("ascii")
    store.data[grounding._page_key("Z", 0)] = {"v": 1, "shard": 0, "of": 2, "data": big}
    store.data[grounding._page_key("Z", 1)] = {"v": 1, "shard": 1, "of": 2, "data": big}
    assert grounding.load_pages(store, "Z") is None


def test_store_pages_rejects_non_positive_shard_bytes():
    with pytest.raises(ValueError):
        grounding.store_pages(FakeStore(), "X", ["a"], shard_bytes=0)


def test_store_and_load_pages_round_trip_a_million_char_page():
    store = FakeStore()
    huge = "a" * 1_000_000
    grounding.store_pages(store, "HUGE", [huge])
    assert grounding.load_pages(store, "HUGE") == [huge]


def test_build_index_over_fixture_pages():
    index = grounding.build_index(FIXTURE_PAGE_TEXTS, HashEmbedder())
    assert len(index) > 0
    hits = index.search("decoupling capacitor on VIN")
    assert set(index.pages_cited(hits)) <= {1, 2, 3}


@pytest.mark.parametrize(
    "citation,expected",
    [
        ("p.14", {14}),
        ("p 14", {14}),
        ("page 14", {14}),
        ("pg. 14", {14}),
        ("Pages 3 and 7", {3}),
        ("section 14", set()),
        ("", set()),
        ("p.100000", set()),
        ("p14", set()),
    ],
)
def test_cited_pages(citation, expected):
    assert grounding.cited_pages(citation) == expected


def test_ground_findings_corroborated_when_citation_matches_evidence():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    result = grounding.ground_findings(
        {"REG": index}, [_corroboration_finding("p.2")], k=1
    )
    gf = result[0]
    assert gf.status == grounding.GroundingStatus.CORROBORATED
    assert gf.evidence[0].page == 2
    assert "100nF" in gf.evidence[0].quote
    assert len(gf.evidence[0].quote) <= 300


def test_ground_findings_related_when_citation_page_has_no_evidence():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    result = grounding.ground_findings(
        {"REG": index}, [_corroboration_finding("p.3")], k=1
    )
    assert result[0].status == grounding.GroundingStatus.RELATED


def test_ground_findings_related_when_citation_page_shares_too_few_tokens():
    pages = ["alpha bravo charlie delta echo foxtrot golf hotel india"]
    index = grounding.build_index(pages, HashEmbedder())
    finding = Finding(
        severity=Severity.MARGINAL,
        title="alpha",
        detail="unrelatedword anotherword thirdword",
        citation="p.1",
    )
    result = grounding.ground_findings(
        {"REG": index}, [finding], k=1, min_score=0.0
    )
    gf = result[0]
    assert gf.evidence and gf.evidence[0].page == 1
    assert gf.status == grounding.GroundingStatus.RELATED


def test_ground_findings_related_when_citation_is_empty():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    result = grounding.ground_findings(
        {"REG": index}, [_corroboration_finding("")], k=1
    )
    assert result[0].status == grounding.GroundingStatus.RELATED


def test_ground_findings_unsupported_with_a_high_min_score():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    result = grounding.ground_findings(
        {"REG": index}, [_corroboration_finding("p.2")], min_score=0.99
    )
    assert result[0].status == grounding.GroundingStatus.UNSUPPORTED
    assert result[0].evidence == ()


def test_ground_findings_with_no_indexes_is_unsupported():
    result = grounding.ground_findings({}, [_corroboration_finding("p.2")])
    assert result[0].status == grounding.GroundingStatus.UNSUPPORTED


def test_ground_findings_with_no_findings_returns_empty_list():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    assert grounding.ground_findings({"REG": index}, []) == []


def test_ground_findings_rejects_non_positive_k():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    with pytest.raises(ValueError):
        grounding.ground_findings(
            {"REG": index}, [_corroboration_finding("p.2")], k=0
        )


def test_ground_findings_blank_finding_is_unsupported():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    finding = Finding(severity=Severity.NOTE, title="", detail="", citation="")
    result = grounding.ground_findings({"REG": index}, [finding])
    assert result[0].status == grounding.GroundingStatus.UNSUPPORTED


def test_ground_findings_picks_evidence_from_the_matching_index():
    mcu_pages = [
        "front matter",
        "GPIO pin PA0 configured as alternate function timer output compare channel",
    ]
    reg_pages = [
        "front matter",
        "capacitor tantalum output stability voltage regulator dropout feedback",
    ]
    mcu_index = grounding.build_index(mcu_pages, HashEmbedder())
    reg_index = grounding.build_index(reg_pages, HashEmbedder())
    finding = Finding(
        severity=Severity.NOTE,
        title="GPIO alternate function routing",
        detail="PA0 timer output compare alternate function GPIO configuration",
        citation="",
    )
    result = grounding.ground_findings({"REG": reg_index, "MCU": mcu_index}, [finding])
    assert result[0].evidence[0].part == "MCU"


def test_ground_findings_output_order_matches_input_and_is_deterministic():
    index = grounding.build_index(GROUNDING_PAGES, HashEmbedder())
    findings = [_corroboration_finding("p.2"), _corroboration_finding("p.3")]
    first = grounding.ground_findings({"REG": index}, findings, k=1)
    second = grounding.ground_findings({"REG": index}, findings, k=1)
    assert [gf.finding for gf in first] == findings
    assert first == second


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="needs GOOGLE_API_KEY")
def test_gemini_embedder_embeds_and_ranks_correctly():
    embedder = GeminiEmbedder()
    vectors = embedder.embed(["one", "two", "three"])
    assert len(vectors) == 3
    assert all(len(v) == 768 for v in vectors)

    sentences = [
        "The sky is blue on a clear afternoon.",
        "A 22uF tantalum capacitor stabilizes the regulator output voltage.",
        "The cat sat on the warm windowsill.",
    ]
    index = grounding.build_index(sentences, embedder)
    hits = index.search("which capacitor stabilizes the regulator output", k=1)
    assert hits[0].chunk.text == sentences[1]


@pytest.mark.skipif(not os.getenv("GOOGLE_API_KEY"), reason="needs GOOGLE_API_KEY")
def test_batching_embedder_over_gemini_embedder():
    embedder = grounding.BatchingEmbedder(GeminiEmbedder(), batch_size=2)
    texts = ["one", "two", "three", "four", "five"]
    vectors = embedder.embed(texts)
    assert len(vectors) == 5
    assert all(len(v) == 768 for v in vectors)


# ------------------------------------------------- datasheet download, end to end


def test_read_datasheet_downloads_over_a_real_socket(
    start_server, bypass_ssrf_guard_for_localhost, fixture_pdf_bytes
):
    """The default path: a URL becomes bytes in the model request.

    Everything else about this fix is asserted with an injected fetcher, which
    proves the wiring but not that the wiring reaches a socket. This one serves
    the fixture PDF over real HTTP and checks the model was handed exactly those
    bytes -- the step that was silently missing when Gemini was asked to fetch
    the URL itself.
    """
    import json

    from silkscreen.agents import ScriptedModel, read_datasheet

    facts_json = json.dumps({
        "part_number": "SILK1117",
        "package": "SOT-223-3",
        "pin_count": 3,
        "pins": [
            {"number": "1", "name": "GND"},
            {"number": "2", "name": "VOUT"},
            {"number": "3", "name": "VIN"},
        ],
    })
    server = start_server(_handler_returning(fixture_pdf_bytes))
    model = ScriptedModel(responses=[facts_json])

    facts = read_datasheet(model, "SILK1117", pdf_url=_url(server, "/ds.pdf"))

    document = model.calls[0]["documents"][0]
    assert document.data == fixture_pdf_bytes
    assert document.url is None
    assert facts.pin_map() == {"GND": "1", "VOUT": "2", "VIN": "3"}


def test_a_url_serving_html_is_refused_before_the_model(
    start_server, bypass_ssrf_guard_for_localhost
):
    """The LCSC failure, reproduced over HTTP rather than with a stub."""
    from silkscreen.agents import ScriptedModel, read_datasheet

    page = b"<!doctype html><html><head><title>AMS1117-3.3 | LCSC</title>"
    server = start_server(_handler_returning(page))
    model = ScriptedModel(responses=["{}"])

    with pytest.raises(grounding.GroundingError, match="did not return a PDF"):
        read_datasheet(model, "AMS1117-3.3", pdf_url=_url(server, "/C6186.pdf"))
    assert model.calls == []
