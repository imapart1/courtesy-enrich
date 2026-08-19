from enrich.patterns import (
    apply_pattern,
    candidate_locals,
    classify_shape,
    detect_pattern,
    learn_domain_shape,
    split_name,
)


def test_apply_pattern():
    assert apply_pattern("{first}.{last}", "Mike", "Sutterer") == "mike.sutterer"
    assert apply_pattern("{f}{last}", "Sarah", "Rossi") == "srossi"
    assert apply_pattern("{first}", "Vahe", "") == "vahe"
    assert apply_pattern("{f}.{last}", "Anna", "Freeman") == "a.freeman"
    assert apply_pattern("{first}.{last}", "Solo", "") is None  # needs a last name


def test_norm_handles_accents_and_hyphens():
    assert apply_pattern("{first}.{last}", "José-María", "Núñez") == "josemaria.nunez"
    assert split_name("Bridget Jean-Baptiste") == ("bridget", "jeanbaptiste")


def test_detect_pattern():
    assert detect_pattern("mike.sutterer", "Mike", "Sutterer") == "{first}.{last}"
    assert detect_pattern("srossi", "Sarah", "Rossi") == "{f}{last}"
    assert detect_pattern("vahe", "Vahe", "Hayrapetian") == "{first}"
    assert detect_pattern("nobody", "Mike", "Sutterer") is None


def test_classify_shape():
    assert classify_shape("mike.sutterer") == "first.last"
    assert classify_shape("a.freeman") == "f.last"
    assert classify_shape("vahe") == "single_short"
    assert classify_shape("privacypolicy") == "generic"
    assert classify_shape("michaelangelo") == "single_long"


def test_learn_domain_shape_from_sheet_style_rows():
    # blissy.com style: first names only
    shape, conf, n = learn_domain_shape(["vahe", "edgar", "brooks", "gabrielle", "forest"])
    assert shape in ("single_short", "single_long") and n == 5 and conf >= 0.6
    # bonnieplants.com style: dotted
    shape, conf, n = learn_domain_shape(["mike.sutterer", "al.cheatham", "stan.cope"])
    assert shape == "first.last" and conf == 1.0


def test_candidate_locals_ordering():
    locs = candidate_locals("Jane", "Smith", pattern="{f}{last}", limit=3)
    assert locs[0] == "jsmith"
    assert len(locs) == len(set(locs)) == 3
    # shape-informed
    locs = candidate_locals("Jane", "Smith", shape="first.last", limit=2)
    assert locs[0] == "jane.smith"
