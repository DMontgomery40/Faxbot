"""ECM partial pages as a step cost (brief 85 M4, N3): measured before priced.

The loopback (test_sslfax_loopback case w) measured the cost of a second partial page; these tests pin the
arithmetic the predictor is offered (pages.coding.ecm_extra_seconds), so the price of a page follows its block
count, not its octets.
"""
import pytest

from app.pages import coding


def test_a_page_needs_one_partial_page_per_65536_octets_at_256_octet_frames():
    assert coding.ecm_blocks(0) == coding.ecm_blocks(1) == coding.ecm_blocks(65_536) == 1
    assert coding.ecm_blocks(65_537) == 2 and coding.ecm_blocks(131_072) == 2 and coding.ecm_blocks(131_073) == 3
    # 64-octet frames: four times as many edges.
    assert coding.ecm_blocks(16_384, 64) == 1 and coding.ecm_blocks(16_385, 64) == 2
    with pytest.raises(ValueError):
        coding.ecm_blocks(1000, 128)


def test_the_step_is_a_whole_turnaround_whatever_the_overflow():
    under, over, far = 64_965 * 8, 66_256 * 8, 130_000 * 8  # the loopback's two pages, and one far past the edge
    assert coding.ecm_extra_seconds([under]) == 0
    assert coding.ecm_extra_seconds([over]) == coding.ECM_BLOCK_SECONDS == pytest.approx(3.3)
    assert coding.ecm_extra_seconds([far]) == coding.ecm_extra_seconds([over])
    assert coding.ecm_extra_seconds([over, over, under]) == pytest.approx(2 * coding.ECM_BLOCK_SECONDS)
    assert coding.ecm_extra_seconds([under * 3], frame_octets=64) > coding.ecm_extra_seconds([under * 3])
    assert coding.ecm_extra_seconds([over], block_seconds=4.24) == pytest.approx(4.24)
