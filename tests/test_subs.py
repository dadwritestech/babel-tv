import threading

import pytest

from babel_tv.subs import (LINGER_S, MAX_SHOW_S, MIN_SHOW_S, Cue, CueTrack, ass_escape,
                           display_end, srt_time, wrap)


def test_srt_time_format():
    assert srt_time(0) == "00:00:00,000"
    assert srt_time(3723.4567) == "01:02:03,457"
    assert srt_time(-1) == "00:00:00,000"


def test_short_cue_stretched_to_min_reading_time():
    c = Cue(10.0, 10.3, "はい")
    assert display_end(c, None) == pytest.approx(max(10.3 + LINGER_S, 10.0 + MIN_SHOW_S))


def test_long_text_gets_reading_time_capped():
    c = Cue(0.0, 1.0, "x" * 500)
    assert display_end(c, None) == pytest.approx(MAX_SHOW_S)


def test_display_never_overlaps_next_cue():
    c = Cue(0.0, 2.0, "a fairly long subtitle line that needs time")
    assert display_end(c, 2.4) == 2.4


def test_display_end_never_before_start():
    assert display_end(Cue(5.0, 6.0, "x"), 4.0) == 5.0


def test_active_cue_lookup():
    tr = CueTrack()
    tr.add(Cue(1.0, 2.0, "one"))
    tr.add(Cue(5.0, 6.0, "two"))
    assert tr.active(0.5) is None
    assert tr.active(1.0).text == "one"
    assert tr.active(2.0 + LINGER_S - 0.01).text == "one"
    assert tr.active(2.0 + LINGER_S + 0.01) is None
    assert tr.active(5.5).text == "two"


def test_out_of_order_arrival_is_sorted():
    # Translations can finish out of order (a short segment after a long one).
    tr = CueTrack()
    tr.add(Cue(4.0, 5.0, "second"))
    tr.add(Cue(1.0, 3.5, "first"))
    assert [c.text for c in tr.cues()] == ["first", "second"]
    assert tr.active(3.9).text == "first"
    assert tr.active(4.0).text == "second"


def test_empty_text_not_added():
    tr = CueTrack()
    tr.add(Cue(0, 1, "  "))
    assert len(tr) == 0


def test_srt_export():
    tr = CueTrack()
    tr.add(Cue(1.0, 2.0, "నమస్కారం"))
    tr.add(Cue(2.5, 4.0, "వార్తలు"))
    srt = tr.to_srt()
    assert srt.startswith("1\n00:00:01,000 --> 00:00:02,500\nనమస్కారం\n")
    assert "2\n00:00:02,500 --> 00:00:05,500\nవార్తలు" in srt


def test_wrap_limits_lines_and_keeps_newest_words():
    text = " ".join(f"word{i}" for i in range(60))
    out = wrap(text, width=30, max_lines=2)
    assert len(out.split("\n")) <= 2
    assert out.endswith("word59")


def test_ass_escape():
    assert ass_escape("a{b}\nc\\") == "a\\{b\\}\\Nc\\\\"


def test_concurrent_adds():
    tr = CueTrack()
    ts = [threading.Thread(target=lambda k=k: [tr.add(Cue(k * 100 + i, k * 100 + i + 1, "x"))
                                               for i in range(100)]) for k in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    starts = [c.start for c in tr.cues()]
    assert len(starts) == 400 and starts == sorted(starts)
