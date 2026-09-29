"""``la-askpass``: what ssh runs when it needs an answer, if LocalAgent started it.

ssh passes the prompt ("alice@host's password: ", or a new host key's question)
as the only argument and reads the answer from our stdout. With
``SSH_ASKPASS_REQUIRE=force`` it does that even though no terminal is attached,
which is how the desktop app can log in without one. This program only relays:
it asks the app that started ssh, over loopback, and prints what the user typed.

It is a GUI entry point (``pythonw`` on Windows), so no console window flashes
up, and it imports nothing but the standard library, so it starts quickly.
"""

import json
import os
import sys
import urllib.error
import urllib.request

MAX_WAIT_SECONDS = 600  # the user may be fetching a password; ssh itself waits as long as we do


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "ssh is asking for input"
    url = os.environ.get("LA_ASKPASS_URL", "")
    secret = os.environ.get("LA_ASKPASS_SECRET", "")
    if not url.startswith("http://127.0.0.1:") or not secret:
        return 1  # not started by LocalAgent: refuse rather than hang or send an answer anywhere else
    request = urllib.request.Request(
        url, data=json.dumps({"prompt": prompt}).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-LA-Askpass": secret},
    )
    # No proxy handler: an HTTP_PROXY in the environment must never see a password, even for 127.0.0.1.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=MAX_WAIT_SECONDS) as response:
            answer = json.loads(response.read().decode("utf-8")).get("answer")
    except (urllib.error.URLError, OSError, ValueError):
        return 1
    if not isinstance(answer, str):
        return 1  # the user cancelled: ssh treats a failed askpass as "no answer"
    os.write(1, (answer + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
