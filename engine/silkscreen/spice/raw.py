"""Reader for the SPICE rawfile, in the dialects ngspice and LTspice write.

Both simulators emit the same document: a text header naming the analysis, the
variables and the point count, then the numbers. They differ in three ways that
each silently corrupt a parse if guessed wrong, so all three are detected rather
than assumed:

* **Encoding.** LTspice XVII writes its header as UTF-16LE. ngspice writes
  ASCII. Detected from a NUL byte in the first few bytes.
* **Body format.** ``Values:`` introduces decimal text, ``Binary:`` introduces
  packed floats.
* **Binary width.** ngspice packs every number as float64. LTspice packs the
  independent variable as float64 and the rest as float32 unless the ``double``
  flag is set. Reading one as the other does not fail -- it yields plausible
  garbage -- so the dialect is taken from the ``Command:`` header line, which
  each simulator stamps with its own name.

Complex results (AC analysis) carry ``complex`` in ``Flags``, two numbers per
value in both formats.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import RawParseError

__all__ = ["RawPlot", "parse_rawfile"]


@dataclass(frozen=True)
class RawPlot:
    """One analysis' worth of vectors, exactly as the simulator wrote them."""

    title: str
    plotname: str
    flags: tuple[str, ...]
    #: Variable name in file order, e.g. ``("time", "v(out)")``.
    variables: tuple[str, ...]
    #: ``{variable: values}``; values are ``complex`` when :attr:`complex_data`.
    data: dict[str, tuple[float, ...] | tuple[complex, ...]]
    command: str = ""

    @property
    def complex_data(self) -> bool:
        return "complex" in self.flags

    @property
    def n_points(self) -> int:
        return len(self.data[self.variables[0]]) if self.variables else 0


def _decode_header(blob: bytes) -> tuple[str, int, bool]:
    """Return ``(header_text, body_offset, is_utf16)``.

    ``body_offset`` is a byte offset into ``blob`` just past the ``Values:`` or
    ``Binary:`` marker line.
    """
    utf16 = len(blob) > 1 and blob[1:2] == b"\x00"
    encoding = "utf-16-le" if utf16 else "latin-1"

    for marker in (b"Binary:", b"Values:"):
        needle = marker.decode("ascii").encode(encoding)
        index = blob.find(needle)
        if index == -1:
            continue
        header_bytes = blob[:index]
        rest = blob[index + len(needle) :]
        # Skip the newline that ends the marker line, in whichever encoding.
        newline = "\n".encode(encoding)
        nl_index = rest.find(newline)
        if nl_index == -1:
            raise RawParseError(f"rawfile ends immediately after {marker!r}")
        body_offset = index + len(needle) + nl_index + len(newline)
        header = header_bytes.decode(encoding, errors="replace")
        header += marker.decode("ascii")
        return header, body_offset, utf16

    raise RawParseError(
        "rawfile has neither a 'Values:' nor a 'Binary:' section; "
        "the simulator did not write results"
    )


def _parse_header_fields(header: str) -> tuple[dict[str, str], list[str]]:
    """Split the header into ``Key: value`` fields and the variable table."""
    fields: dict[str, str] = {}
    variables: list[str] = []
    in_variables = False

    for line in header.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("Values:", "Binary:"):
            break
        if stripped.startswith("Variables:"):
            in_variables = True
            continue
        if in_variables:
            # "<index>\t<name>\t<type>[\t...]" -- name is the second column.
            columns = stripped.split()
            if len(columns) < 2:
                raise RawParseError(f"malformed variable line: {line!r}")
            variables.append(columns[1])
            continue
        key, _, value = stripped.partition(":")
        if not _:
            continue
        fields[key.strip().lower()] = value.strip()

    if not variables:
        raise RawParseError("rawfile header declares no variables")
    return fields, variables


def _parse_ascii_values(
    body: str, variables: list[str], n_points: int, is_complex: bool
) -> dict[str, list]:
    """Read the decimal body.

    Each point is an index followed by one number per variable, so the data is
    scanned as a flat token stream and the leading index of each row dropped.
    Doing it by whitespace rather than by line survives both simulators' habits
    around blank lines and continuation indenting.
    """
    columns: dict[str, list] = {name: [] for name in variables}
    tokens = body.split()
    per_point = len(variables) + 1  # the row index, then one value each

    if len(tokens) < per_point * n_points:
        raise RawParseError(
            f"rawfile declares {n_points} points of {len(variables)} variables "
            f"({per_point * n_points} numbers) but the body has {len(tokens)}"
        )

    for point in range(n_points):
        base = point * per_point
        for offset, name in enumerate(variables, start=1):
            token = tokens[base + offset]
            try:
                if is_complex:
                    real, _, imag = token.partition(",")
                    columns[name].append(complex(float(real), float(imag)))
                else:
                    columns[name].append(float(token))
            except ValueError as exc:
                raise RawParseError(
                    f"point {point}, variable {name!r}: cannot read {token!r} "
                    f"as a number"
                ) from exc
    return columns


def _parse_binary_values(
    body: bytes,
    variables: list[str],
    n_points: int,
    is_complex: bool,
    *,
    narrow_dependents: bool,
) -> dict[str, list]:
    """Read the packed body.

    ``narrow_dependents`` is LTspice's layout: float64 for the independent
    variable, float32 for the rest.
    """
    columns: dict[str, list] = {name: [] for name in variables}

    if is_complex:
        stride = 16 * len(variables)
        needed = stride * n_points
        if len(body) < needed:
            raise RawParseError(
                f"binary rawfile: need {needed} bytes for {n_points} complex "
                f"points, have {len(body)}"
            )
        for point in range(n_points):
            base = point * stride
            for index, name in enumerate(variables):
                real, imag = struct.unpack_from("<dd", body, base + index * 16)
                columns[name].append(complex(real, imag))
        return columns

    widths = [8] + [4 if narrow_dependents else 8] * (len(variables) - 1)
    stride = sum(widths)
    needed = stride * n_points
    if len(body) < needed:
        raise RawParseError(
            f"binary rawfile: need {needed} bytes for {n_points} points, "
            f"have {len(body)}"
        )
    offsets: list[int] = []
    running = 0
    for width in widths:
        offsets.append(running)
        running += width

    for point in range(n_points):
        base = point * stride
        for index, name in enumerate(variables):
            fmt = "<d" if widths[index] == 8 else "<f"
            (value,) = struct.unpack_from(fmt, body, base + offsets[index])
            columns[name].append(value)
    return columns


def parse_rawfile(blob: bytes) -> RawPlot:
    """Parse one rawfile. Raises :class:`RawParseError` on anything unreadable.

    Only the *last* plot in a multi-plot file is returned, matching what a caller
    means by "the result" when a deck runs one analysis: ngspice prepends the
    operating point to some runs, and the analysis asked for is the one that
    comes last.
    """
    if not blob.strip():
        raise RawParseError(
            "rawfile is empty: the simulator produced no data at all"
        )

    header, body_offset, utf16 = _decode_header(blob)
    fields, variables = _parse_header_fields(header)

    try:
        n_points = int(fields.get("no. points", "").split()[0])
    except (ValueError, IndexError) as exc:
        raise RawParseError(
            f"rawfile header has no readable 'No. Points' "
            f"({fields.get('no. points')!r})"
        ) from exc

    declared_vars = fields.get("no. variables")
    if declared_vars:
        try:
            expected = int(declared_vars.split()[0])
        except ValueError:
            expected = len(variables)
        if expected != len(variables):
            raise RawParseError(
                f"rawfile declares {expected} variables but lists {len(variables)}"
            )

    flags = tuple(fields.get("flags", "").lower().split())
    is_complex = "complex" in flags
    command = fields.get("command", "")

    binary = b"Binary:" in blob[: body_offset + 16] or (
        "Binary:" in header
    )

    if binary:
        # ngspice packs everything as float64; LTspice narrows the dependent
        # variables to float32 unless it says otherwise.
        ltspice = "ltspice" in command.lower() or utf16
        narrow = ltspice and "double" not in flags
        columns = _parse_binary_values(
            blob[body_offset:],
            variables,
            n_points,
            is_complex,
            narrow_dependents=narrow,
        )
    else:
        encoding = "utf-16-le" if utf16 else "latin-1"
        columns = _parse_ascii_values(
            blob[body_offset:].decode(encoding, errors="replace"),
            variables,
            n_points,
            is_complex,
        )

    return RawPlot(
        title=fields.get("title", ""),
        plotname=fields.get("plotname", ""),
        flags=flags,
        variables=tuple(variables),
        data={name: tuple(values) for name, values in columns.items()},
        command=command,
    )
