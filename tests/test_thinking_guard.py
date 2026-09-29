"""Bounding a reply's thinking: a length of its own, and a loop that makes the rest pointless."""

from localagent.models.thinking import CHARS_PER_TOKEN, ThinkingGuard, looping

PASSAGE = ("Looking at the project layout and the failing behaviour, the next thing to check is how the "
           "values flow through the helper, and whether the helper is even reached. ")
PROSE = ("The task asks for a voxel scene. First list the files, then read the entry point, then decide "
         "whether Three.js is already a dependency. After that, plan the geometry, the water shader and "
         "the particle spray, and finally write the scene and open it in a browser to check the result. ")


def varied(count: int = 300) -> list[str]:
    """Thinking that keeps moving: different files, numbers and conclusions every time."""
    return [f"Checking module_{i}.py at line {i}: it touches buffer {i} before index {i * 3} is built, "
            f"which explains the delay of {i * 5} ms reported for case {i * 7}. " for i in range(1, count + 1)]


def test_a_repeated_passage_is_a_loop_and_ordinary_thinking_is_not():
    assert looping(PASSAGE * 12, window=100, span=40_000, times=2)
    assert not looping(PROSE * 2 + PASSAGE, window=100, span=40_000, times=2)
    assert not looping("", window=100, span=40_000, times=2)


def test_the_repeat_need_not_line_up_with_the_window():
    """The tail moves with the loop, so any period is caught, not just multiples of the window."""
    assert looping(("abcdefghij" * 37 + "|") * 5, window=120, span=40_000, times=2)


def test_the_guard_waits_for_enough_text_then_says_what_happened():
    guard = ThinkingGuard(start=2_000, step=200, window=100, times=2)
    stopped = False
    fed = 0
    while not stopped and fed < 200:
        stopped = guard.feed(PASSAGE)
        fed += 1
    assert stopped and 2_000 <= fed * len(PASSAGE) < 6_000
    assert guard.reason == "circles" and "100-character passage" in guard.detail


def test_long_but_varied_thinking_is_left_alone():
    """Thinking that keeps moving must run to its end; only a bound of its own stops it."""
    guard = ThinkingGuard(start=2_000, step=200, window=100, times=2)
    assert not any(guard.feed(thought) for thought in varied())


def tokens(text: str, size: int) -> list[str]:
    """*text* as a server streams it, one token of *size* characters per chunk."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def test_thinking_stops_at_its_own_bound_even_when_it_never_repeats():
    """The output budget has to hold a whole file; the thinking bound is what keeps a step short."""
    guard = ThinkingGuard(1_000, start=2_000, step=200, window=100, times=2)
    chunks = tokens("".join(varied()), 3)
    stopped = next(i for i, chunk in enumerate(chunks, start=1) if guard.feed(chunk))
    assert stopped == guard.tokens == 1_000
    assert guard.reason == "overlong" and "limit of 1,000 tokens" in guard.detail


def test_the_bound_counts_tokens_whatever_the_language():
    """Chinese thinking is 1.15 characters a token (measured on Qwen3.8); a character bound let it think 3x longer."""
    guard = ThinkingGuard(1_000)
    chinese = "先看项目结构，再读入口文件，确认依赖之后规划几何、水面着色器和粒子效果，最后写场景并在浏览器里检查。" * 40
    stopped = next(i for i, chunk in enumerate(chinese, start=1) if guard.feed(chunk))
    assert stopped == 1_000 and guard.reason == "overlong"


def test_a_server_that_packs_many_tokens_into_a_chunk_is_counted_by_its_text():
    """A cloud API may send a sentence per chunk; counting chunks alone would barely bound it."""
    guard = ThinkingGuard(1_000, start=2_000, step=200, window=100, times=2)
    thoughts = varied()
    stopped = next(i for i, thought in enumerate(thoughts, start=1) if guard.feed(thought))
    fed = sum(len(t) for t in thoughts[:stopped])
    bound = 1_000 * CHARS_PER_TOKEN
    assert stopped < 1_000 and bound <= fed < bound + len(thoughts[0])
    assert guard.tokens == fed // CHARS_PER_TOKEN


def test_no_bound_means_only_the_loop_stops_the_thinking():
    guard = ThinkingGuard(0, start=2_000, step=200, window=100, times=2)
    assert not any(guard.feed(thought) for thought in varied(2_000))


def test_a_reset_guard_forgets_the_attempt_that_failed():
    """A retried request is a new reply; text from the dropped one must not count as a repeat."""
    guard = ThinkingGuard(start=1_000, step=200, window=100, times=2)
    for _ in range(20):
        guard.feed(PASSAGE)
    guard.reset()
    assert not guard.feed(PASSAGE) and guard.reason == "" and guard.detail == ""
    assert guard.tokens == len(PASSAGE) // CHARS_PER_TOKEN
