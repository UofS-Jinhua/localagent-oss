"""Small statistics helpers."""


def mean(values):
    """Arithmetic mean of a non-empty sequence."""
    if not values:
        raise ValueError("mean() requires at least one value")
    return sum(values) / len(values)


def median(values):
    """Middle value; the average of the two middle values for even-length input."""
    if not values:
        raise ValueError("median() requires at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle] + ordered[middle + 1]) / 2


def moving_average(values, window):
    """Mean of every run of `window` consecutive values."""
    if window <= 0:
        raise ValueError("window must be positive")
    return [mean(values[i:i + window]) for i in range(len(values) - window)]
