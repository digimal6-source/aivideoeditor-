"""Font registry: discovery, validation, upload and fallback resolution.

No font binaries are committed to this repository (Indivisible and Rubik are not
ours to redistribute). Fonts are discovered at runtime from ``FONTS_DIR`` and can
be uploaded through the web UI.

When a requested font is missing the renderer does **not** silently substitute
it: it falls back so the render still completes, and it attaches an explicit
warning that the UI shows to the user.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import UnsupportedMediaError, ValidationError
from .settings import Settings
from .storage import sanitize_filename

# Magic numbers for the container formats we accept.
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO")
_MAX_FONT_BYTES = 20 * 1024 * 1024

#: Fonts the product expects by default. Used to produce actionable messages.
EXPECTED_FONTS = {
    "indivisible": "Indivisible",
    "rubik-bold": "Rubik Bold",
}

_FALLBACK_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/gnu-free/FreeSansBold.ttf",
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "font"


@dataclass(frozen=True)
class FontRecord:
    id: str
    family: str
    filename: str
    path: Path
    source: str  # "user" | "fallback"
    extension: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "family": self.family,
            "filename": self.filename,
            "source": self.source,
            "extension": self.extension,
        }


def read_family_name(path: Path) -> str | None:
    """Read the font's internal family name (needed by libass to match a style)."""
    try:
        from fontTools.ttLib import TTFont  # type: ignore
    except Exception:  # pragma: no cover - fontTools missing
        return None
    try:
        font = TTFont(str(path), fontNumber=0, lazy=True)
        try:
            name_table = font["name"]
            # 16 = Typographic Family, 1 = Family
            for name_id in (16, 1):
                record = name_table.getDebugName(name_id)
                if record:
                    return str(record).strip()
        finally:
            font.close()
    except Exception:
        return None
    return None


def validate_font_bytes(head: bytes) -> None:
    if not any(head.startswith(magic) for magic in _FONT_MAGIC):
        raise UnsupportedMediaError(
            "That file does not look like a TrueType or OpenType font (.ttf / .otf)."
        )


class FontRegistry:
    """Scans ``FONTS_DIR`` and resolves font ids to concrete files."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dir = settings.fonts_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- discovery -------------------------------------------------------

    def scan(self) -> list[FontRecord]:
        records: list[FontRecord] = []
        for path in sorted(self.dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in self.settings.allowed_font_extensions:
                continue
            family = read_family_name(path) or path.stem.replace("_", " ").replace("-", " ").title()
            records.append(
                FontRecord(
                    id=slugify(path.stem),
                    family=family,
                    filename=path.name,
                    path=path,
                    source="user",
                    extension=path.suffix.lower(),
                )
            )
        return records

    def list_fonts(self) -> list[dict]:
        installed = self.scan()
        known = {record.id for record in installed}
        payload = [record.to_dict() for record in installed]
        for font_id, label in EXPECTED_FONTS.items():
            if font_id not in known:
                payload.append(
                    {
                        "id": font_id,
                        "family": label,
                        "filename": None,
                        "source": "missing",
                        "extension": None,
                    }
                )
        return payload

    def get(self, font_id: str) -> FontRecord | None:
        if not font_id:
            return None
        wanted = slugify(font_id)
        for record in self.scan():
            if record.id == wanted or slugify(record.family) == wanted:
                return record
        return None

    # -- resolution ------------------------------------------------------

    def resolve(self, font_id: str, *, purpose: str) -> tuple[FontRecord, str | None]:
        """Return ``(font, warning)``.

        ``warning`` is ``None`` when the exact requested font was found. When the
        font is missing we fall back so the render still succeeds, and the caller
        surfaces the warning verbatim in the UI.
        """
        record = self.get(font_id)
        if record is not None:
            return record, None

        label = EXPECTED_FONTS.get(slugify(font_id), font_id or "the selected font")
        fallback = self.fallback()
        if font_id:
            warning = (
                f"{label} font is not installed, so the {purpose} was rendered with "
                f"{fallback.family} instead. Upload {label} (.ttf or .otf) in the Fonts "
                f"section to get the exact styling."
            )
        else:
            warning = (
                f"No {purpose} font selected, so {fallback.family} was used. "
                f"Upload a font file to control the exact styling."
            )
        return fallback, warning

    def fallback(self) -> FontRecord:
        """A guaranteed-present font so rendering never crashes."""
        for candidate in _FALLBACK_CANDIDATES:
            path = Path(candidate)
            if path.is_file():
                return self._as_fallback(path)

        matched = self._fc_match()
        if matched is not None:
            return self._as_fallback(matched)

        installed = self.scan()
        if installed:
            return installed[0]

        raise ValidationError(
            "No usable font was found. Upload a .ttf or .otf font in the Fonts section."
        )

    def _as_fallback(self, path: Path) -> FontRecord:
        family = read_family_name(path) or path.stem
        return FontRecord(
            id=slugify(path.stem),
            family=family,
            filename=path.name,
            path=path,
            source="fallback",
            extension=path.suffix.lower(),
        )

    @staticmethod
    def _fc_match() -> Path | None:
        """Ask fontconfig for any sans-serif font. Argument array, never a shell."""
        binary = shutil.which("fc-match")
        if not binary:
            return None
        try:
            result = subprocess.run(
                [binary, "-f", "%{file}", "sans-serif:bold"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        candidate = Path(result.stdout.strip())
        return candidate if candidate.is_file() else None

    # -- upload ----------------------------------------------------------

    def save_upload(self, stream: BinaryIO, filename: str) -> FontRecord:
        safe = sanitize_filename(filename, default="font.ttf")
        ext = Path(safe).suffix.lower()
        if ext not in self.settings.allowed_font_extensions:
            raise UnsupportedMediaError("Only .ttf and .otf font files are supported.")

        head = stream.read(4)
        validate_font_bytes(head)

        target = self.dir / safe
        written = len(head)
        try:
            with open(target, "wb") as handle:
                handle.write(head)
                while True:
                    chunk = stream.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MAX_FONT_BYTES:
                        raise UnsupportedMediaError("Font files must be smaller than 20 MB.")
                    handle.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        family = read_family_name(target)
        if family is None:
            target.unlink(missing_ok=True)
            raise UnsupportedMediaError(
                "That font file could not be parsed. Please upload a valid .ttf or .otf file."
            )

        return FontRecord(
            id=slugify(target.stem),
            family=family,
            filename=target.name,
            path=target,
            source="user",
            extension=ext,
        )

    def delete(self, font_id: str) -> bool:
        record = self.get(font_id)
        if record is None or record.source != "user":
            return False
        record.path.unlink(missing_ok=True)
        return True

    def fonts_dir_for_libass(self) -> str:
        return str(self.dir)
