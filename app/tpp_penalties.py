"""
TPP addition to transfermarkt-api: a player's penalty record (scored AND missed).

Transfermarkt lists every penalty a player has taken on one page. This endpoint
returns that page's tables as plain data (heading, column headings, and each
cell's text plus any club / competition names held in image titles), so the
TPP Stats plugin on MMU can pick out the ones taken for Manchester United.

Endpoints:  GET /penalties/player/{player_id}   (penalties scored and missed)
            GET /penalties/debuts/{player_id}   (his debut for each club and competition)

Uses only Python's own HTML reader, and imports any web library only when a
request comes in, so this file can never stop the service from starting.
"""
import re
from html.parser import HTMLParser

from fastapi import APIRouter, HTTPException

router = APIRouter()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _fetch(url: str):
    """Fetch a page with whichever HTTP library this service has installed."""
    try:
        from curl_cffi import requests as cffi

        res = cffi.get(url, headers=HEADERS, timeout=30, impersonate="chrome")
        return res.status_code, res.content
    except ImportError:
        pass
    try:
        import httpx

        res = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        return res.status_code, res.content
    except ImportError:
        pass
    try:
        import requests

        res = requests.get(url, headers=HEADERS, timeout=30)
        return res.status_code, res.content
    except ImportError:
        pass
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class _Tables(HTMLParser):
    """Collects every <table class="items"> with the <h2> before it. Tables nested inside a
    cell (Transfermarkt puts the player or club box in one) count as part of that cell."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.stack = []  # One entry per open <table>: the items table dict, or None for others.
        self.h2 = None
        self.last_h2 = ""
        self.cur = None  # The items table being read.

    def _in_cell(self):
        return self.cur is not None and self.cur["cell"] is not None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._in_cell():
            for key in ("title", "alt"):
                if a.get(key, "").strip():
                    self.cur["cell"]["titles" if key == "title" else "alts"].append(a[key].strip())
        if tag == "h2" and self.cur is None:
            self.h2 = []
        if tag == "table":
            if self._in_cell() or self.cur is not None:
                self.stack.append(None)  # Nested table: part of the current cell.
                return
            if "items" in (a.get("class") or "").split():
                self.cur = {"heading": self.last_h2, "head": [], "rows": [], "section": "", "row": None, "cell": None}
                self.stack.append(self.cur)
            else:
                self.stack.append(None)
            return
        if self.cur is None or not self.stack or self.stack[-1] is not self.cur:
            return  # Structure tags count only at the items table's own level.
        if tag in ("thead", "tbody", "tfoot"):
            self.cur["section"] = tag
        elif tag == "tr":
            self.cur["row"] = []
        elif tag in ("td", "th"):
            self.cur["cell"] = {"text": "", "titles": [a["title"].strip()] if a.get("title", "").strip() else [], "alts": [], "kind": tag}

    def handle_endtag(self, tag):
        if tag == "h2" and self.h2 is not None:
            self.last_h2 = _clean("".join(self.h2))
            self.h2 = None
            return
        if tag == "table":
            if not self.stack:
                return
            top = self.stack.pop()
            if top is not None and top is self.cur:
                self.tables.append({"heading": top["heading"], "head": top["head"], "rows": top["rows"]})
                self.cur = None
            return
        if self.cur is None or not self.stack or self.stack[-1] is not self.cur:
            return
        if tag in ("td", "th") and self.cur["cell"] is not None:
            cell = self.cur["cell"]
            cell["text"] = _clean(cell["text"])
            if self.cur["section"] == "thead" or cell["kind"] == "th" and self.cur["section"] != "tbody":
                self.cur["head"].append(cell["text"])
            elif self.cur["row"] is not None:
                self.cur["row"].append({"text": cell["text"], "titles": cell["titles"], "alts": cell["alts"]})
            self.cur["cell"] = None
        elif tag == "tr" and self.cur["row"] is not None:
            if self.cur["row"]:
                self.cur["rows"].append(self.cur["row"])
            self.cur["row"] = None

    def handle_data(self, data):
        if self.h2 is not None:
            self.h2.append(data)
        if self._in_cell():
            self.cur["cell"]["text"] += data + " "


@router.get("/player/{player_id}")
def player_penalties(player_id: str, page: int = 1):
    """page: Transfermarkt shows long lists 15 rows at a time; ask for page 2, 3... for the rest."""
    if not player_id.isdigit():
        raise HTTPException(status_code=400, detail="Player id must be a number.")
    url = f"https://www.transfermarkt.com/-/elfmetertore/spieler/{player_id}"
    if page > 1:
        url += f"/page/{int(page)}"
    try:
        status, content = _fetch(url)
    except Exception as exc:  # Network trouble: report it, never crash.
        raise HTTPException(status_code=502, detail=f"Transfermarkt could not be reached: {exc}")
    if status != 200:
        raise HTTPException(status_code=status if status >= 400 else 502, detail="Transfermarkt returned an error.")
    parser = _Tables()
    parser.feed(content.decode("utf-8", errors="replace"))
    tables = parser.tables
    del parser, content  # Free the page straight away: the service runs on a small instance.
    return {"id": player_id, "page": page, "url": url, "tables": tables}


@router.get("/debuts/{player_id}")
def player_debuts(player_id: str):
    """A player's debut for each club and competition, from Transfermarkt's debuts page."""
    if not player_id.isdigit():
        raise HTTPException(status_code=400, detail="Player id must be a number.")
    url = f"https://www.transfermarkt.com/-/debuets/spieler/{player_id}"
    try:
        status, content = _fetch(url)
    except Exception as exc:  # Network trouble: report it, never crash.
        raise HTTPException(status_code=502, detail=f"Transfermarkt could not be reached: {exc}")
    if status != 200:
        raise HTTPException(status_code=status if status >= 400 else 502, detail="Transfermarkt returned an error.")
    html_text = content.decode("utf-8", errors="replace")
    parser = _Tables()
    parser.feed(html_text)
    tables = parser.tables
    # Diagnostic while the page layout is being checked: what the page is, and which tables it has.
    title = re.search(r"<title>(.*?)</title>", html_text, re.S | re.I)
    table_classes = re.findall(r"<table[^>]*class=\"([^\"]*)\"", html_text)[:20]
    h2s = [_clean(re.sub(r"<[^>]+>", " ", h)) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", html_text, re.S)][:20]
    size = len(html_text)
    del parser, content, html_text
    return {"id": player_id, "url": url, "title": _clean(title.group(1)) if title else "", "page_size": size, "table_classes": table_classes, "headings": h2s, "tables": tables}


# Other player pages, read the same way (for checking their layout, then importing).
_PAGES = {
    "debuts": "debuets",
    "national": "nationalmannschaft",
    "goals": "alletore",
}


@router.get("/page/{kind}/{player_id}")
def player_page(kind: str, player_id: str):
    """kind: debuts, national (national-team career, tournament by tournament) or goals (every goal)."""
    if kind not in _PAGES:
        raise HTTPException(status_code=400, detail="Unknown page: use one of " + ", ".join(_PAGES))
    if not player_id.isdigit():
        raise HTTPException(status_code=400, detail="Player id must be a number.")
    url = f"https://www.transfermarkt.com/-/{_PAGES[kind]}/spieler/{player_id}"
    try:
        status, content = _fetch(url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transfermarkt could not be reached: {exc}")
    if status != 200:
        raise HTTPException(status_code=status if status >= 400 else 502, detail="Transfermarkt returned an error.")
    html_text = content.decode("utf-8", errors="replace")
    parser = _Tables()
    parser.feed(html_text)
    tables = parser.tables
    title = re.search(r"<title>(.*?)</title>", html_text, re.S | re.I)
    table_classes = re.findall(r"<table[^>]*class=\"([^\"]*)\"", html_text)[:20]
    h2s = [_clean(re.sub(r"<[^>]+>", " ", h)) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", html_text, re.S)][:20]
    size = len(html_text)
    del parser, content, html_text
    return {"id": player_id, "url": url, "title": _clean(title.group(1)) if title else "", "page_size": size, "table_classes": table_classes, "headings": h2s, "tables": tables}

