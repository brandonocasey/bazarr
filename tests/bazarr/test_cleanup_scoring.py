import math

import pytest

from subtitles.cleanup.scoring import display_signal, pearson_score


def test_pearson_uses_centered_covariance_and_never_fabricates_scores():
    # Means are 1/2 and 1/4; covariance=1/2, variances=1 and 3/4.
    assert pearson_score([1, 1, 0, 0], [1, 0, 0, 0])[0] == pytest.approx(1 / math.sqrt(3))
    assert pearson_score([1, 1, 0, 0], [0, 0, 1, 1])[0] == -1
    assert pearson_score([1, 1, 0, 0], [1, 1, 0, 0])[0] == 1
    for candidate in ([0, 0, 0, 0], [1, 1, 1, 1], [0], [math.nan] * 4):
        score, error = pearson_score([1, 1, 0, 0], candidate)
        assert score is None
        assert error


def test_display_occupancy_merges_overlaps_clips_and_keeps_original_timing():
    assert display_signal([(-1, 0.005), (0.002, 0.015), (0.03, 1)], 0.04) == pytest.approx([1, 0.5, 0, 1])
    assert display_signal([(0.02, 0.03)], 0.04) == pytest.approx([0, 0, 1, 0])
    with pytest.raises(ValueError, match='finite and ordered'):
        display_signal([(0.03, 0.01)], 0.04)
