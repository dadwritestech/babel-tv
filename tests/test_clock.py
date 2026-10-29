import pytest

from babel_tv.chunker import SR
from babel_tv.pipeline import ArrivalClock


def test_wall_at_and_live_offset_with_bursty_hls():
    c = ArrivalClock()
    # Two 6 s HLS segments delivered in bursts at wall 100 and 106.5.
    c.mark(6 * SR, 100.0)
    c.mark(12 * SR, 106.5)
    assert c.wall_at(3.0) == 100.0
    assert c.wall_at(6.5) == 106.5
    assert c.wall_at(20.0) is None
    # A stall-free player must run (wall - media) >= max over bursts.
    assert c.live_offset() == pytest.approx(max(100 - 6, 106.5 - 12))
