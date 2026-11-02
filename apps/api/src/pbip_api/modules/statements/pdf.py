"""A minimal PDF 1.4 writer for text statements, without a third-party dependency.

A statement only needs lines of text and rules on A4 pages, which the PDF format expresses
directly. It uses the standard Type 1 fonts every reader ships (Helvetica, Helvetica-Bold,
Courier), so no font is embedded. Text is encoded as WinAnsi (cp1252); a character outside it
becomes "?" instead of corrupting the file. Courier is monospaced, which lets amount columns be
right-aligned without a font metrics table.
"""

from dataclasses import dataclass, field

A4_WIDTH = 595.0
A4_HEIGHT = 842.0
COURIER_ADVANCE = 0.6  # every Courier glyph is 600/1000 em wide

_FONTS = {"regular": "F1", "bold": "F2", "mono": "F3"}
_BASE_FONTS = (("F1", "Helvetica"), ("F2", "Helvetica-Bold"), ("F3", "Courier"))


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def mono_width(text: str, size: float) -> float:
    return len(text) * COURIER_ADVANCE * size


@dataclass
class _Page:
    ops: list[bytes] = field(default_factory=list)


class PdfDocument:
    """Collects text and rules per page, then serialises with a correct cross-reference table."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.pages: list[_Page] = []

    def add_page(self) -> None:
        self.pages.append(_Page())

    def text(self, x: float, y: float, value: str, size: float = 10, font: str = "regular") -> None:
        if not self.pages:
            self.add_page()
        name = _FONTS[font]
        self.pages[-1].ops.append(
            b"BT /%s %.1f Tf %.2f %.2f Td (%s) Tj ET" % (name.encode(), size, x, y, _escape(value))
        )

    def text_right(self, right: float, y: float, value: str, size: float = 9) -> None:
        """Right-aligned monospaced text ending at ``right``."""
        self.text(right - mono_width(value, size), y, value, size, "mono")

    def rule(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5) -> None:
        if not self.pages:
            self.add_page()
        self.pages[-1].ops.append(b"%.2f w %.2f %.2f m %.2f %.2f l S" % (width, x1, y1, x2, y2))

    def render(self) -> bytes:
        if not self.pages:
            self.add_page()
        objects: list[bytes] = []
        font_ids = {name: 3 + i for i, (name, _) in enumerate(_BASE_FONTS)}
        first_page_id = 3 + len(_BASE_FONTS)
        page_ids = [first_page_id + 2 * i for i in range(len(self.pages))]
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
        objects.append(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(self.pages)))
        for _, base in _BASE_FONTS:
            objects.append(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
                % base.encode()
            )
        resources = b" ".join(b"/%s %d 0 R" % (n.encode(), i) for n, i in font_ids.items())
        for pid, page in zip(page_ids, self.pages, strict=True):
            objects.append(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.0f %.0f] "
                b"/Resources << /Font << %s >> >> /Contents %d 0 R >>"
                % (A4_WIDTH, A4_HEIGHT, resources, pid + 1)
            )
            stream = b"\n".join(page.ops)
            objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        info_id = len(objects) + 1
        objects.append(b"<< /Title (%s) /Producer (PBIP) >>" % _escape(self.title))
        return _serialise(objects, info_id)


def _serialise(objects: list[bytes], info_id: int) -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        info_id,
        xref_at,
    )
    return bytes(out)
