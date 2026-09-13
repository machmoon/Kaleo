from __future__ import annotations

import base64
import io
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit

from .retrieval import (
    CHUNK_CHARS,
    CHUNK_OVERLAP,
    Embedder,
    Retrieved,
    VectorIndex,
    chunk_pages,
)
from .review import Finding

__all__ = [
    "BatchingEmbedder",
    "Evidence",
    "GroundedFinding",
    "GroundingError",
    "GroundingStatus",
    "build_index",
    "cited_pages",
    "extract_pages",
    "fetch_pdf",
    "ground_findings",
    "load_pages",
    "pages_for_part",
    "store_pages",
]

_Address = ipaddress.IPv4Address | ipaddress.IPv6Address

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_READ_CHUNK = 64 * 1024
_USER_AGENT = "silkscreen/0.1"

_PAGE_FORMAT = 1
_MAX_DECOMPRESSED_BYTES = 67_108_864
_MAX_SHARDS = 4096
_MAX_COMPRESSED_BYTES = 16_777_216

_MAX_PAGES = 2000
_MAX_TEXT_CHARS = 5_000_000

_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")

_QUERY_CHARS = 500
_QUOTE_CHARS = 300
_MAX_CITED_PAGE = 99_999

_PAGE_REF = re.compile(r"\b(?:p|pp|pg|page|pages)\b\.?\s*(\d+)", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class GroundingError(RuntimeError):
    pass


class _Store(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, value: dict[str, Any]) -> None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _as_address(value: str) -> _Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _resolved_addresses(host: str, port: int, *, url: str) -> list[_Address]:
    found: list[_Address] = []
    literal = _as_address(host)
    if literal is not None:
        found.append(literal)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise GroundingError(
            f"fetching {url} failed: cannot resolve {host!r}: {exc}"
        ) from exc

    for info in infos:
        raw = info[4][0]
        address = _as_address(raw)
        if address is None:
            raise ValueError(f"host {host!r} resolved to {raw!r}")
        found.append(address)
    if not found:
        raise ValueError(f"host {host!r} resolved to no addresses")
    return found


def _embedded_ipv4(addr: _Address) -> ipaddress.IPv4Address | None:
    if not isinstance(addr, ipaddress.IPv6Address):
        return None
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    if addr in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    value = int(addr)
    if value >> 32 == 0 and value & 0xFFFFFFFF != 0:
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    return None


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported scheme {parts.scheme!r} in {url!r}")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"malformed authority in {url!r}: {exc}") from exc
    if not host:
        raise ValueError(f"no host in {url!r}")
    for address in _resolved_addresses(host, port or _DEFAULT_PORTS[scheme], url=url):
        if isinstance(address, ipaddress.IPv6Address):
            embedded = _embedded_ipv4(address)
            if (
                not address.is_global
                or address in _SITE_LOCAL
                or (embedded is not None and not embedded.is_global)
            ):
                raise ValueError(
                    f"host {host!r} resolves to non-global address {address}"
                )
        elif not address.is_global:
            raise ValueError(
                f"host {host!r} resolves to non-global address {address}"
            )


def _read_capped(response: Any, *, max_bytes: int, url: str) -> bytes:
    buffer = bytearray()
    while True:
        try:
            block = response.read(_READ_CHUNK)
        except Exception as exc:
            raise GroundingError(f"fetching {url} failed: {exc}") from exc
        if not block:
            break
        buffer.extend(block)
        if len(buffer) > max_bytes:
            raise GroundingError(
                f"fetching {url} failed: body exceeds {max_bytes} bytes"
            )
    return bytes(buffer)


def fetch_pdf(url: str, *, timeout_s: float = 30.0,
              max_bytes: int = 50_000_000) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        _validate_url(current)
        request = urllib.request.Request(
            current, headers={"User-Agent": _USER_AGENT}
        )
        try:
            response = opener.open(request, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            code, reason = exc.code, exc.reason
            exc.close()
            if code in _REDIRECT_CODES and location:
                current = urljoin(current, location)
                continue
            raise GroundingError(
                f"fetching {url} failed: HTTP {code} {reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GroundingError(f"fetching {url} failed: {exc.reason}") from exc
        except OSError as exc:
            raise GroundingError(f"fetching {url} failed: {exc}") from exc
        with response:
            return _read_capped(response, max_bytes=max_bytes, url=url)
    raise GroundingError(
        f"fetching {url} failed: more than {_MAX_REDIRECTS} redirects"
    )


def extract_pages(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise GroundingError(
            'pypdf is not installed. pip install -e ".[agents]"'
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise GroundingError(f"could not open the PDF: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:
            raise GroundingError(f"could not decrypt the PDF: {exc}") from exc
        if not unlocked:
            raise GroundingError("the PDF is encrypted and needs a password")

    try:
        pages = list(reader.pages)
    except Exception as exc:
        raise GroundingError(f"could not read the PDF pages: {exc}") from exc

    if not pages:
        raise GroundingError("the PDF has no pages")

    if len(pages) > _MAX_PAGES:
        raise GroundingError(
            f"the PDF has {len(pages)} pages, more than the {_MAX_PAGES} allowed"
        )

    texts: list[str] = []
    total_chars = 0
    for page in pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        total_chars += len(text)
        if total_chars > _MAX_TEXT_CHARS:
            raise GroundingError(
                f"the PDF text exceeds the {_MAX_TEXT_CHARS} character limit"
            )
        texts.append(text)
    return texts


def pages_for_part(*, url: str | None = None, data: bytes | None = None,
                   timeout_s: float = 30.0,
                   max_bytes: int = 50_000_000) -> list[str]:
    if data is not None:
        return extract_pages(data)
    if url is None:
        raise ValueError("pages_for_part needs a url or data")
    return extract_pages(
        fetch_pdf(url, timeout_s=timeout_s, max_bytes=max_bytes)
    )


class BatchingEmbedder:
    def __init__(self, inner: Embedder, batch_size: int = 96) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        self.inner = inner
        self.batch_size = batch_size
        self.dimension = inner.dimension

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors.extend(self.inner.embed(batch, query=query))
        return vectors


def _page_key(part: str, index: int) -> str:
    return f"{quote(part, safe='')}#pages{index}"


def _shard(store: _Store, part: str, index: int) -> tuple[bytes, int] | None:
    try:
        record = store.get(_page_key(part, index))
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    if record.get("v") != _PAGE_FORMAT or record.get("shard") != index:
        return None
    total = record.get("of")
    if not isinstance(total, int) or total < 1 or index >= total:
        return None
    payload = record.get("data")
    if not isinstance(payload, str):
        return None
    try:
        return base64.b64decode(payload, validate=True), total
    except ValueError:
        return None


def store_pages(store: _Store, part: str, pages: list[str], *,
                shard_bytes: int = 700_000) -> int:
    if shard_bytes < 1:
        raise ValueError(f"shard_bytes must be positive, got {shard_bytes}")
    blob = zlib.compress(json.dumps(pages).encode("utf-8"))
    shards = [blob[i : i + shard_bytes] for i in range(0, len(blob), shard_bytes)]
    total = len(shards)
    for index, shard in enumerate(shards):
        store.put(
            _page_key(part, index),
            {
                "v": _PAGE_FORMAT,
                "shard": index,
                "of": total,
                "data": base64.b64encode(shard).decode("ascii"),
            },
        )
    return total


def load_pages(store: _Store, part: str) -> list[str] | None:
    first = _shard(store, part, 0)
    if first is None:
        return None
    total = first[1]
    if total > _MAX_SHARDS:
        return None
    pieces = [first[0]]
    compressed = len(first[0])
    if compressed > _MAX_COMPRESSED_BYTES:
        return None
    for index in range(1, total):
        found = _shard(store, part, index)
        if found is None or found[1] != total:
            return None
        compressed += len(found[0])
        if compressed > _MAX_COMPRESSED_BYTES:
            return None
        pieces.append(found[0])

    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(b"".join(pieces), _MAX_DECOMPRESSED_BYTES)
    except zlib.error:
        return None
    if decompressor.unconsumed_tail or not decompressor.eof:
        return None
    try:
        pages = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(pages, list):
        return None
    if any(not isinstance(page, str) for page in pages):
        return None
    return pages


def build_index(pages: list[str], embedder: Embedder, *, size: int = CHUNK_CHARS,
                overlap: int = CHUNK_OVERLAP) -> VectorIndex:
    index = VectorIndex(embedder=embedder)
    index.add(chunk_pages(pages, size=size, overlap=overlap))
    return index


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3}


def cited_pages(citation: str) -> set[int]:
    found: set[int] = set()
    for match in _PAGE_REF.finditer(citation):
        value = int(match.group(1))
        if value <= _MAX_CITED_PAGE:
            found.add(value)
    return found


class GroundingStatus(StrEnum):
    CORROBORATED = "corroborated"
    RELATED = "related"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Evidence:
    part: str
    page: int
    score: float
    quote: str


@dataclass(frozen=True)
class GroundedFinding:
    finding: Finding
    status: GroundingStatus
    evidence: tuple[Evidence, ...]


def ground_findings(indexes: dict[str, VectorIndex], findings: list[Finding], *,
                    k: int = 4, min_score: float = 0.05) -> list[GroundedFinding]:
    if k < 1:
        raise ValueError(f"k must be positive, got {k}")

    grounded: list[GroundedFinding] = []
    for finding in findings:
        query = f"{finding.title} {finding.detail}".strip()[:_QUERY_CHARS]
        if not query:
            grounded.append(
                GroundedFinding(
                    finding=finding,
                    status=GroundingStatus.UNSUPPORTED,
                    evidence=(),
                )
            )
            continue

        hits: list[tuple[str, Retrieved]] = []
        for part, index in indexes.items():
            hits.extend((part, hit) for hit in index.search(query, k=k))
        hits.sort(key=lambda item: (-item[1].score, item[0], item[1].chunk.index))
        kept = [(part, hit) for part, hit in hits[:k] if hit.score >= min_score]

        evidence = tuple(
            Evidence(
                part=part,
                page=hit.chunk.page,
                score=hit.score,
                quote=hit.chunk.text[:_QUOTE_CHARS],
            )
            for part, hit in kept
        )
        finding_tokens = _tokens(f"{finding.title} {finding.detail}")
        overlap_pages = cited_pages(finding.citation) & {e.page for e in evidence}
        corroborated = bool(overlap_pages) and any(
            len(finding_tokens & _tokens(e.quote)) >= 2
            for e in evidence
            if e.page in overlap_pages
        )
        if corroborated:
            status = GroundingStatus.CORROBORATED
        elif evidence:
            status = GroundingStatus.RELATED
        else:
            status = GroundingStatus.UNSUPPORTED
        grounded.append(
            GroundedFinding(finding=finding, status=status, evidence=evidence)
        )
    return grounded
