import json

import httpx
import pytest

from print_warden import intake


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_direct_file_link_is_that_file():
    async with _client(lambda r: httpx.Response(500)) as c:
        page = await intake.resolve("https://cdn.example/parts/Bracket.STL?x=1", c)
    assert page.pick(None).name == "Bracket.STL"
    assert page.license is None


async def test_thingiverse_needs_a_token_and_keeps_author_and_license():
    seen = []

    def handler(r):
        seen.append((r.url.path, r.headers.get("authorization")))
        if r.url.path == "/things/42":
            return httpx.Response(
                200,
                json={
                    "name": "Cable clip",
                    "creator": {"name": "maker"},
                    "license": "Creative Commons - Attribution",
                },
            )
        return httpx.Response(
            200,
            json=[
                {"name": "clip.stl", "size": 1234, "download_url": "https://api.thingiverse.com/files/1/download"},
                {"name": "readme.pdf", "download_url": "https://api.thingiverse.com/files/2/download"},
                {"name": "clip_v2.3mf", "download_url": "https://api.thingiverse.com/files/3/download"},
            ],
        )  # fmt: skip

    url = "https://www.thingiverse.com/thing:42/files"
    async with _client(handler) as c:
        with pytest.raises(ValueError, match="THINGIVERSE_TOKEN"):
            await intake.resolve(url, c)
        page = await intake.resolve(url, c, "tok")
    assert seen == [("/things/42", "Bearer tok"), ("/things/42/files", "Bearer tok")]
    assert (page.title, page.author, page.license) == (
        "Cable clip",
        "maker",
        "Creative Commons - Attribution",
    )
    assert [f.name for f in page.files] == ["clip.stl", "clip_v2.3mf"]
    assert page.files[0].headers == {"Authorization": "Bearer tok"}
    with pytest.raises(ValueError, match="name one with `file`"):
        page.pick(None)
    assert page.pick("clip_v2").name == "clip_v2.3mf"
    with pytest.raises(ValueError, match="not on the page"):
        page.pick("nope")
    assert page.public()["files"][0] == {"name": "clip.stl", "size": 1234}


async def test_printables_reads_the_model_and_asks_a_link_per_file():
    calls = []

    def handler(r):
        body = json.loads(r.content)
        calls.append(body["variables"])
        if "PrintProfile" in body["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "print": {
                            "name": "Spool holder",
                            "user": {"publicUsername": "printer_person"},
                            "license": {"name": "Creative Commons — Attribution — Noncommercial"},
                            "stls": [{"id": "7", "name": "holder.stl", "fileSize": 99}, {"id": "8", "name": "photo.png"}],
                        }
                    }
                },
            )  # fmt: skip
        return httpx.Response(
            200,
            json={
                "data": {
                    "getDownloadLink": {
                        "ok": True,
                        "output": {"link": "https://files.printables.com/x/holder.stl"},
                    }
                }
            },
        )

    async with _client(handler) as c:
        page = await intake.resolve(
            "https://www.printables.com/model/12345-spool-holder", c
        )
    assert calls[0] == {"id": "12345"}
    assert calls[1]["id"] == "7"
    assert calls[1]["modelId"] == "12345"
    assert len(calls) == 2
    f = page.pick(None)
    assert (f.name, f.url, f.size) == (
        "holder.stl",
        "https://files.printables.com/x/holder.stl",
        99,
    )
    assert page.author == "printer_person"
    assert page.license.startswith("Creative Commons")


async def test_printables_errors_say_the_api_is_unofficial():
    async with _client(
        lambda r: httpx.Response(200, json={"errors": [{"message": "changed"}]})
    ) as c:
        with pytest.raises(ValueError, match="unofficial"):
            await intake.resolve("https://www.printables.com/model/1-x", c)


async def test_any_page_yields_its_model_links_and_metadata():
    html = """<html><head><title>Fallback</title>
    <meta property="og:title" content="Desk hook &amp; clip">
    <meta name="author" content="someone">
    <link rel="license" href="https://creativecommons.org/licenses/by/4.0/"></head>
    <body><a href="files/hook.stl">STL</a> <a href="/files/hook.stl">again</a>
    <a href="https://other.example/clip.3mf">3MF</a> <a href="pic.jpg">pic</a>
    <a href="javascript:alert(1)//x.stl">bad</a></body></html>"""

    def handler(r):
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    async with _client(handler) as c:
        page = await intake.resolve("https://blog.example/posts/hook", c)
    assert [f.url for f in page.files] == [
        "https://blog.example/posts/files/hook.stl",
        "https://blog.example/files/hook.stl",
        "https://other.example/clip.3mf",
    ]
    assert page.title == "Desk hook & clip"
    assert page.author == "someone"
    assert page.license == "https://creativecommons.org/licenses/by/4.0/"


async def test_a_page_with_no_models_says_so():
    async with _client(
        lambda r: httpx.Response(200, text="<p>hi</p>", headers={"content-type": "text/html"})
    ) as c:  # fmt: skip
        page = await intake.resolve("https://example.com/", c)
    with pytest.raises(ValueError, match="no .stl/.3mf/.scad"):
        page.pick(None)


async def test_site_problems():
    def handler(r):
        if r.url.host == "api.thingiverse.com":
            return httpx.Response(
                200 if r.headers["authorization"] == "Bearer good" else 401
            )
        body = json.loads(r.content)
        assert body["variables"] == {"id": "0"}
        return httpx.Response(200, json={"data": {"print": None}})

    async with _client(handler) as c:
        assert await intake.site_problems(c, "good", True) == {
            "thingiverse": None,
            "printables": None,
        }
        bad = await intake.site_problems(c, "bad", False)
        assert bad == {"thingiverse": "token refused (HTTP 401)"}
        assert await intake.site_problems(c, None, False) == {}

    def changed(r):
        return httpx.Response(
            200,
            json={"errors": [{"message": 'Cannot query field "stls"'}], "data": None},
        )

    async with _client(changed) as c:
        out = await intake.site_problems(c, None, True)
    assert out["printables"].startswith("its API no longer takes the warden's query")
