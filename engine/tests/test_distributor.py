"""The Mouser verifier, offline: a recorded transport answers every request.

No socket is ever opened -- ``urllib_transport`` is exercised only as far
as its host check, which refuses before any request is built.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.distributor import (
    DISTRIBUTOR,
    MAX_RESPONSE_BYTES,
    MOUSER_API_KEY,
    MOUSER_HOST,
    DistributorError,
    HttpRequest,
    HttpResponse,
    MouserClient,
    Verdict,
    ensure_mouser_url,
    from_env,
    urllib_transport,
    verify_mpns,
)
from silkscreen.sourcing import SourcingEntry

KEY = "test-key-0123456789"


def _hit(mpn: str, *, package: str | None = "SOT-223-3") -> bytes:
    attributes = (
        [{"AttributeName": "Package / Case", "AttributeValue": package}]
        if package is not None
        else []
    )
    return json.dumps(
        {
            "Errors": [],
            "SearchResults": {
                "NumberOfResult": 1,
                "Parts": [
                    {
                        "MouserPartNumber": "511-LD1117S33TR",
                        "ManufacturerPartNumber": mpn,
                        "Manufacturer": "STMicroelectronics",
                        "ProductDetailUrl": "https://www.mouser.com/ProductDetail/x",
                        "ProductAttributes": attributes,
                    }
                ],
            },
        }
    ).encode()


def _miss() -> bytes:
    return json.dumps(
        {"Errors": [], "SearchResults": {"NumberOfResult": 0, "Parts": []}}
    ).encode()


class Recorded:
    """A transport that answers from a script and remembers every request."""

    def __init__(self, *responses: HttpResponse | Exception):
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# ---------------------------------------------------------------- request


def test_request_is_the_documented_exact_search_with_the_key_in_the_query():
    client = MouserClient(KEY, Recorded())
    request = client.request("LD1117S33TR")
    assert request.method == "POST"
    assert request.url.startswith(f"https://{MOUSER_HOST}/api/v2/search/partnumber?")
    assert f"apiKey={KEY}" in request.url
    assert json.loads(request.body) == {
        "SearchByPartRequest": {
            "mouserPartNumber": "LD1117S33TR",
            "partSearchOptions": "Exact",
        }
    }
    assert request.headers["Content-Type"] == "application/json"


def test_client_refuses_an_empty_key():
    with pytest.raises(ValueError):
        MouserClient("   ")


# ---------------------------------------------------------------- verdicts


def test_an_exact_hit_is_verified_with_the_distributors_details():
    transport = Recorded(HttpResponse(200, _hit("LD1117S33TR")))
    verdict = MouserClient(KEY, transport)("ST", "ld1117s33tr")
    assert verdict.status == "verified"
    assert verdict.mpn == "LD1117S33TR"
    assert verdict.sku == "511-LD1117S33TR"
    assert verdict.manufacturer == "STMicroelectronics"
    assert verdict.url == "https://www.mouser.com/ProductDetail/x"
    assert verdict.package == "SOT-223-3"
    assert len(transport.requests) == 1


def test_a_different_part_number_in_the_results_is_unlisted():
    transport = Recorded(HttpResponse(200, _hit("LD1117S33CTR")))
    assert MouserClient(KEY, transport)(None, "LD1117S33TR").status == "unlisted"


def test_no_parts_is_unlisted():
    assert MouserClient(KEY, Recorded(HttpResponse(200, _miss())))(
        None, "NOT-A-PART-123"
    ) == Verdict("unlisted")


def test_a_mouser_error_entry_is_unavailable_naming_the_code():
    body = json.dumps(
        {"Errors": [{"Code": "InvalidAuthorization", "Message": "bad key"}]}
    ).encode()
    verdict = MouserClient(KEY, Recorded(HttpResponse(200, body)))(None, "X")
    assert verdict.status == "unavailable"
    assert "InvalidAuthorization: bad key" in verdict.detail
    assert KEY not in verdict.detail


def test_a_non_200_is_unavailable_with_the_status():
    verdict = MouserClient(KEY, Recorded(HttpResponse(503, b"down")))(None, "X")
    assert verdict.status == "unavailable"
    assert "HTTP 503" in verdict.detail


def test_a_body_that_is_not_json_or_not_an_object_is_unavailable():
    assert "not JSON" in MouserClient.read(HttpResponse(200, b"<html>"), "X").detail
    assert "JSON list" in MouserClient.read(HttpResponse(200, b"[1]"), "X").detail
    assert "SearchResults" in MouserClient.read(HttpResponse(200, b"{}"), "X").detail


def test_an_oversize_body_is_unavailable_not_parsed():
    body = b"{" + b" " * MAX_RESPONSE_BYTES + b"}"
    verdict = MouserClient.read(HttpResponse(200, body), "X")
    assert verdict.status == "unavailable"
    assert str(MAX_RESPONSE_BYTES) in verdict.detail


def test_a_transport_failure_is_unavailable_and_never_carries_the_url():
    transport = Recorded(DistributorError("network_error", "timed out"))
    verdict = MouserClient(KEY, transport)(None, "X")
    assert verdict.status == "unavailable"
    assert "timed out" in verdict.detail
    assert KEY not in verdict.detail and "http" not in verdict.detail


def test_a_non_http_product_url_is_dropped():
    body = json.loads(_hit("X"))
    body["SearchResults"]["Parts"][0]["ProductDetailUrl"] = "javascript:alert(1)"
    verdict = MouserClient.read(HttpResponse(200, json.dumps(body).encode()), "X")
    assert verdict.status == "verified" and verdict.url is None


# ---------------------------------------------------------------- host guard


def test_only_https_to_the_mouser_host_is_ever_sent():
    with pytest.raises(DistributorError, match="bad_host"):
        ensure_mouser_url("https://api.mouser.com.evil.example/api/v2/search")
    with pytest.raises(DistributorError, match="bad_host"):
        ensure_mouser_url("http://api.mouser.com/api/v2/search/partnumber")
    assert ensure_mouser_url("https://api.mouser.com/x") == "https://api.mouser.com/x"


def test_the_real_transport_refuses_a_foreign_host_before_connecting():
    send = urllib_transport(timeout_s=0.01)
    with pytest.raises(DistributorError, match="bad_host"):
        send(HttpRequest("POST", "https://example.com/?apiKey=x"))


# ---------------------------------------------------------------- from_env


def test_from_env_is_none_without_a_key_and_a_client_with_one():
    assert from_env({}) is None
    assert from_env({MOUSER_API_KEY: "  "}) is None
    client = from_env({MOUSER_API_KEY: KEY}, Recorded())
    assert isinstance(client, MouserClient)


# ---------------------------------------------------------------- verify_mpns


def _rows() -> list[SourcingEntry]:
    return [
        SourcingEntry("U1", "3.3V", "device", "SOT-223-3_TabPin2",
                      manufacturer=None, mpn="LD1117S33TR", mpn_status="proposed"),
        SourcingEntry("R1", "10k", "resistor", "C_0603"),
        SourcingEntry("C1", "22uF", "capacitor", "C_1206",
                      manufacturer="Samsung", mpn="CL31A226KAHNNNE",
                      mpn_status="proposed"),
        SourcingEntry("C2", "1uF", "capacitor", "C_0603",
                      manufacturer="Murata", mpn="GRM188R61A105KA61D",
                      mpn_status="proposed"),
    ]


def test_verify_mpns_marks_hits_verified_and_misses_loudly():
    asked: list[tuple[str | None, str]] = []

    def verify(manufacturer, mpn):
        asked.append((manufacturer, mpn))
        if mpn == "LD1117S33TR":
            return Verdict("verified", manufacturer="STMicroelectronics",
                           mpn=mpn, sku="511-LD1117S33TR",
                           url="https://www.mouser.com/ProductDetail/x")
        return Verdict("unlisted")

    rows, warnings = verify_mpns(_rows(), verify)
    assert warnings == []
    # Only rows with a part number are asked; R1 has none.
    assert [m for _, m in asked] == [
        "LD1117S33TR", "CL31A226KAHNNNE", "GRM188R61A105KA61D"
    ]
    by_ref = {r.ref: r for r in rows}
    u1 = by_ref["U1"]
    assert u1.mpn_status == "verified"
    assert u1.distributor == DISTRIBUTOR == "Mouser"
    assert u1.distributor_sku == "511-LD1117S33TR"
    assert u1.distributor_url == "https://www.mouser.com/ProductDetail/x"
    assert u1.manufacturer == "STMicroelectronics"  # filled in, the model gave none
    assert u1.verify_error is None
    assert by_ref["C1"].mpn_status == "proposed"
    assert by_ref["C1"].manufacturer == "Samsung"  # the model's spelling kept
    assert by_ref["C1"].verify_error == "Mouser does not list CL31A226KAHNNNE"
    assert by_ref["R1"] == _rows()[1]


def test_verify_mpns_stops_after_the_first_unavailable_and_says_so_per_row():
    asked: list[str] = []

    def verify(manufacturer, mpn):
        asked.append(mpn)
        return Verdict("unavailable", detail="Mouser answered HTTP 503")

    rows, warnings = verify_mpns(_rows(), verify)
    assert asked == ["LD1117S33TR"]
    assert warnings == [
        "part numbers were not verified past U1: Mouser answered HTTP 503"
    ]
    by_ref = {r.ref: r for r in rows}
    assert by_ref["U1"].mpn_status == "proposed"
    assert by_ref["U1"].verify_error == "Mouser answered HTTP 503"
    assert by_ref["C1"].verify_error == "Mouser was not asked: Mouser answered HTTP 503"
    assert by_ref["C2"].verify_error == "Mouser was not asked: Mouser answered HTTP 503"
    assert by_ref["R1"].verify_error is None


def test_verify_mpns_reports_a_verifier_that_raised_on_the_row():
    def verify(manufacturer, mpn):
        raise OSError("connection reset")

    rows, warnings = verify_mpns(_rows(), verify)
    assert rows[0].mpn_status == "proposed"
    assert "OSError: connection reset" in rows[0].verify_error
    assert len(warnings) == 1 and "U1" in warnings[0]


def test_verify_mpns_treats_an_unknown_verdict_as_a_programming_error():
    with pytest.raises(ValueError, match="expected one of"):
        verify_mpns(_rows(), lambda m, p: Verdict("maybe"))


def test_verify_mpns_stops_asking_when_its_budget_runs_out():
    import time

    asked: list[str] = []

    def slow(manufacturer, mpn):
        asked.append(mpn)
        time.sleep(0.05)
        return Verdict("unlisted")

    rows, warnings = verify_mpns(_rows(), slow, budget_s=0.01)
    assert asked == ["LD1117S33TR"]
    assert warnings == [
        "part numbers were not verified past C1: the 0.01 s verification budget ran out"
    ]
    by_ref = {r.ref: r for r in rows}
    assert by_ref["U1"].verify_error == "Mouser does not list LD1117S33TR"
    assert by_ref["C1"].verify_error == (
        "Mouser was not asked: the 0.01 s verification budget ran out"
    )
    assert by_ref["C2"].mpn_status == "proposed" and by_ref["C2"].verify_error


def test_the_real_transport_turns_a_truncated_response_into_a_distributor_error():
    import http.client
    from unittest import mock

    send = urllib_transport(timeout_s=0.01)
    truncated = mock.patch(
        "urllib.request.OpenerDirector.open",
        side_effect=http.client.IncompleteRead(b"x"),
    )
    with truncated, pytest.raises(DistributorError, match="network_error"):
        send(HttpRequest("POST", "https://api.mouser.com/api/v2/search/partnumber"))
