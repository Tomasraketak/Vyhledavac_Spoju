"""Trasa konkrétního vlaku z cd.cz – zastávky a časy, bez prohlížeče.

Stránka ``https://www.cd.cz/vlak/<číslo>/<datum>/<odkud>/<odjezd>/<kam>/<příjezd>/``
má seznam zastávek přímo v HTML (data pro Knockout: ``"name"``, ``"timeArr"``,
``"timeDep"``, ``"stationUrl"``). Funguje pro vlaky ČD a části zahraničních,
ne pro RegioJet ani Leo Express.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

URL = "https://www.cd.cz/vlak/{number}/{date}/{codes}"
_TRAIN_NO = re.compile(r"\b(\d{1,6})\b")


@dataclass(frozen=True)
class TrainStop:
    name: str
    arrival: dt.datetime | None
    departure: dt.datetime | None
    code: str | None       # 54xxxxx, prázdné u zahraničních zastávek

    @property
    def time(self) -> dt.datetime | None:
        return self.departure or self.arrival


def train_number(line: str) -> str | None:
    """'R 933 Krakonoš' -> '933'."""
    match = _TRAIN_NO.search(line or "")
    return match.group(1) if match else None


def _url(number: str, departure: dt.datetime, from_code: str | None, to_code: str | None,
         arrival: dt.datetime | None) -> str:
    date = f"{departure.day}.{departure.month}.{departure.year}"
    codes = ""
    if from_code and to_code and arrival is not None:
        codes = (f"{from_code}/{departure:%H}.{departure:%M}/"
                 f"{to_code}/{arrival:%H}.{arrival:%M}/")
    return URL.format(number=number, date=date, codes=codes)


def _route_rows(html: str) -> list[dict]:
    """Pole ``"route": [...]`` z dat stránky (jedna položka = zastávka)."""
    start = html.find('"route":[')
    if start < 0:
        return []
    try:
        rows, _ = json.JSONDecoder().raw_decode(html, start + len('"route":'))
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("name")]


def parse(html: str, day: dt.datetime) -> list[TrainStop]:
    stops: list[TrainStop] = []
    previous: dt.datetime | None = None

    def when(text: str | None) -> dt.datetime | None:
        nonlocal previous
        if not text or not re.match(r"^\d{1,2}:\d{2}$", text):
            return None
        hh, mm = (int(x) for x in text.split(":"))
        value = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        while previous is not None and value < previous - dt.timedelta(hours=1):
            value += dt.timedelta(days=1)          # vlak přes půlnoc
        previous = value
        return value

    for row in _route_rows(html):
        arrival = when(row.get("timeArr"))
        departure = when(row.get("timeDep"))
        code = re.search(r"/stanice/(\d+)/", row.get("stationUrl") or "")
        stops.append(TrainStop(row["name"], arrival, departure, code.group(1) if code else None))
    return stops


async def train_stops(client: httpx.AsyncClient, line: str, departure: dt.datetime,
                      arrival: dt.datetime | None = None, from_code: str | None = None,
                      to_code: str | None = None) -> list[TrainStop]:
    """Zastávky vlaku ``line`` jedoucího ``departure``; prázdný seznam, když to nejde."""
    number = train_number(line)
    if number is None:
        return []
    url = _url(number, departure, from_code, to_code, arrival)
    try:
        response = await client.get(url, timeout=20, follow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "cs"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("cd.cz: trasu vlaku %s nelze načíst (%s)", line, exc)
        return []
    return parse(response.text, departure)
