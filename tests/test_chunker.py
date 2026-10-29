import numpy as np
import pytest

from babel_tv.chunker import FRAME, FRAME_S, Chunker, FrameBuffer


def run(chunker, probs):
    probs = np.asarray(probs, dtype=np.float32)
    frames = np.zeros((len(probs), FRAME), dtype=np.float32)
    frames[:, 0] = np.arange(len(probs))  # tag each frame with its index
    return chunker.feed(frames, probs) + chunker.flush()


def frames_of(seconds):
    return round(seconds / FRAME_S)


def pattern(*parts):
    """pattern((0.0, 1.0), (0.9, 2.0)) -> 1 s of prob 0 then 2 s of prob 0.9."""
    out = []
    for p, secs in parts:
        out += [p] * frames_of(secs)
    return out


def test_silence_only_yields_nothing():
    assert run(Chunker(), pattern((0.05, 5))) == []


def test_single_utterance_closed_by_pause():
    segs = run(Chunker(max_s=10), pattern((0.0, 1), (0.9, 1.5), (0.0, 1)))
    assert len(segs) == 1
    s = segs[0]
    assert s.start == pytest.approx(1.0, abs=FRAME_S)
    assert s.end == pytest.approx(2.5, abs=FRAME_S)
    assert not s.forced


def test_padding_included_in_audio():
    c = Chunker(max_s=10, pad_s=0.15)
    seg = run(c, pattern((0.0, 1), (0.9, 1.0), (0.0, 1)))[0]
    first_frame = int(seg.audio[0])
    assert first_frame == frames_of(1.0) - c.pad
    assert len(seg.audio) == (frames_of(1.0) + 2 * c.pad) * FRAME


def test_two_utterances_split_on_pause():
    segs = run(Chunker(max_s=10), pattern((0.9, 1), (0.0, 0.6), (0.9, 1), (0.0, 1)))
    assert len(segs) == 2
    assert segs[1].start == pytest.approx(1.6, abs=FRAME_S)


def test_short_pause_does_not_split():
    segs = run(Chunker(max_s=10, min_silence_s=0.35), pattern((0.9, 1), (0.0, 0.2), (0.9, 1), (0.0, 1)))
    assert len(segs) == 1
    assert segs[0].duration == pytest.approx(2.2, abs=2 * FRAME_S)


def test_blip_shorter_than_min_speech_dropped():
    assert run(Chunker(min_speech_s=0.3), pattern((0.0, 1), (0.9, 0.1), (0.0, 1))) == []


def test_long_speech_force_cut_at_max():
    c = Chunker(max_s=3.0)
    segs = run(c, pattern((0.9, 10), (0.0, 1)))
    assert all(s.duration <= 3.0 + FRAME_S for s in segs)
    assert [s.forced for s in segs[:-1]] == [True] * (len(segs) - 1)
    # Segments tile the speech without gaps or overlaps.
    for a, b in zip(segs, segs[1:]):
        assert b.start == pytest.approx(a.end, abs=1e-9)
    assert segs[0].start == 0 and segs[-1].end == pytest.approx(10, abs=FRAME_S)


def test_force_cut_prefers_dip_between_words():
    # 2.6 s speech, a 0.1 s dip (too short to close), then more speech.
    probs = pattern((0.9, 2.6), (0.3, 0.1), (0.9, 3), (0.0, 1))
    segs = run(Chunker(max_s=3.0, cut_search_s=0.8), probs)
    assert segs[0].forced
    dip_at = frames_of(2.6) * FRAME_S
    assert dip_at <= segs[0].end <= dip_at + 0.1 + FRAME_S


def test_hysteresis_keeps_speech_through_mid_probs():
    # 0.4 is under threshold 0.5 but over neg threshold 0.35: still speech.
    segs = run(Chunker(max_s=10), pattern((0.9, 0.5), (0.4, 1.0), (0.9, 0.5), (0.0, 1)))
    assert len(segs) == 1 and segs[0].duration == pytest.approx(2.0, abs=FRAME_S)


def test_streaming_in_pieces_matches_one_shot():
    probs = pattern((0.0, 0.5), (0.9, 4), (0.0, 0.5), (0.8, 1), (0.0, 1))
    whole = run(Chunker(), probs)
    c = Chunker()
    frames = np.zeros((len(probs), FRAME), dtype=np.float32)
    pieces = []
    for i in range(0, len(probs), 7):
        pieces += c.feed(frames[i:i + 7], probs[i:i + 7])
    pieces += c.flush()
    assert [(s.start, s.end) for s in pieces] == [(s.start, s.end) for s in whole]


def test_memory_is_bounded_during_silence():
    c = Chunker()
    run(c, pattern((0.0, 60)))
    assert len(c._frames) <= c.pad


def test_frame_buffer_handles_ragged_reads():
    fb = FrameBuffer()
    pcm = (np.arange(FRAME * 3, dtype=np.int16)).tobytes()
    a = fb.push(pcm[:1001])  # odd byte count splits a sample
    b = fb.push(pcm[1001:])
    got = np.concatenate([a.reshape(-1), b.reshape(-1)]) if len(a) else b.reshape(-1)
    assert got.shape == (FRAME * 3,)
    assert np.allclose(got * 32768, np.arange(FRAME * 3))
