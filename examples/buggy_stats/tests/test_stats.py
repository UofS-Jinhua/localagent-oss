import pytest

from stats import mean, median, moving_average


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_rejects_empty():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd_length():
    assert median([3, 1, 2]) == 2


def test_median_even_length():
    assert median([4, 1, 3, 2]) == 2.5
    assert median([10, 20]) == 15


def test_moving_average():
    assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]


def test_moving_average_single_full_window():
    assert moving_average([1, 2, 3], 3) == [2.0]
