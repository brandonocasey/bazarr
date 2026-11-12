import math
from collections.abc import Iterable, Sequence

FRAME_SECONDS = 0.01
SAMPLE_RATE = 16000
VAD_MODE = 2


def display_signal(intervals: Iterable[tuple[float, float]], duration: float) -> list[float]:
    """Return display occupancy fractions for presentation-time bins spanning 10 milliseconds."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Media duration must be positive and finite')
    count = math.ceil(duration / FRAME_SECONDS)
    signal = [0.0] * count
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError('Subtitle timing must be finite and ordered')
        start, end = max(0.0, start), min(duration, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in merged:
        first = int(start / FRAME_SECONDS)
        last = min(count - 1, math.ceil(end / FRAME_SECONDS) - 1)
        for index in range(first, last + 1):
            left = index * FRAME_SECONDS
            right = min(duration, left + FRAME_SECONDS)
            signal[index] = min(1.0, signal[index] + (min(end, right) - max(start, left)) / (right - left))
    return signal


def pearson_score(reference: Sequence[float], candidate: Sequence[float]) -> tuple[float | None, str | None]:
    """Compare signals without shifting timestamps; constant signals have no score."""
    if len(reference) != len(candidate) or len(reference) < 2:
        return None, 'Signals must cover the same presentation timeline with at least two bins'
    if any(not math.isfinite(value) for signal in (reference, candidate) for value in signal):
        return None, 'Signals contain non-finite values'
    ref_mean = math.fsum(reference) / len(reference)
    sub_mean = math.fsum(candidate) / len(candidate)
    ref_variance = math.fsum((value - ref_mean) ** 2 for value in reference)
    sub_variance = math.fsum((value - sub_mean) ** 2 for value in candidate)
    if ref_variance == 0 or sub_variance == 0:
        return None, 'Pearson correlation is undefined for a constant audio or subtitle signal'
    covariance = math.fsum((a - ref_mean) * (b - sub_mean) for a, b in zip(reference, candidate))
    return max(-1.0, min(1.0, covariance / math.sqrt(ref_variance * sub_variance))), None
