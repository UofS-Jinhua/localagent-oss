"""WebSearch and WebFetch against a real HTTP server that plays the web: pages, a PDF, search services."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import make_pdf
from starlette.testclient import TestClient

from localagent.agent import Agent
from localagent.config import read_secrets, save_secret
from localagent.server import create_app
from localagent.store import Store
from localagent.tools import make_tools
from localagent.tools.base import ToolError
from localagent.tools.web import PAGE_CHARS, WebSettings, html_to_text, make_web_tools, search
from localagent.tools.workspace import Workspace

ARTICLE = """<!doctype html><html><head><title>Speculative decoding &amp; you</title>
<script>var tracking = "do not show";</script><style>body{color:red}</style></head>
<body><nav><a href="/">Home</a> <a href="/docs">Docs</a></nav>
<div class="toolbar">Sign in to star this repository</div>
<main><article>
<h1>Speculative decoding</h1>
<p>A small <b>draft</b> model proposes tokens;   the target model
verifies them in one batch. See <a href="/paper.pdf">the paper</a> and <a href="#fig">figure 1</a>.</p>
<pre>llama-server -md draft.gguf
  --draft-max 16</pre>
<ul><li>acceptance rate</li><li>draft length</li></ul>
<table><tr><th>draft</th><th>speed-up</th></tr><tr><td>0.5B</td><td>2.1x</td></tr></table>
<p>Padding so the article counts as the page's real content, not a stray fragment of it.</p>
</article></main>
<footer>Copyright and cookie notice</footer></body></html>"""

DDG_LITE = """<html><body><table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fspec&amp;rut=1" class='result-link'>Speculative <b>decoding</b> guide</a></td></tr>
<tr><td class='result-snippet'>How a <b>draft</b> model speeds up
 generation.</td></tr>
<tr><td><a rel="nofollow" href="https://duckduckgo.com/y.js?ad_domain=ads.example" class='result-link'>Buy GPUs</a></td></tr>
<tr><td class='result-snippet'>Sponsored</td></tr>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Farxiv.org%2Fabs%2F2211.17192" class='result-link'>Fast Inference from Transformers</a></td></tr>
<tr><td class='result-snippet'>Leviathan et al.</td></tr>
</table></body></html>"""


class TheWeb:
    def __init__(self):
        self.hits: dict[str, int] = {}
        self.requests: list[tuple[str, str, dict, bytes]] = []
        web = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body, mime, headers=None):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method):
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                web.hits[path] = web.hits.get(path, 0) + 1
                web.requests.append((method, self.path, dict(self.headers), body))
                query = parse_qs(urlsplit(self.path).query)
                if path == "/article":
                    self.reply(200, ARTICLE, "text/html; charset=utf-8")
                elif path == "/moved":
                    self.reply(302, "", "text/plain", {"Location": "/article"})
                elif path == "/gbk":
                    page = '<html><head><meta charset="gbk"><title>投机解码</title></head><body><p>草稿模型先猜，目标模型一次验证。</p></body></html>'
                    self.reply(200, page.encode("gbk"), "text/html")
                elif path == "/long.txt":
                    self.reply(200, "".join(f"line {n:05d}\n" for n in range(2500)), "text/plain")
                elif path == "/paper.pdf":
                    self.reply(200, make_pdf(["Speculative decoding", "Results table"]), "application/pdf")
                elif path == "/logo.png":
                    self.reply(200, b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "image/png")
                elif path == "/lite/":
                    self.reply(200, DDG_LITE, "text/html")
                elif path == "/blocked/":
                    self.reply(202, "<html>Unfortunately, bots use DuckDuckGo too. anomaly</html>", "text/html")
                elif path == "/searx/search":
                    if query.get("format") != ["json"]:
                        return self.reply(403, "", "text/plain")
                    self.reply(200, json.dumps({"results": [{"title": "SearXNG hit", "url": "https://example.org/s",
                                                             "content": "from searx"}]}), "application/json")
                elif path == "/brave":
                    if self.headers.get("X-Subscription-Token") != "brave-key":
                        return self.reply(401, '{"error":"bad key"}', "application/json")
                    self.reply(200, json.dumps({"web": {"results": [
                        {"title": "Brave <strong>hit</strong>", "url": "https://example.org/b",
                         "description": "a <strong>draft</strong> model"}]}}), "application/json")
                elif path == "/tavily":
                    if self.headers.get("Authorization") != "Bearer tavily-key":
                        return self.reply(401, "", "application/json")
                    asked = json.loads(body)
                    self.reply(200, json.dumps({"results": [{"title": f"Tavily: {asked['query']}",
                                                             "url": "https://example.org/t", "content": "t"}]}),
                               "application/json")
                else:
                    self.reply(404, "not here", "text/plain")

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def web():
    the_web = TheWeb()
    yield the_web
    the_web.close()


def tools(settings):
    return {t.name: t for t in make_web_tools(settings)}


def test_a_page_comes_back_as_its_readable_text_with_links(web):
    fetch = tools(WebSettings())["WebFetch"]

    text = fetch.call({"url": f"{web.url}/moved"})

    assert text.startswith(f"Speculative decoding & you\n{web.url}/article · ")  # the address it ended up at
    assert "# Speculative decoding" in text
    assert "A small draft model proposes tokens; the target model verifies them in one batch." in text
    assert f"[the paper]({web.url}/paper.pdf)" in text  # followable, made absolute
    assert "figure 1" in text and "(#fig)" not in text  # a jump within the page is no link to follow
    assert "```\nllama-server -md draft.gguf\n  --draft-max 16\n```" in text  # code keeps its layout
    assert "- acceptance rate" in text and "| 0.5B | 2.1x" in text
    for noise in ("tracking", "color:red", "Home", "Sign in to star", "cookie notice"):
        assert noise not in text


def test_a_page_without_a_main_part_keeps_everything_but_the_chrome():
    title, text = html_to_text("<title>t</title><body><p>" + "word " * 60 + "</p><footer>legal</footer>")
    assert title == "t" and text.startswith("word word") and "legal" not in text


def test_a_page_in_gbk_is_read_by_its_meta_charset(web):
    text = tools(WebSettings())["WebFetch"].call({"url": f"{web.url}/gbk"})
    assert text.startswith("投机解码\n") and "草稿模型先猜，目标模型一次验证。" in text


def test_a_long_page_is_read_in_parts_and_downloaded_once(web):
    fetch = tools(WebSettings())["WebFetch"]

    first = fetch.call({"url": f"{web.url}/long.txt"})
    assert "line 00000" in first and f"showing 0–{PAGE_CHARS:,}" in first
    assert f"WebFetch(url, start={PAGE_CHARS})" in first
    rest = fetch.call({"url": f"{web.url}/long.txt", "start": PAGE_CHARS * 2})
    assert "line 02499" in rest and "more characters" not in rest
    assert web.hits["/long.txt"] == 1
    with pytest.raises(ToolError, match="past the end"):
        fetch.call({"url": f"{web.url}/long.txt", "start": 10**6})


def test_a_pdf_is_read_as_its_text(web):
    text = tools(WebSettings())["WebFetch"].call({"url": f"{web.url}/paper.pdf"})
    assert text.startswith("PDF, 2 pages\n")
    assert "--- page 1 ---\nSpeculative decoding" in text and "Results table" in text


def test_what_cannot_be_read_says_why(web):
    fetch = tools(WebSettings())["WebFetch"]
    with pytest.raises(ToolError, match="image/png"):
        fetch.call({"url": f"{web.url}/logo.png"})
    with pytest.raises(ToolError, match="HTTP 404"):
        fetch.call({"url": f"{web.url}/nothing"})
    with pytest.raises(ToolError, match="http"):
        fetch.call({"url": "file:///etc/passwd"})


def test_duckduckgo_results_lead_to_the_real_addresses_without_ads(web):
    settings = WebSettings(url=f"{web.url}/lite/")

    text = tools(settings)["WebSearch"].call({"query": "speculative decoding"})

    assert text == ("1. Speculative decoding guide\n   https://example.org/spec\n   How a draft model speeds up generation.\n"
                    "2. Fast Inference from Transformers\n   https://arxiv.org/abs/2211.17192\n   Leviathan et al.")
    method, _, _, body = web.requests[-1]
    assert method == "POST" and parse_qs(body.decode()) == {"q": ["speculative decoding"]}


def test_when_duckduckgo_takes_the_machine_for_a_bot_the_error_says_what_to_do(web):
    with pytest.raises(ToolError, match="Choose another search service"):
        search(WebSettings(url=f"{web.url}/blocked/"), "anything")


def test_searxng_brave_and_tavily(web, isolated_home):
    [hit] = search(WebSettings(search="searxng", url=f"{web.url}/searx"), "q")
    assert (hit.title, hit.url, hit.snippet) == ("SearXNG hit", "https://example.org/s", "from searx")

    brave = WebSettings(search="brave", url=f"{web.url}/brave")
    with pytest.raises(ToolError, match="BRAVE_API_KEY"):
        search(brave, "q")
    save_secret("BRAVE_API_KEY", "brave-key")
    [hit] = search(brave, "q")
    assert (hit.title, hit.snippet) == ("Brave hit", "a draft model")  # its markup is gone

    save_secret("TAVILY_API_KEY", "wrong")
    with pytest.raises(ToolError, match="HTTP 401"):
        search(WebSettings(search="tavily", url=f"{web.url}/tavily"), "q")
    save_secret("MY_TAVILY", "tavily-key")
    [hit] = search(WebSettings(search="tavily", url=f"{web.url}/tavily", api_key_env="MY_TAVILY"), "draft models")
    assert hit.title == "Tavily: draft models"


def test_the_settings_decide_which_tools_exist(isolated_home, registry, project):
    agent = Agent(registry, project)
    assert {"WebSearch", "WebFetch"} <= set(agent.tools)  # on by default, no key needed
    agent.close()

    WebSettings(search="off").save()
    agent = Agent(registry, project)
    assert "WebFetch" in agent.tools and "WebSearch" not in agent.tools
    agent.close()

    WebSettings(enabled=False).save()
    agent = Agent(registry, project)
    assert "WebFetch" not in agent.tools and "WebSearch" not in agent.tools
    agent.close()

    WebSettings(search="brave").save()
    agent = Agent(registry, project)
    assert "BRAVE_API_KEY" in agent.workspace.hidden_env  # commands the agent runs cannot read the search key
    agent.close()

    read_only = [t.name for t in make_tools(Workspace(project), read_only=True, web=WebSettings())]
    assert read_only == ["Read", "RunTests", "Grep", "Glob", "WebSearch", "WebFetch"]  # they change nothing


def test_a_task_reads_a_page_and_the_prompt_says_to_look_things_up(isolated_home, llm, registry, project, web):
    llm.add_tool_calls([("WebFetch", {"url": f"{web.url}/article"})])
    llm.add_tool_calls([("finish", {"success": True, "summary": "The target model verifies drafts in one batch."})])
    agent = Agent(registry, project)

    assert agent.run("how does speculative decoding verify drafts?").success

    system = llm.requests[0]["messages"][0]["content"]
    assert "WebSearch" in system and "name the URLs" in system
    [page] = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "verifies them in one batch" in page
    agent.close()


def test_the_settings_page_stores_the_key_and_never_shows_it(isolated_home, tmp_path, web):
    store = Store(tmp_path / "ui.db")
    auth = {"Authorization": "Bearer t0ken"}
    with TestClient(create_app("t0ken", store=store, static_dir=None)) as client:
        assert client.get("/api/web").status_code == 401
        assert client.get("/api/web", headers=auth).json()["search"] == "duckduckgo"

        saved = client.put("/api/web", headers=auth, json={"search": "brave", "url": f"{web.url}/brave",
                                                           "api_key": "brave-key"})
        assert saved.status_code == 200 and saved.json()["has_key"] and saved.json()["key_env"] == "BRAVE_API_KEY"
        assert read_secrets()["BRAVE_API_KEY"] == "brave-key"
        assert "brave-key" not in client.get("/api/web", headers=auth).text

        tried = client.post("/api/web/search", headers=auth, json={"query": "draft"})
        assert tried.status_code == 200 and tried.json()["results"][0]["url"] == "https://example.org/b"

        assert client.put("/api/web", headers=auth, json={"search": "bing"}).status_code == 400
        assert client.put("/api/web", headers=auth, json={"search": "searxng"}).status_code == 400
    store.close()
