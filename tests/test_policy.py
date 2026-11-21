from app.policy import (POWERS, ACTIONS, KINDS, ADMIN_POWERS, LEGACY, VERIFY, plist, pjoin, clean_powers, in_scope, norm, scope_error, meta)


def test_every_default_power_exists():
    for kind, (label, powers) in KINDS.items():
        assert label and set(powers) <= set(POWERS), kind
    assert set(ADMIN_POWERS) <= set(POWERS)
    for role, powers in LEGACY.items():
        assert set(powers) <= set(POWERS), role
    assert {"verify." + k for k in VERIFY} <= set(POWERS)


def test_acting_on_a_record_implies_seeing_it():
    assert "registry.read" in clean_powers(["building.review"])
    assert "registry.read" in clean_powers(["inspection.record"])
    assert "registry.read" not in clean_powers(["verify.etrade"])  # a verification-only office does not get building data
    assert "registry.read" not in clean_powers(["tax.rules"])
    for p in ACTIONS:
        assert "registry.read" in clean_powers([p])


def test_unknown_power_is_refused():
    try:
        clean_powers(["building.review", "everything"])
    except ValueError as e:
        assert "everything" in str(e)
    else:
        raise AssertionError("an unknown power must be refused")


def test_storage_roundtrip_ignores_order_and_duplicates():
    assert pjoin(["b", "a", "a"]) == "a,b"
    assert plist("a,b") == ["a", "b"] and plist("") == [] and plist(None) == []


def test_area_matching():
    assert in_scope("W3", None, "w3 ", "K1")  # capitals and end spaces do not matter
    assert not in_scope("W3", None, "W4", "K1")
    assert in_scope(None, None, "anything", "anything")  # no area = every woreda
    assert in_scope("W3", "K1", "W3", "k1") and not in_scope("W3", "K1", "W3", "K2")
    assert norm("  Bole ") == "bole" and norm(None) == ""


def test_woreda_and_kebele_offices_must_have_an_area():
    assert scope_error("woreda", None, None)
    assert scope_error("woreda", "W3", None) is None
    assert scope_error("kebele", "W3", None) and scope_error("kebele", None, "K1")
    assert scope_error("kebele", "W3", "K1") is None
    assert scope_error("revenue", None, None) is None  # city-wide bodies need no area
    assert scope_error("revenue", None, "K1")  # a kebele name alone is ambiguous


def test_woreda_office_starts_with_what_the_old_officer_role_could_do():
    assert set(KINDS["woreda"][1]) == set(LEGACY["officer"])
    assert "building.review" in KINDS["woreda"][1]
    assert "building.review" not in KINDS["revenue"][1] and "tax.rules" not in KINDS["woreda"][1]


def test_meta_has_everything_the_form_needs():
    m = meta()
    assert set(m["kinds"]) == set(KINDS) and m["powers"] == POWERS
    assert m["kinds"]["woreda"]["label"] == "Woreda office" and "building.review" in m["kinds"]["woreda"]["powers"]
