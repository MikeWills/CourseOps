"""KML / KMZ parsing.

Organizer files are messy in predictable ways, and this module is written around
those defects rather than around the spec:

- KMZ is a zip; the KML inside is usually but not always named `doc.kml`.
- Namespaces vary (kml/2.2, kml/2.0, sometimes absent). We match on local tag
  names and ignore namespaces entirely.
- Placemarks nest arbitrarily deep in Document/Folder trees. The folder path is
  worth keeping — it is often the only clue to what a feature is, when the
  placemark itself is named "Untitled Path".
- A single Placemark may hold a MultiGeometry of several LineStrings.
- Coordinates are `lon,lat[,alt]` separated by any whitespace, frequently with
  newlines and indentation inside the text node.

These files come from the race organizer and are uploaded through the web UI, so
they are untrusted third-party input. XML is parsed with defusedxml to block
entity-expansion attacks, KMZ archives are checked against decompression bombs,
and both the archive and the KML payload are size-capped.

Nothing here writes to the database and nothing guesses what a feature *is*.
Classification is a suggestion only; a human confirms it in the review step.
"""

from __future__ import annotations

import re
import zipfile
from html import unescape
from dataclasses import dataclass, field
from pathlib import Path
from defusedxml import ElementTree as SafeElementTree
from xml.etree import ElementTree

from .geo import LonLat, line_length_m

# Placemark names that carry no information. Seen constantly in exports from
# Google Earth, Garmin, Strava and course-mapping tools.
_MEANINGLESS_NAMES = {
    "", "untitled path", "untitled placemark", "untitled polygon", "untitled",
    "path", "line", "route", "placemark", "point", "new path", "no name",
}

_AID_HINTS = re.compile(
    r"\b(aid|water|hydrat|fluid|station|stop|refresh|gatorade)\b", re.I
)
_START_HINTS = re.compile(r"\b(start|begin)\b", re.I)
_FINISH_HINTS = re.compile(r"\b(finish|end)\b", re.I)
_MEDICAL_HINTS = re.compile(r"\b(medical|med|first aid|ems|ambulance)\b", re.I)
_PARKING_HINTS = re.compile(r"\b(parking|lot|garage)\b", re.I)
_COURSE_HINTS = re.compile(
    r"\b(full|half|marathon|10k|5k|course|route|relay|kids|fun run)\b", re.I
)


# A real marathon course KML is well under a megabyte; a KMZ with imagery might
# reach a few. These caps are generous for legitimate files and still bound the
# work a hostile one can cause.
MAX_KML_BYTES = 64 * 1024 * 1024
MAX_KMZ_BYTES = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


class KmlError(Exception):
    """The file could not be read as KML or KMZ."""


@dataclass
class KmlFeature:
    """One geometry pulled from a KML file, unclassified."""

    name: str
    folder: str                 # slash-joined Document/Folder path
    geom_type: str              # linestring | point | polygon
    coords: list[LonLat]
    description: str | None = None
    # The <styleUrl> reference, e.g. '#start_marker'. Kept because exporters
    # routinely give several placemarks the SAME name and distinguish them only
    # by style — MapMyRun names both the start and the finish after the route.
    # Without this they are indistinguishable in the review list.
    style_id: str | None = None
    # The exporter's attribute table, when the description carries one. ArcGIS
    # renders it as HTML rather than using ExtendedData, and for some files it
    # is the ONLY thing distinguishing a water stop from a mile marker - every
    # placemark being named after its race instead.
    attributes: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def point(self) -> LonLat | None:
        return self.coords[0] if self.geom_type == "point" and self.coords else None

    @property
    def length_m(self) -> float:
        return line_length_m(self.coords) if len(self.coords) >= 2 else 0.0

    @property
    def has_useful_name(self) -> bool:
        return self.name.strip().lower() not in _MEANINGLESS_NAMES

    @property
    def attribute_label(self) -> str | None:
        """A label built from the exporter's own attribute table, if it has one.

        A real file needed this: every one of 78 placemarks was named after its
        race - "10K", "ALL", "FULL" - so the review list read as 78 rows of the
        same three words. Type and NUM turn that into "MM 12" and "WATER",
        which is the difference between a reviewable list and a wall.
        """
        kind = (self.attributes.get("Type") or "").strip()
        if not kind:
            return None
        number = (self.attributes.get("NUM") or "").strip()
        return f"{kind} {number}".strip()

    @property
    def label(self) -> str:
        """Best available human label, falling back to the folder path."""
        from_attributes = self.attribute_label
        if from_attributes:
            # The name is usually the race here, which is worth keeping as
            # context but is useless on its own.
            race = (self.attributes.get("Race") or "").strip()
            return f"{from_attributes} ({race})" if race else from_attributes
        if self.has_useful_name:
            return self.name.strip()
        if self.folder:
            return f"{self.folder} (unnamed)"
        return "(unnamed)"

    def suggest(self) -> str:
        """A guess at what this is. Advisory only — a human confirms it.

        Deliberately conservative: it is better to leave something unassigned
        and have the operator classify it than to silently file a parking lot
        as an aid station.
        """
        # An exporter's own attribute table beats any guess we could make from
        # the text, so it wins outright when present. This is not a hint - the
        # file is stating what the thing is.
        kind = (self.attributes.get("Type") or "").strip()
        if kind and self.geom_type == "point":
            return f"poi:{attribute_key(kind)}"

        # style_id is included because it is sometimes the ONLY thing that
        # distinguishes two placemarks (see the field's note). Underscores and
        # hyphens become spaces first: '_' is a word character, so a \b-anchored
        # pattern would never match inside 'start_marker'.
        text = " ".join(
            filter(None, [self.name, self.folder, self.description, self.style_id])
        )
        text = re.sub(r"[_\-]+", " ", text)
        if self.geom_type in {"linestring", "polygon"}:
            return "course" if _COURSE_HINTS.search(text) else "unassigned"
        if _MEDICAL_HINTS.search(text):
            return "poi:medical"
        if _START_HINTS.search(text) and _FINISH_HINTS.search(text):
            return "poi:start_finish"
        if _START_HINTS.search(text):
            return "poi:start"
        if _FINISH_HINTS.search(text):
            return "poi:finish"
        if _PARKING_HINTS.search(text):
            return "poi:parking"
        if _AID_HINTS.search(text):
            return "poi:aid_station"
        return "unassigned"


# GIS exporters (ArcGIS in particular) do not put attributes in ExtendedData.
# They render the whole attribute table into <description> as an HTML table and
# ship an XSL alongside to style it. The real information about a place -
# whether it is a water stop, a mile marker, a first aid post, and which race it
# belongs to - lives in there and nowhere else.
#
# A real file made this worth parsing: 78 placemarks all named after their race
# ("10K", "ALL", "FULL"), whose descriptions carry Type=WATER / MM / FIRST AID /
# Exchange Zone / Start / END. Without reading the table there is nothing to
# tell one pin from another; with it, every point files itself.
#
# Deliberately a narrow regex over the CDATA rather than an HTML parse: the
# markup is machine-generated and uniform, we want four or five known cells from
# it, and adding an HTML parser as a dependency to read a table nobody styles is
# a poor trade. If it does not match, the caller gets an empty dict and falls
# back to the name, which is the behaviour we had before.
_ATTR_ROW = re.compile(
    r"<td[^>]*>\s*([^<>]{1,60}?)\s*</td>\s*<td[^>]*>\s*(.*?)\s*</td>",
    re.S | re.I,
)
_NULLISH = {"", "<null>", "&lt;null&gt;", "null", "none"}


# An exporter's word for something we already have a layer for. Without this,
# a file saying Type=END suggests a layer key "end" while the event has one
# called "finish", and the assignment is refused for naming a layer that does
# not exist. Kept deliberately tiny: only synonyms for layers that ship by
# default, never an attempt to guess at a club's own vocabulary.
_TYPE_ALIASES = {
    "end": "finish",
    "finish_line": "finish",
    "start_line": "start",
}


def attribute_key(value: str) -> str:
    """`FIRST AID` -> `first_aid`. Matches categories.slugify deliberately, so
    a layer created from a file's own attribute lines up with the layer key a
    club would get by typing the same words into the setup screen."""
    key = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")[:40]
    return _TYPE_ALIASES.get(key, key)


def attributes_from_description(description: str | None) -> dict[str, str]:
    """Pull an exporter's attribute table out of a description blob.

    Returns {} for anything that is not one, which includes every hand-written
    description and every file from an exporter that uses ExtendedData properly.
    """
    if not description or "<td" not in description.lower():
        return {}

    found: dict[str, str] = {}
    for key, value in _ATTR_ROW.findall(description):
        key = unescape(key).strip()
        value = unescape(re.sub(r"<[^>]+>", "", value)).strip()
        if not key or key in found:
            continue
        if value.lower() in _NULLISH:
            value = ""
        found[key] = value
    return found


_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<\s*(?:br|/p|/div|/tr|/li)\b[^>]*>", re.I)
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")
MAX_NOTES_LENGTH = 500


def description_notes(description: str | None) -> str | None:
    """What a description is worth showing a volunteer, or None.

    An exporter's attribute table is worth nothing here. Everything in it was
    either consumed at import (Type became the layer, Race the course) or is
    noise (SHAPE=Point, NUM=<Null>), and shown raw it is a page of markup in
    a popup somebody opened to find out where the water is. A hand-written
    description - Google My Maps, a club's own file - is kept, with any markup
    stripped, because those say things like "behind the church, use the
    side gate".
    """
    if not description:
        return None
    if attributes_from_description(description):
        return None
    text = _BREAK.sub("\n", description)
    text = _TAG.sub("", text)
    text = unescape(text)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANKS.sub("\n", text).strip()
    return text[:MAX_NOTES_LENGTH] or None


def _local(tag: str) -> str:
    """Strip the namespace: '{http://...}Placemark' -> 'placemark'."""
    return tag.rpartition("}")[2].lower()


def _text(element: ElementTree.Element, child_name: str) -> str | None:
    for child in element:
        if _local(child.tag) == child_name:
            return (child.text or "").strip() or None
    return None


def parse_coordinates(raw: str) -> tuple[list[LonLat], list[str]]:
    """Parse a KML <coordinates> text node into (lon, lat) pairs."""
    coords: list[LonLat] = []
    warnings: list[str] = []
    for token in raw.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            warnings.append(f"Skipped unparseable coordinate {token!r}")
            continue
        if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
            # Almost always lat/lon written in the wrong order.
            warnings.append(
                f"Coordinate {lon},{lat} is out of range - KML is lon,lat; "
                "this file may have them reversed."
            )
            continue
        coords.append((lon, lat))
    return coords, warnings


def _geometries(
    element: ElementTree.Element,
) -> list[tuple[str, ElementTree.Element]]:
    """Find geometry elements under a Placemark, descending into MultiGeometry."""
    found: list[tuple[str, ElementTree.Element]] = []
    for child in element:
        tag = _local(child.tag)
        if tag in {"linestring", "linearring", "point", "polygon"}:
            found.append(("linestring" if tag == "linearring" else tag, child))
        elif tag in {"multigeometry", "track"}:
            found.extend(_geometries(child))
        elif tag in {"outerboundaryis", "innerboundaryis"}:
            found.extend(_geometries(child))
    return found


def _coordinates_of(element: ElementTree.Element) -> str:
    """Collect <coordinates> text, descending through boundary wrappers."""
    chunks = []
    for child in element.iter():
        if _local(child.tag) == "coordinates" and child.text:
            chunks.append(child.text)
    return " ".join(chunks)


def _walk(
    element: ElementTree.Element, folder_path: list[str], out: list[KmlFeature]
) -> None:
    for child in element:
        tag = _local(child.tag)
        if tag in {"document", "folder"}:
            name = _text(child, "name")
            _walk(child, folder_path + ([name] if name else []), out)
        elif tag == "placemark":
            out.extend(_features_from_placemark(child, folder_path))
        else:
            _walk(child, folder_path, out)


def _features_from_placemark(
    placemark: ElementTree.Element, folder_path: list[str]
) -> list[KmlFeature]:
    name = _text(placemark, "name") or ""
    description = _text(placemark, "description")
    style_id = _text(placemark, "styleurl")
    if style_id:
        style_id = style_id.lstrip("#").strip() or None
    folder = " / ".join(folder_path)
    features: list[KmlFeature] = []

    geometries = _geometries(placemark)
    for index, (geom_type, node) in enumerate(geometries):
        coords, warnings = parse_coordinates(_coordinates_of(node))
        if not coords:
            continue
        if geom_type == "point":
            coords = coords[:1]
        elif len(coords) < 2:
            warnings.append("Line had fewer than two points; treated as a point.")
            geom_type = "point"

        # A MultiGeometry yields several features from one placemark; number
        # them so they stay distinguishable in the review list.
        suffix = f" [{index + 1}]" if len(geometries) > 1 else ""
        features.append(
            KmlFeature(
                name=f"{name}{suffix}",
                folder=folder,
                geom_type=geom_type,
                coords=coords,
                description=description,
                style_id=style_id,
                attributes=attributes_from_description(description),
                warnings=warnings,
            )
        )
    return features


def parse_kml_bytes(data: bytes) -> list[KmlFeature]:
    if len(data) > MAX_KML_BYTES:
        raise KmlError(
            f"KML payload is {len(data) / 1e6:.0f} MB, over the "
            f"{MAX_KML_BYTES / 1e6:.0f} MB limit."
        )
    try:
        root = SafeElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise KmlError(f"Not valid XML: {exc}") from exc
    except Exception as exc:
        # defusedxml raises its own types for entity-expansion attempts.
        raise KmlError(f"Rejected XML: {type(exc).__name__}: {exc}") from exc

    features: list[KmlFeature] = []
    _walk(root, [], features)
    if not features and _local(root.tag) == "placemark":
        features = _features_from_placemark(root, [])
    return features


def read_kmz(path: Path) -> bytes:
    """Extract the KML payload from a KMZ archive, guarding against zip bombs.

    defusedxml protects the XML parse but not the unzip that precedes it, so
    the declared uncompressed size is checked before anything is read.
    """
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        preferred = [n for n in names if n.lower() == "doc.kml"]
        candidates = preferred or [n for n in names if n.lower().endswith(".kml")]
        if not candidates:
            raise KmlError("KMZ archive contains no .kml file.")

        info = archive.getinfo(candidates[0])
        if info.file_size > MAX_KML_BYTES:
            raise KmlError(
                f"KML inside the archive expands to {info.file_size / 1e6:.0f} MB, "
                f"over the {MAX_KML_BYTES / 1e6:.0f} MB limit."
            )
        if info.compress_size and (
            info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise KmlError(
                "Archive entry has an implausible compression ratio "
                f"({info.file_size / info.compress_size:.0f}x) and was rejected "
                "as a possible decompression bomb."
            )
        return archive.read(candidates[0])


def load(path: str | Path) -> list[KmlFeature]:
    """Parse a .kml or .kmz file into features. Raises KmlError."""
    file_path = Path(path)
    if not file_path.is_file():
        raise KmlError(f"No such file: {file_path}")

    size = file_path.stat().st_size
    if zipfile.is_zipfile(file_path):
        if size > MAX_KMZ_BYTES:
            raise KmlError(
                f"KMZ is {size / 1e6:.0f} MB, over the "
                f"{MAX_KMZ_BYTES / 1e6:.0f} MB limit."
            )
        data = read_kmz(file_path)
    else:
        if size > MAX_KML_BYTES:
            raise KmlError(
                f"KML is {size / 1e6:.0f} MB, over the "
                f"{MAX_KML_BYTES / 1e6:.0f} MB limit."
            )
        data = file_path.read_bytes()

    features = parse_kml_bytes(data)
    if not features:
        raise KmlError(
            "No placemarks with usable geometry were found. The file may be an "
            "image overlay, a network link to a remote KML, or styling only."
        )
    return features
