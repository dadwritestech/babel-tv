from babel_tv.asr import RepeatGuard, is_hallucination


def test_repeat_guard_drops_loops_but_not_new_lines():
    g = RepeatGuard()
    assert not g.is_repeat("...la relación.")
    assert g.is_repeat("la relación")
    assert g.is_repeat("...la relación...")
    assert not g.is_repeat("Fue un desastre.")


def test_repeat_guard_forgets_after_window():
    g = RepeatGuard(window=2)
    for t in ["a b", "c d", "e f"]:
        assert not g.is_repeat(t)
    assert not g.is_repeat("a b")


def test_hallucination_filter():
    assert is_hallucination("ご視聴ありがとうございました")
    assert is_hallucination("Thank you for watching")
    assert not is_hallucination("बीजेपी हार गई")
