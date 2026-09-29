"""Bound the thinking of one reply: a length of its own, and a loop that makes the rest pointless.

A step's output budget has to hold the largest file the model writes in one tool call, which leaves
far more room than thinking should ever need -- the 27B models here have spent 25,000 tokens
deliberating and called nothing. So thinking is bounded separately, in tokens, while it streams.

The server only reports its token count in the chunk after the reply is over, so the stream is counted
as it arrives: one token per chunk of thinking. llama.cpp (and LM Studio, which runs it) sends one chunk
per token, speculative drafting included: measured on Qwen3.8 27B with MTP, 137 chunks for 137 tokens of
English, 229 for 229 of Chinese, 2,986 for 3,000 of code. A server that packs several tokens into a
chunk (a cloud API) is counted at no less than one token per CHARS_PER_TOKEN characters, which no
thinking measured here comes near (English 2.6-4.6 characters per token, Chinese 1.15).

On top of it, a reply that has begun repeating itself is stopped at once, before the bound: the
remaining characters would all say the same thing. Only the thinking stream is watched. Written
content repeats for honest reasons (boilerplate, tables, a file full of similar functions), and
cutting a file in half would be worse than a slow step.
"""

WINDOW = 400  # characters of the tail compared against what came before
SPAN = 40_000  # how far back to look for it
TIMES = 2  # the tail plus one earlier copy: measured loops replay whole passages, not phrases
START = 8_000  # nothing is judged before the model has had room to think
STEP = 1_000  # characters of new text between checks (each check scans SPAN characters)
CHARS_PER_TOKEN = 6  # thinking is counted as at least one token per this many characters


def looping(text: str, *, window: int = WINDOW, span: int = SPAN, times: int = TIMES) -> bool:
    """Whether the end of *text* has already appeared *times* times in its last *span* characters.

    The tail moves with the loop, so the repeated block does not have to align with *window*: whatever
    the period of the repetition, the last *window* characters occur once per period.
    """
    if len(text) < window * times:
        return False
    return text[-span:].count(text[-window:]) >= times


class ThinkingGuard:
    """Feed a reply's thinking as it streams; ``True`` once it must stop.

    One guard belongs to one request. ``limit`` is the model's ``max_thinking_tokens`` (0: no bound of
    its own, only the loop). Feed it one chunk at a time: the chunks are what it counts. Checking for a
    loop costs a scan of *span* characters, so it happens once per *step* characters of new text, not
    per chunk.
    """

    def __init__(self, limit: int = 0, *, start: int = START, step: int = STEP, window: int = WINDOW,
                 span: int = SPAN, times: int = TIMES):
        self.limit = limit
        self.start, self.step, self.window, self.span, self.times = start, step, window, span, times
        self.reason = ""  # "circles" or "overlong", once it has triggered
        self.detail = ""  # what to tell the user, once it has triggered
        self._pieces: list[str] = []
        self._length = 0  # characters
        self._chunks = 0
        self._checked = 0

    @property
    def tokens(self) -> int:
        """How many tokens of thinking have been fed, as far as the stream shows."""
        return max(self._chunks, self._length // CHARS_PER_TOKEN)

    def reset(self) -> None:
        """Forget what was fed: a retried request starts a new reply, not a continuation of the last one."""
        self.reason = self.detail = ""
        self._pieces, self._length, self._chunks, self._checked = [], 0, 0, 0

    def feed(self, piece: str) -> bool:
        """Add the next chunk of thinking; ``True`` means stop the reply here."""
        self._pieces.append(piece)
        self._length += len(piece)
        self._chunks += 1
        if self.limit and self.tokens >= self.limit:
            self.reason = "overlong"
            self.detail = f"it passed its thinking limit of {self.limit:,} tokens without calling a tool"
            return True
        if self._length < max(self.start, self._checked + self.step):
            return False
        self._checked = self._length
        text = "".join(self._pieces)
        self._pieces = [text]  # one string from here on, so later joins are cheap
        if not looping(text, window=self.window, span=self.span, times=self.times):
            return False
        self.reason = "circles"
        self.detail = (f"it wrote out a {self.window}-character passage it had already been through, "
                       f"after {self.tokens:,} tokens of thinking")
        return True
