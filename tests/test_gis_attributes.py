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
