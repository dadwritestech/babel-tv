from babel_tv.player import CATCHUP_SPEED, catchup_speed, cue_ass


def test_catchup_starts_only_when_well_behind():
    assert catchup_speed(5.5, 5.0, 1.0) == 1.0
    assert catchup_speed(7.0, 5.0, 1.0) == CATCHUP_SPEED


def test_catchup_hysteresis():
    # Once speeding up, keep going until nearly back on target.
    assert catchup_speed(6.0, 5.0, CATCHUP_SPEED) == CATCHUP_SPEED
    assert catchup_speed(5.2, 5.0, CATCHUP_SPEED) == 1.0


def test_cue_ass_has_source_line_above_translation():
    ass = cue_ass("తెలుగు", "हिंदी")
    assert ass.index("हिंदी") < ass.index("తెలుగు")
    assert r"\N" in ass
    assert "हिंदी" not in cue_ass("తెలుగు")
