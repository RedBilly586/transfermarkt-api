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
    # Where the page loads its data from when it is filled in by JavaScript.
    components = sorted(set(re.findall(r"<(tm-[a-z0-9-]+)", html_text)))[:30]
    data_links = sorted(set(re.findall(r"""["'](/(?:ceapi|api)/[^"'\s<>]{3,200})["']""", html_text)))[:40]
    # Absolute data addresses (Transfermarkt's data server is on transfermarkt.technology) and script files.
    api_hosts = sorted(set(re.findall(r"""https?://[a-z0-9.-]*(?:tmapi|api)[a-z0-9.-]*\.[a-z]{2,}[^"'\s<>]{0,120}""", html_text)))[:30]
    scripts = sorted(set(re.findall(r"""<script[^>]+src=["']([^"']+)["']""", html_text)))[:30]
    found_in_scripts = []
    for src in scripts:
        # The player components (national team career, performance tables) hold the data address.
        if "svelte-components/player-" not in src:
            continue
        full = src if src.startswith("http") else "https://www.transfermarkt.com" + src
        try:
            st, body = _fetch(full)
        except Exception:
            continue
        if st != 200:
            continue
        text = body.decode("utf-8", errors="replace")
        for m in re.finditer(r"""https?://[a-z0-9.-]+\.[a-z]{2,}/[^"'`\s<>]{0,140}|["'`]/[a-z0-9_-]+/[a-z0-9/_${}.?=&-]{3,140}["'`]""", text, re.I):
            u = m.group(0)
            if re.search(r"api|technology|performance|national|player|stats|debut|ceapi", u, re.I) and not re.search(r"\.(png|svg|jpg|css|woff)", u, re.I):
                found_in_scripts.append(src.rsplit("/", 2)[-2] + ": " + u[:160])
        del text, body
        if len(found_in_scripts) > 80:
            break
    attrs = sorted(set(re.findall(r"""\s(data-[a-z0-9-]+|:[a-z0-9-]+|player-id|spieler-id)=["']""", html_text)))[:40]
    snippets = []
    for m in re.finditer(r"debut|nationalmannschaft|national-team|ceapi", html_text, re.I):
        a = max(0, m.start() - 120)
        snippets.append(_clean(html_text[a:m.end() + 160]))
        if len(snippets) >= 8:
            break
    del parser, content, html_text
    return {"id": player_id, "url": url, "title": _clean(title.group(1)) if title else "", "page_size": size, "table_classes": table_classes, "headings": h2s, "components": components, "data_links": data_links, "api_hosts": api_hosts, "scripts": scripts, "found_in_scripts": sorted(set(found_in_scripts))[:60], "attributes": attrs, "snippets": snippets, "tables": tables}


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
    # Where the page loads its data from when it is filled in by JavaScript.
    components = sorted(set(re.findall(r"<(tm-[a-z0-9-]+)", html_text)))[:30]
    data_links = sorted(set(re.findall(r"""["'](/(?:ceapi|api)/[^"'\s<>]{3,200})["']""", html_text)))[:40]
    # Absolute data addresses (Transfermarkt's data server is on transfermarkt.technology) and script files.
    api_hosts = sorted(set(re.findall(r"""https?://[a-z0-9.-]*(?:tmapi|api)[a-z0-9.-]*\.[a-z]{2,}[^"'\s<>]{0,120}""", html_text)))[:30]
    scripts = sorted(set(re.findall(r"""<script[^>]+src=["']([^"']+)["']""", html_text)))[:30]
    found_in_scripts = []
    for src in scripts:
        # The player components (national team career, performance tables) hold the data address.
        if "svelte-components/player-" not in src:
            continue
        full = src if src.startswith("http") else "https://www.transfermarkt.com" + src
        try:
            st, body = _fetch(full)
        except Exception:
            continue
        if st != 200:
            continue
        text = body.decode("utf-8", errors="replace")
        for m in re.finditer(r"""https?://[a-z0-9.-]+\.[a-z]{2,}/[^"'`\s<>]{0,140}|["'`]/[a-z0-9_-]+/[a-z0-9/_${}.?=&-]{3,140}["'`]""", text, re.I):
            u = m.group(0)
            if re.search(r"api|technology|performance|national|player|stats|debut|ceapi", u, re.I) and not re.search(r"\.(png|svg|jpg|css|woff)", u, re.I):
                found_in_scripts.append(src.rsplit("/", 2)[-2] + ": " + u[:160])
        del text, body
        if len(found_in_scripts) > 80:
            break
    attrs = sorted(set(re.findall(r"""\s(data-[a-z0-9-]+|:[a-z0-9-]+|player-id|spieler-id)=["']""", html_text)))[:40]
    snippets = []
    for m in re.finditer(r"debut|nationalmannschaft|national-team|ceapi", html_text, re.I):
        a = max(0, m.start() - 120)
        snippets.append(_clean(html_text[a:m.end() + 160]))
        if len(snippets) >= 8:
            break
    del parser, content, html_text
    return {"id": player_id, "url": url, "title": _clean(title.group(1)) if title else "", "page_size": size, "table_classes": table_classes, "headings": h2s, "components": components, "data_links": data_links, "api_hosts": api_hosts, "scripts": scripts, "found_in_scripts": sorted(set(found_in_scripts))[:60], "attributes": attrs, "snippets": snippets, "tables": tables}


def _summary(obj, depth=0):
    """A short view of a big JSON reply: field names, list lengths and the first two entries of each list."""
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: _summary(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        return {"_count": len(obj), "_first": [_summary(x, depth + 1) for x in obj[:2]]}
    return obj


@router.get("/tmapi/{path:path}")
def tmapi(path: str, full: int = 0):
    """Transfermarkt's own data server (used by its national team, debut and performance pages).
    Default: a summary of the reply. full=1: the whole reply."""
    if not re.match(r"^[A-Za-z0-9/_.-]{3,200}$", path):
        raise HTTPException(status_code=400, detail="Bad path.")
    url = f"https://tmapi.transfermarkt.technology/{path}"
    try:
        status, content = _fetch(url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transfermarkt could not be reached: {exc}")
    if status != 200:
        raise HTTPException(status_code=status if status >= 400 else 502, detail=f"Transfermarkt's data server returned {status}.")
    import json

    try:
        data = json.loads(content.decode("utf-8", errors="replace"))
    except ValueError:
        raise HTTPException(status_code=502, detail="Transfermarkt's data server did not send data.")
    del content
    return data if full else {"url": url, "summary": _summary(data)}


def _find(obj, want):
    """The first value whose key contains every word in want (case-insensitive), searching nested data."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if all(w in kl for w in want) and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            r = _find(v, want)
            if r is not None:
                return r
    return None


@router.get("/games/{player_id}")
def player_games(player_id: str):
    """Every game of a player's career, one short line each (from Transfermarkt's data server):
    date, season, competition, national team or club, club and opponent ids, goals, minutes."""
    if not player_id.isdigit():
        raise HTTPException(status_code=400, detail="Player id must be a number.")
    import json

    url = f"https://tmapi.transfermarkt.technology/player/{player_id}/performance-game"
    try:
        status, content = _fetch(url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transfermarkt could not be reached: {exc}")
    if status != 200:
        raise HTTPException(status_code=status if status >= 400 else 502, detail=f"Transfermarkt's data server returned {status}.")
    data = json.loads(content.decode("utf-8", errors="replace"))
    del content
    games = (data.get("data") or {}).get("performance") or []
    out = []
    for g in games:
        info = g.get("gameInformation") or {}
        clubs = g.get("clubsInformation") or {}
        stats = g.get("statistics") or {}
        out.append({
            "date": _find(info.get("date"), ["date"]) if isinstance(info.get("date"), dict) else info.get("date"),
            "season": info.get("seasonId"),
            "competition": info.get("competitionId"),
            "national": info.get("isNationalGame"),
            "club": _find(clubs.get("club"), ["id"]) if isinstance(clubs.get("club"), dict) else clubs.get("club"),
            "opponent": _find(clubs.get("opponent"), ["id"]) if isinstance(clubs.get("opponent"), dict) else clubs.get("opponent"),
            "goals": _find(stats.get("goalStatistics"), ["goal"]),
            "minutes": _find(stats.get("playingTimeStatistics"), ["minute"]),
        })
    first = games[0] if games else None
    del data, games
    return {"id": player_id, "count": len(out), "first_game_in_full": first, "games": out[:5], "games_are_cut_to": 5}

