"""
TPP addition to transfermarkt-api: a player's penalty record (scored AND missed).

Transfermarkt lists every penalty a player has taken on one page. This endpoint
returns that page's tables as plain data (heading, column headings, and each
cell's text plus any club / competition names held in image titles), so the
TPP Stats plugin on MMU can pick out the ones taken for Manchester United.

Endpoint:  GET /penalties/player/{player_id}
"""
import re

import requests
from fastapi import APIRouter, HTTPException
from lxml import html

router = APIRouter()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip()


@router.get("/player/{player_id}")
def player_penalties(player_id: str):
    if not player_id.isdigit():
        raise HTTPException(status_code=400, detail="Player id must be a number.")
    url = f"https://www.transfermarkt.com/-/elfmetertore/spieler/{player_id}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Transfermarkt could not be reached: {exc}")
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail="Transfermarkt returned an error.")

    doc = html.fromstring(res.content)
    tables = []
    for table in doc.xpath('//table[contains(@class,"items")]'):
        heading_el = table.xpath("preceding::h2[1]")
        heading = _text(heading_el[0]) if heading_el else ""
        head = [_text(th) for th in table.xpath(".//thead//th")]
        rows = []
        for tr in table.xpath("./tbody/tr"):
            cells = []
            for td in tr.xpath("./td"):
                titles = [t.strip() for t in td.xpath(".//@title") if t.strip()]
                alts = [a.strip() for a in td.xpath(".//img/@alt") if a.strip()]
                cells.append({"text": _text(td), "titles": titles, "alts": alts})
            if cells:
                rows.append(cells)
        tables.append({"heading": heading, "head": head, "rows": rows})
    del doc  # Free the page straight away: the service runs on a small instance.
    return {"id": player_id, "url": url, "tables": tables}
