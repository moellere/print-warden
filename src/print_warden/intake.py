"""Model intake: a model page URL to the files on it, with the title, author and license kept for the job.

- A direct link to a `.stl`/`.3mf`/`.scad` file is that one file; nothing is known about it.
- Thingiverse (`thingiverse.com/thing:<id>`): the v1 API (`things/<id>`, `things/<id>/files`), which needs an app
  token (`THINGIVERSE_TOKEN`); downloads carry it too.
- Printables (`printables.com/model/<id>-...`): the GraphQL endpoint the site itself uses (model, author, license and
  model files, then a download link per file). It is not a published API and may change; a failure says so.
- Any other page: the links on it that end in a model suffix, with `og:title`, the `author` meta and a
  `rel="license"` link when present.

Nothing here decides to print anything: it names files and where they came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

SUFFIXES = (".stl", ".scad", ".3mf")
THINGIVERSE_API = "https://api.thingiverse.com"
PRINTABLES_API = "https://api.printables.com/graphql/"
_THING = re.compile(r"thing:(\d+)")
_PRINTABLES = re.compile(r"/model/(\d+)")


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    size: int | None = None


@dataclass(frozen=True)
class Page:
    source_url: str
    files: list[ModelFile]
    title: str | None = None
    author: str | None = None
    license: str | None = None

    def pick(self, file: str | None) -> ModelFile:
        """The one file to print: the only one, or the one named (case-insensitive, by name or name stem)."""
        if not self.files:
            raise ValueError(f"no .stl/.3mf/.scad files found at {self.source_url}")
        if file:
            want = file.lower()
            for f in self.files:
                if want in (f.name.lower(), f.name.rsplit(".", 1)[0].lower()):
                    return f
            raise ValueError(
                f"{file!r} is not on the page; files: {', '.join(f.name for f in self.files)}"
            )
        if len(self.files) > 1:
            raise ValueError(
                f"{len(self.files)} model files on the page; name one with `file`: "
                + ", ".join(f.name for f in self.files)
            )
        return self.files[0]

    def public(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "title": self.title,
            "author": self.author,
            "license": self.license,
            "files": [{"name": f.name, "size": f.size} for f in self.files],
        }


def is_model_file(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(SUFFIXES)


def _name(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


async def resolve(
    url: str, client: httpx.AsyncClient, thingiverse_token: str | None = None
) -> Page:
    if is_model_file(url):
        return Page(url, [ModelFile(_name(url), url)])
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("thingiverse.com") and (m := _THING.search(url)):
        return await _thingiverse(url, m[1], client, thingiverse_token)
    if host.endswith("printables.com") and (m := _PRINTABLES.search(url)):
        return await _printables(url, m[1], client)
    return await _page(url, client)


async def _thingiverse(
    url: str, thing: str, client: httpx.AsyncClient, token: str | None
) -> Page:
    if not token:
        raise ValueError(
            "Thingiverse pages need an app token: set THINGIVERSE_TOKEN (thingiverse.com/apps/create)"
        )
    auth = {"Authorization": f"Bearer {token}"}
    r = await client.get(f"{THINGIVERSE_API}/things/{thing}", headers=auth)
    r.raise_for_status()
    meta = r.json()
    r = await client.get(f"{THINGIVERSE_API}/things/{thing}/files", headers=auth)
    r.raise_for_status()
    files = [
        ModelFile(str(f["name"]), str(f["download_url"]), auth, f.get("size"))
        for f in r.json()
        if str(f.get("name", "")).lower().endswith(SUFFIXES) and f.get("download_url")
    ]
    creator = meta.get("creator") or {}
    return Page(
        url,
        files,
        meta.get("name"),
        creator.get("name") or creator.get("first_name"),
        meta.get("license"),
    )


_PRINT_QUERY = """query PrintProfile($id: ID!) { print(id: $id) { id name user { publicUsername }
 license { name } stls { id name fileSize } } }"""
_LINK_QUERY = """mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!,
 $source: DownloadSourceEnum!) { getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: $source)
 { ok output { link } } }"""


async def _printables(url: str, model: str, client: httpx.AsyncClient) -> Page:
    async def gql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        r = await client.post(
            PRINTABLES_API, json={"query": query, "variables": variables}
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors") or not body.get("data"):
            raise ValueError(
                f"Printables did not answer as expected (its API is unofficial): {body.get('errors')}"
            )
        return dict(body["data"])

    p = (await gql(_PRINT_QUERY, {"id": model})).get("print")
    if not p:
        raise ValueError(f"Printables model {model} not found")
    files = []
    for f in p.get("stls") or []:
        if not str(f.get("name", "")).lower().endswith(SUFFIXES):
            continue
        out = (
            await gql(
                _LINK_QUERY,
                {
                    "id": f["id"],
                    "modelId": model,
                    "fileType": "stl",
                    "source": "model_detail",
                },
            )
        )["getDownloadLink"]
        if out and out.get("ok"):
            files.append(
                ModelFile(str(f["name"]), out["output"]["link"], {}, f.get("fileSize"))
            )
    return Page(
        url,
        files,
        p.get("name"),
        (p.get("user") or {}).get("publicUsername"),
        (p.get("license") or {}).get("name"),
    )


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.meta: dict[str, str] = {}
        self.license: str | None = None
        self._title: list[str] | None = None
        self.title: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
            if "license" in a.get("rel", "").split():
                self.license = a["href"]
        elif tag == "link" and "license" in a.get("rel", "").split():
            self.license = a.get("href") or self.license
        elif tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content"):
                self.meta[key.lower()] = a["content"]
        elif tag == "title":
            self._title = []

    def handle_data(self, data: str) -> None:
        if self._title is not None:
            self._title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title is not None:
            self.title = "".join(self._title).strip() or None
            self._title = None


async def _page(url: str, client: httpx.AsyncClient) -> Page:
    r = await client.get(url)
    r.raise_for_status()
    if is_model_file(str(r.url)):
        return Page(url, [ModelFile(_name(str(r.url)), str(r.url))])
    if "html" not in r.headers.get("content-type", "html"):
        raise ValueError(f"{url} is neither a model file nor a web page")
    p = _Links()
    p.feed(r.text[:2_000_000])
    seen: dict[str, ModelFile] = {}
    for href in p.links:
        full = urljoin(str(r.url), href)
        if full.startswith(("http://", "https://")) and is_model_file(full):
            seen.setdefault(full, ModelFile(_name(full), full))
    return Page(
        url,
        list(seen.values()),
        p.meta.get("og:title") or p.title,
        p.meta.get("author") or p.meta.get("article:author"),
        p.license,
    )
