"""Fetching web pages for research and reading their text.

Pages come from search results, so both the address and the content are
untrusted:

- Only http(s), and never a private, loopback, link-local or otherwise internal
  address (checked on every redirect hop), so a search result can't point the
  worker at this machine or the local network.
- At most ``max_bytes`` are read; only HTML and plain text are accepted.
- Pages that fail, need a login or subscription, or have too little text to be
  an article raise :class:`FetchFailed`, and the job moves on.

The text that comes back is still untrusted: the pipeline wraps it in markers
and never follows instructions found in it (see ``pipeline.py``).
"""

import asyncio
import ipaddress
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; JARVIS-research/1.0; personal assistant)"
MAX_REDIRECTS = 5
MIN_TEXT_CHARS = 400
ACCEPTED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscribe to read",
    "subscribers only",
    "this content is for subscribers",
    "to continue reading",
    "create a free account to continue",
    "sign in to continue reading",
    "already a subscriber",
    "you have reached your free article limit",
    "paywall",
)

# Instructions aimed at an AI, planted in a page. Lines like these are never used as findings.
INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instructions?|prompts?|rules|directions)\b",
        r"\b(previous|prior|above|earlier)\s+(instructions?|prompts?)\b",
        r"\bsystem\s+prompt\b",
        r"\bnew\s+instructions?\b",
        r"\b(you are|act as|pretend to be)\s+(now\s+)?(an?\s+)?(ai|assistant|language model|llm|chatbot)\b",
        r"\bif you are an?\s+(ai|assistant|language model|llm|chatbot)\b",
        r"\b(ai|llm)\s+(assistants?|agents?|models?)\s+(reading|processing|summari[sz]ing)\s+this\b",
        r"<\s*/?\s*(system|instructions?|assistant)\s*>",
    )
]


def looks_like_injection(text: str) -> bool:
    return any(p.search(text or "") for p in INJECTION_PATTERNS)


class FetchFailed(Exception):
    """The page couldn't be used. ``str(e)`` says why (shown in the report's source list)."""


@dataclass
class Page:
    url: str  # as requested
    final_url: str  # after redirects
    title: str
    text: str
    published: str  # publication date when the page states one, else ""
    fetched_at: str
    has_injection: bool = False  # the page contains instructions aimed at an AI


Resolver = Callable[[str], Awaitable[list[str]]]


async def _system_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def _is_internal(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


class PageFetcher:
    def __init__(
        self,
        *,
        timeout_secs: float = 15.0,
        max_bytes: int = 3_000_000,
        page_chars: int = 8000,
        http: httpx.AsyncClient | None = None,
        resolve: Resolver | None = None,
    ):
        self._timeout = timeout_secs
        self._max_bytes = max_bytes
        self._page_chars = page_chars
        self._http = http
        self._resolve = resolve or _system_resolve

    async def fetch(self, url: str) -> Page:
        if self._http is not None:
            return await self._fetch(self._http, url)
        async with httpx.AsyncClient() as http:
            return await self._fetch(http, url)

    async def _check_address(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchFailed("not a web address")
        host = parts.hostname
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = await self._resolve(host)
            except OSError as e:
                raise FetchFailed("the site's address couldn't be found") from e
        if not addresses or any(_is_internal(a) for a in addresses):
            raise FetchFailed("refused: the address is on a private or internal network")

    async def _fetch(self, http: httpx.AsyncClient, url: str) -> Page:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_address(current)
            try:
                async with http.stream(
                    "GET",
                    current,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.8"},
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise FetchFailed(f"bad redirect (HTTP {response.status_code})")
                        current = urljoin(current, location)
                        continue
                    if response.status_code in (401, 402, 403, 451):
                        raise FetchFailed(f"needs a login or subscription (HTTP {response.status_code})")
                    if response.status_code >= 400:
                        raise FetchFailed(f"the page didn't load (HTTP {response.status_code})")
                    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and ctype not in ACCEPTED_TYPES:
                        raise FetchFailed(f"not a web page ({ctype})")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self._max_bytes:
                            break
                    encoding = response.encoding or "utf-8"
            except httpx.HTTPError as e:
                raise FetchFailed(f"the page didn't load ({type(e).__name__})") from e
            raw = bytes(body[: self._max_bytes]).decode(encoding, errors="replace")
            return self._read(url, current, raw, is_html=ctype != "text/plain")
        raise FetchFailed("too many redirects")

    def _read(self, url: str, final_url: str, raw: str, *, is_html: bool) -> Page:
        if is_html:
            parsed = parse_html(raw)
            title, text, published = parsed.title, parsed.text, parsed.published
        else:
            title, text, published = "", _clean_text(raw), ""
        lowered = text.lower()
        if len(text) < MIN_TEXT_CHARS:
            raise FetchFailed("too little readable text (paywalled, or needs JavaScript)")
        if len(text) < 3000 and any(m in lowered for m in PAYWALL_MARKERS):
            raise FetchFailed("paywalled")
        return Page(
            url=url,
            final_url=final_url,
            title=title or urlsplit(final_url).hostname or final_url,
            text=text[: self._page_chars],
            published=published,
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            has_injection=looks_like_injection(text),
        )


# HTML to text

_SKIP = {
    "script", "style", "noscript", "template", "svg", "nav", "footer", "header",
    "aside", "form", "iframe", "button", "select", "canvas", "dialog",
}
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
    "section", "article", "blockquote", "pre", "table", "main", "figcaption", "dd", "dt",
}
_DATE_META = {
    "article:published_time", "og:published_time", "datepublished", "pubdate",
    "publishdate", "publish-date", "date", "dc.date", "dc.date.issued",
    "dcterms.created", "sailthru.date", "parsely-pub-date", "article.published",
}


@dataclass
class ParsedHtml:
    title: str
    text: str
    published: str


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.title = ""
        self.og_title = ""
        self.published = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key == "og:title" and not self.og_title:
                self.og_title = a.get("content", "")
            if key in _DATE_META and not self.published:
                self.published = a.get("content", "")
            return
        if tag == "time" and not self.published and a.get("datetime"):
            self.published = a["datetime"]
        if tag in _SKIP:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)


_JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]{6,40})"')


def parse_html(raw: str) -> ParsedHtml:
    reader = _Reader()
    try:
        reader.feed(raw)
        reader.close()
    except Exception:  # malformed markup: keep what was read
        pass
    published = reader.published
    if not published:
        match = _JSONLD_DATE.search(raw)
        published = match.group(1) if match else ""
    title = unescape((reader.og_title or reader.title).strip())
    return ParsedHtml(
        title=" ".join(title.split())[:300],
        text=_clean_text("".join(reader.parts)),
        published=_short_date(published),
    )


def _clean_text(text: str) -> str:
    lines, previous = [], None
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and line != previous:
            lines.append(line)
        previous = line
    return "\n".join(lines)


def _short_date(value: str) -> str:
    """A publication date as YYYY-MM-DD when it parses, else as given (trimmed)."""
    value = (value or "").strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:40]
