"""Reading an exporter's attribute table out of <description>.

ArcGIS does not use ExtendedData. It renders the whole attribute table into
<description> as an HTML blob and ships an XSL to style it, so for those files
the description is the only place that says what a point actually is.

A real organizer's file made this necessary. All 78 of its placemarks are named
after their race - "10K", "ALL", "FULL" - and the thing distinguishing a water
stop from a mile marker lives only in that table.
"""

import pytest

from courseops import kml

ESRI = """<html><body>
<table>
<tr><td>SHAPE</td><td>Point</td></tr>
<tr bgcolor="#D4E4F3"><td>Race</td><td>10K</td></tr>
<tr><td>Type</td><td>WATER</td></tr>
<tr bgcolor="#D4E4F3"><td>NUM</td><td>&lt;Null&gt;</td></tr>
</table></body></html>"""


def _point(description, name="10K"):
    return kml.KmlFeature(
        name=name, folder="Marathon Points", geom_type="point",
        coords=[(-93.99, 44.13)], description=description,
        attributes=kml.attributes_from_description(description),
    )


def test_the_attribute_table_is_read():
    assert kml.attributes_from_description(ESRI) == {
        "SHAPE": "Point", "Race": "10K", "Type": "WATER", "NUM": "",
    }


def test_a_null_cell_becomes_empty_rather_than_the_word_null():
    """Esri writes <Null> for an absent value. Carrying that through would put
    the word "Null" on a map."""
    assert kml.attributes_from_description(ESRI)["NUM"] == ""


@pytest.mark.parametrize("description", [None, "", "Just a sentence.", "<p>hi</p>"])
def test_anything_that_is_not_an_attribute_table_yields_nothing(description):
    """Hand-written descriptions and well-behaved exporters must be unaffected."""
    assert kml.attributes_from_description(description) == {}


def test_the_type_column_decides_the_suggestion():
    """Not a hint - the file is stating what the thing is, so it wins over any
    guess made from the surrounding text."""
    assert _point(ESRI).suggest() == "poi:water"


def test_a_shorthand_is_aliased_onto_a_layer_that_ships_by_default():
    """A file saying END would otherwise suggest a layer key that no event has,
    and the assignment would be refused for naming a layer that does not exist.
    """
    description = ESRI.replace("<td>WATER</td>", "<td>END</td>")
    assert _point(description).suggest() == "poi:finish"


def test_the_label_uses_the_attributes_when_the_name_is_the_race():
    """78 rows all reading "10K" is not a reviewable list."""
    description = ESRI.replace("<td>&lt;Null&gt;</td>", "<td>12</td>")
    assert _point(description).label == "WATER 12 (10K)"


def test_a_real_name_still_wins_when_there_is_no_attribute_table():
    feature = _point(None, name="Aid 3")
    assert feature.label == "Aid 3"


def test_a_line_is_not_classified_by_the_point_attribute():
    """Type belongs to places. A route segment carrying one must still be
    considered as a course, or a course files itself as a pin."""
    feature = kml.KmlFeature(
        name="0", folder="Full_Route", geom_type="linestring",
        coords=[(-93.99, 44.13), (-93.98, 44.14)], description=ESRI,
        attributes=kml.attributes_from_description(ESRI),
    )
    assert not feature.suggest().startswith("poi:")


# --- what a description is worth showing --------------------------------------

def test_an_attribute_table_yields_no_notes():
    """Everything in the table was used at import or is noise, and shown raw it
    is a page of markup in the popup someone opened to find the water."""
    assert kml.description_notes(ESRI) is None


def test_a_full_arcgis_document_yields_no_notes():
    """The real export wraps the table in a whole document - head, inline
    styles, a script - which is what reached the map."""
    doc = ('<html xmlns:fo="http://www.w3.org/1999/XSL/Format"><head>'
           '<META http-equiv="Content-Type" content="text/html"></head>'
           '<body style="margin:0px">' + ESRI +
           '<script type="text/javascript">function changeImage(a, b) {}'
           '</script></body></html>')
    assert kml.description_notes(doc) is None


@pytest.mark.parametrize("description, expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("Corner of Oak and 3rd", "Corner of Oak and 3rd"),
    ("<p>Behind the church.</p><p>Use the side gate.</p>",
     "Behind the church.\nUse the side gate."),
    ("Water &amp; gels<br>Portaloo 50m north", "Water & gels\nPortaloo 50m north"),
    ("<div>  lots   of   space  </div>", "lots of space"),
])
def test_a_hand_written_description_is_kept_as_text(description, expected):
    """Google My Maps and a club's own file say useful things, in markup."""
    assert kml.description_notes(description) == expected


def test_notes_are_capped():
    assert len(kml.description_notes("x" * 2000)) == kml.MAX_NOTES_LENGTH
