from courseops import what3words as w3w


def test_normalize_strips_slashes_and_case():
    assert w3w.normalize("///Filled.Count.Soap") == "filled.count.soap"
    assert w3w.normalize("  filled.count.soap  ") == "filled.count.soap"
    assert w3w.normalize("") is None
    assert w3w.normalize(None) is None


def test_plausible_shapes():
    assert w3w.is_plausible("filled.count.soap")
    assert w3w.is_plausible("///filled.count.soap")
    assert not w3w.is_plausible("filled.count")
    assert not w3w.is_plausible("filled.count.soap.extra")
    assert not w3w.is_plausible("34.73,-86.58")
    assert not w3w.is_plausible(None)


def test_display_uses_the_recognizable_prefix():
    assert w3w.format_for_display("filled.count.soap") == "///filled.count.soap"
    assert w3w.format_for_display(None) == "--"
