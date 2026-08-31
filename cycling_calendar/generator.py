from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Alarm, Calendar, Event
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)
ROME = ZoneInfo("Europe/Rome")
UCI_API = "https://www.uci.org/api/calendar/{period}"
UCI_BASE = "https://www.uci.org"
CALENDAR_URL = "https://dizzle0987.github.io/cycling-calendar/calendar.ics"
RESULT_LOOKBACK_DAYS = 3
RESULT_FIELDS = ("stage_podium", "general_classification", "results_source", "results_url")

UCI_CLASSES = ("1.UWT", "2.UWT", "1.Pro", "2.Pro", "CM", "CC", "CN")
ALWAYS_INCLUDE_CLASSES = {"1.UWT", "2.UWT"}
SELECTED_PRO_RACES = {
    "alula-tour", "arctic-race-of-norway", "brabantse-pijl", "brussels-cycling-classic",
    "circuit-franco-belge", "coppa-agostoni-giro-delle-brianze", "coppa-bernocchi-gp-banco-bpm",
    "faun-ardeche-classic", "faun-drome-classic", "giro-dell-emilia", "giro-del-piemonte",
    "giro-del-veneto", "gran-piemonte", "gp-de-fourmies-la-voix-du-nord", "kuurne-brussel-kuurne",
    "milano-torino", "nokere-koerse", "paris-tours-elite", "presidential-cycling-tour-of-turkiye",
    "scheldeprijs", "tour-of-britain-men", "lloyds-tour-of-britain-men", "tour-of-oman",
    "tour-of-the-alps", "tre-valli-varesine", "tro-bro-leon", "trofeo-laigueglia",
    "veneto-classic", "vuelta-a-burgos", "omloop-het-nieuwsblad",
}
MONUMENTS = {
    "milano-sanremo", "ronde-van-vlaanderen", "paris-roubaix-hauts-de-france",
    "liege-bastogne-liege", "il-lombardia",
}
RACE_ALIASES = {
    "in-flanders-fields-from-middelkerke-to-wevelgem": "gent-wevelgem",
    "dssk-donostia-san-sebastian-klasikoa": "clasica-san-sebastian",
    "la-vuelta-ciclista-a-espana": "vuelta-a-espana",
    "national-road-championships-italy": "campionati-italiani-strada",
}

GRAND_TOURS = (
    {
        "race_key": "giro-d-italia",
        "name": "Giro d'Italia",
        "url": "https://www.giroditalia.it/en/the-route/",
        "parser": "giro",
        "country": "Italia",
        "broadcast_it": "Eurosport 1; streaming HBO Max e discovery+ (anche DAZN, TIMVISION e Prime Video Channels)",
        "broadcast_source_url": "https://www.eurosport.it/ciclismo/giro-d-italia/2026/eurosport-presenta-il-giro-ditalia-2026-in-diretta-integrale-su-hbo-max-e-discovery_sto23296443/story.shtml",
    },
    {
        "race_key": "tour-de-france",
        "name": "Tour de France",
        "url": "https://www.letour.fr/en/overall-route",
        "parser": "aso",
        "country": "Francia",
        "broadcast_it": "Eurosport 1; streaming HBO Max e discovery+ (anche DAZN, TIMVISION e Prime Video Channels)",
        "broadcast_source_url": "https://www.eurosport.it/ciclismo/tour-de-france/2026/eurosport-presenta-il-tour-de-france-2026-in-diretta-integrale-su-hbo-max-e-discovery_sto23315058/story.shtml",
    },
    {
        "race_key": "vuelta-a-espana",
        "name": "La Vuelta a España",
        "url": "https://www.lavuelta.es/en/overall-route",
        "parser": "aso",
        "country": "Spagna",
        "broadcast_it": "Eurosport; streaming HBO Max e discovery+ (anche DAZN, TIMVISION e Prime Video Channels)",
        "broadcast_source_url": "https://www.eurosport.it/tutti-gli-sport/una-grande-estate-su-eurosport-dove-guardare-tutti-gli-eventi_sto23197223/story.shtml",
    },
)


class UpdateError(RuntimeError):
    """All remote sources failed; existing generated files must remain intact."""


@dataclass
class FetchResult:
    events: list[dict[str, Any]]
    successful_sources: list[str]
    errors: list[str]


def parse_uci_result_index(html: str) -> list[dict[str, Any]]:
    """Read the structured result descriptors embedded by the official UCI page."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one('[data-component="CompetitionDetailsModule"][data-props]')
    if node is None:
        return []
    payload = json.loads(str(node.get("data-props") or "{}"))
    accordion = (payload.get("results") or {}).get("accordion") or []
    return [group for group in accordion if isinstance(group, dict)]


def parse_uci_podium(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_podium: list[dict[str, str]] = []
    for item in payload.get("results") or []:
        values = item.get("values") or {}
        try:
            rank = int(str(values.get("rank") or ""))
        except ValueError:
            continue
        if rank > 3 or item.get("headerType") != "rider":
            continue
        rider = " ".join(
            part.strip() for part in (str(values.get("firstname") or ""), str(values.get("lastname") or ""))
            if part.strip()
        )
        if not rider:
            continue
        raw_podium.append({
            "rank": str(rank),
            "rider": rider,
            "team": str(values.get("team") or "").strip(),
            "result": str(values.get("result") or "").strip(),
        })
    podium = sorted(raw_podium, key=lambda row: int(row["rank"]))[:3]

    def seconds(value: str) -> int | None:
        parts = value.split(":")
        if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
            return None
        values = [int(part) for part in parts]
        if len(values) == 2:
            values.insert(0, 0)
        return values[0] * 3600 + values[1] * 60 + values[2]

    leader_seconds = seconds(podium[0]["result"]) if podium else None
    for row in podium[1:]:
        if re.fullmatch(r"\+?\s*0+(?::0+){0,2}", row["result"]):
            row["result"] = "stesso tempo"
            continue
        raw_seconds = seconds(row["result"])
        if raw_seconds is None:
            continue
        if leader_seconds is not None and raw_seconds >= leader_seconds:
            raw_seconds -= leader_seconds
        if raw_seconds == 0:
            row["result"] = "stesso tempo"
        else:
            hours, remainder = divmod(raw_seconds, 3600)
            minutes, value_seconds = divmod(remainder, 60)
            row["result"] = f"+{hours}h {minutes:02d}' {value_seconds:02d}''" if hours else (
                f"+{minutes}' {value_seconds:02d}''" if minutes else f"+{value_seconds}''"
            )
    return podium


def _result_descriptor(
    groups: list[dict[str, Any]], stage_number: int | None, general: bool,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    if stage_number is not None:
        wanted = re.compile(rf"(?:stage|tappa)\s*{stage_number}\b", re.IGNORECASE)
        candidates = [group for group in groups if wanted.search(str(group.get("label") or ""))]
        if general:
            candidates.extend(
                group for group in groups
                if "final classification" in str(group.get("label") or "").casefold()
                and group not in candidates
            )
    else:
        candidates = groups[-1:] if groups else []
    titles = (
        ("Stage General Classification", "General Classification", "Final General Classification")
        if general else ("Stage Classification", "General Classification", "Final Classification")
    )
    for title in titles:
        for group in candidates:
            for descriptor in group.get("results") or []:
                if str(descriptor.get("title") or "").casefold() == title.casefold():
                    return descriptor
    return None


def _fetch_uci_podium(
    session: requests.Session, descriptor: dict[str, Any], source_url: str,
) -> list[dict[str, str]]:
    event_code = str(descriptor.get("eventCode") or "")
    if not event_code:
        return []
    response = session.get(
        f"{UCI_BASE}/api/calendar/results/{event_code}",
        params={
            "discipline": "ROA",
            "raceType": descriptor.get("raceType") or "A",
            "raceName": descriptor.get("title") or "",
        },
        headers={"Referer": source_url},
        timeout=25,
    )
    response.raise_for_status()
    return parse_uci_podium(response.json())


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "cycling-calendar/1.0 (+https://github.com/Dizzle0987/cycling-calendar)",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })
    return session


def normalize(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(char for char in plain if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def canonical_race_key(name: str) -> str:
    key = normalize(name)
    if "uci-road-world-championship" in key:
        return "uci-road-world-championships"
    if "road-european-championship" in key:
        return "uec-road-european-championships"
    return RACE_ALIASES.get(key, key)


def display_race_name(name: str, race_class: str) -> str:
    """Keep championship names stable while UCI changes the edition year."""
    if race_class == "CM" and "road world championship" in name.lower():
        return re.sub(r"(?:^|\s)\d{4}(?=\s|$)", " ", name).strip()
    if race_class == "CC" and "road european championship" in name.lower():
        return re.sub(r"(?:^|\s)\d{4}(?=\s|$)", " ", name).strip()
    return name


def _parse_uci_dates(value: str) -> tuple[date, date]:
    value = re.sub(r"\s+", " ", value.strip())
    formats = ("%d %b %Y", "%d %B %Y")
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            return parsed, parsed
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{2}) ([A-Za-z]+) - (\d{2}) ([A-Za-z]+) (\d{4})", value)
    if not match:
        raise ValueError(f"Intervallo UCI non riconosciuto: {value}")
    first_day, first_month, last_day, last_month, year = match.groups()
    end = datetime.strptime(f"{last_day} {last_month} {year}", "%d %b %Y").date()
    start_year = int(year) - (1 if datetime.strptime(first_month, "%b").month > end.month else 0)
    start = datetime.strptime(f"{first_day} {first_month} {start_year}", "%d %b %Y").date()
    return start, end


def _include_uci_event(name: str, race_class: str, country: str) -> bool:
    key = canonical_race_key(name)
    if race_class in ALWAYS_INCLUDE_CLASSES:
        return True
    if race_class in {"1.Pro", "2.Pro"}:
        return key in SELECTED_PRO_RACES
    if race_class == "CM":
        return "road-world-championship" in key
    if race_class == "CC":
        return "european-championship" in key
    return race_class == "CN" and country == "ITA"


def parse_uci_calendar(payload: dict[str, Any], race_class: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for month in payload.get("items") or []:
        for day in month.get("items") or []:
            for item in day.get("items") or []:
                name = str(item.get("name") or "").strip()
                country = str(item.get("country") or "").strip()
                details = item.get("detailsLink") or {}
                source_path = str(details.get("url") or "")
                if not name or not source_path or not _include_uci_event(name, race_class, country):
                    continue
                source_id_match = re.search(r"/(\d+)$", source_path)
                source_id = source_id_match.group(1) if source_id_match else canonical_race_key(name)
                if source_id in unique:
                    continue
                start_date, end_date = _parse_uci_dates(str(item.get("dates") or ""))
                race_key = canonical_race_key(name)
                public_name = display_race_name(name, race_class)
                category = "UCI WorldTour" if race_class.endswith("UWT") else (
                    "UCI ProSeries" if race_class.endswith("Pro") else "Campionato"
                )
                notes = "Orari e dettagli del percorso non ancora confermati dalla fonte primaria."
                if race_class == "CM":
                    notes = "Rassegna iridata: il programma specifico maschile sarà aggiunto quando confermato."
                elif race_class == "CC":
                    notes = "Rassegna europea: il programma specifico maschile sarà aggiunto quando confermato."
                elif race_class == "CN":
                    notes = "Campionati italiani: programma maschile e orari da confermare."
                unique[source_id] = {
                    "race_key": race_key,
                    "race_name": public_name,
                    "title": public_name,
                    "stage_number": None,
                    "start": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "all_day": True,
                    "country": country,
                    "category": category,
                    "uci_class": race_class,
                    "circuit": "UCI WorldTour" if race_class.endswith("UWT") else (
                        "UCI ProSeries" if race_class.endswith("Pro") else "UCI Road International Calendar"
                    ),
                    "source": "UCI",
                    "source_id": source_id,
                    "source_url": urljoin(UCI_BASE, source_path),
                    "official_url": urljoin(UCI_BASE, source_path),
                    "notes": notes,
                    "broadcast_it": _default_broadcast(race_class),
                }
    return list(unique.values())


def _default_broadcast(race_class: str) -> str:
    if race_class in {"1.UWT", "2.UWT"}:
        return "Eurosport / HBO Max / discovery+: programmazione della singola gara da verificare"
    if race_class == "CM":
        return "RAI; copertura Warner Bros. Discovery (canale/piattaforma da confermare)"
    return "Da confermare"


def _stage_type(value: str) -> str:
    key = normalize(value)
    if any(word in key for word in ("time-trial", "time-trial", "itt", "crono", "contre-la-montre")):
        return "cronometro"
    if any(word in key for word in ("hilly", "ondulada", "collinare", "media-montagna")):
        return "collinare"
    if any(word in key for word in ("mountain", "montagna", "alta-montagna")):
        return "montagna"
    if any(word in key for word in ("flat", "llana", "pianura", "pianeggiante", "veloce")):
        return "pianura"
    return value.strip() or "Da confermare"


def _parse_route_date(value: str) -> date:
    clean = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%a %m/%d/%Y", "%a. %d/%m/%Y", "%a %d/%m/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            continue
    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", clean)
    if match:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    raise ValueError(f"Data di tappa non riconosciuta: {value}")


def parse_aso_route_html(html: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    stages: list[dict[str, Any]] = []
    for row in soup.select("table tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if len(cells) < 5 or not cells[0].isdigit():
            continue
        number = int(cells[0])
        stage_date = _parse_route_date(cells[2])
        route = re.split(r"\s*>\s*", cells[3], maxsplit=1)
        if len(route) != 2:
            continue
        link = row.find("a", href=True)
        stages.append(_stage_event(
            config, number, stage_date, route[0], route[1], cells[4], cells[1],
            urljoin(config["url"], link["href"]) if link else config["url"],
        ))
    return stages


def parse_giro_route_html(html: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    stages: list[dict[str, Any]] = []
    # The official page uses cards rather than a semantic table. Each stage link is
    # the stable anchor; nearby text carries date, route, distance and difficulty.
    for link in soup.select('a[href*="stage-"]'):
        container = link.find_parent("div", class_="single-tappa")
        if container is None:
            continue
        number_match = re.search(r"(\d+)", str(container.get("id") or ""))
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})", container.get_text(" ", strip=True))
        start_node = container.select_one(".partenza-value")
        finish_node = container.select_one(".arrivo-value")
        distance_node = container.select_one(".distanza-value")
        if not number_match or not date_match or not start_node or not finish_node:
            continue
        number = int(number_match.group(1))
        raw_type = str(container.get("data-tipologia") or "")
        stages.append(_stage_event(
            config, number, _parse_route_date(date_match.group(1)),
            start_node.get_text(" ", strip=True), finish_node.get_text(" ", strip=True),
            f"{distance_node.get_text(' ', strip=True).replace(',', '.')} km" if distance_node else "",
            raw_type or ("cronometro" if "ITT" in finish_node.get_text().upper() else "Da confermare"),
            urljoin(config["url"], link["href"]),
        ))
    # Multiple responsive cards can repeat a stage.
    return list({event["stage_number"]: event for event in stages}.values())


def _stage_event(
    config: dict[str, Any], number: int, stage_date: date, start_place: str,
    finish_place: str, distance: str, stage_type: str, source_url: str,
) -> dict[str, Any]:
    race_name = str(config["name"])
    return {
        "race_key": config["race_key"],
        "race_name": race_name,
        "title": f"{race_name} — Tappa {number}: {start_place} → {finish_place}",
        "stage_number": number,
        "stage_name": f"{start_place} → {finish_place}",
        "start": stage_date.isoformat(),
        "end_date": stage_date.isoformat(),
        "all_day": True,
        "start_location": start_place,
        "finish_location": finish_place,
        "location": f"{start_place} → {finish_place}",
        "country": config["country"],
        "distance": distance,
        "stage_type": _stage_type(stage_type),
        "category": "Grande Giro",
        "uci_class": "2.UWT",
        "circuit": "UCI WorldTour",
        "source": f"Sito ufficiale {race_name}",
        "source_url": source_url,
        "official_url": source_url,
        "broadcast_it": config["broadcast_it"],
        "broadcast_source_url": config["broadcast_source_url"],
        "notes": "Orario di partenza e arrivo non confermato: evento pubblicato come giornata intera.",
    }


def fetch_remote_events(session: requests.Session, year: int) -> FetchResult:
    events: list[dict[str, Any]] = []
    successes: list[str] = []
    errors: list[str] = []
    calendar_years = (year, year + 1)
    for calendar_year in calendar_years:
        for race_class in UCI_CLASSES:
            class_events: list[dict[str, Any]] = []
            class_ok = False
            for period in ("past", "upcoming"):
                try:
                    response = session.get(
                        UCI_API.format(period=period),
                        params={
                            "discipline": "ROA", "raceCategory": "ME",
                            "raceClass": race_class, "year": calendar_year,
                        },
                        timeout=25,
                    )
                    response.raise_for_status()
                    class_events.extend(parse_uci_calendar(response.json(), race_class))
                    class_ok = True
                except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"UCI {race_class}/{period} {calendar_year}: {exc}")
            if class_ok:
                successes.append(f"UCI {race_class} {calendar_year}")
                events.extend(class_events)

    for config in GRAND_TOURS:
        try:
            response = session.get(config["url"], timeout=30)
            response.raise_for_status()
            parsed = (
                parse_aso_route_html(response.text, config)
                if config["parser"] == "aso" else parse_giro_route_html(response.text, config)
            )
            parsed = [event for event in parsed if int(str(event["start"])[:4]) in calendar_years]
            if not parsed:
                raise ValueError("nessuna tappa riconosciuta")
            events.extend(parsed)
            successes.append(f"Sito ufficiale {config['name']}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{config['name']}: {exc}")
    return FetchResult(events, successes, errors)


def enrich_with_uci_results(
    session: requests.Session,
    events: list[dict[str, Any]],
    uci_events: list[dict[str, Any]],
    today: date,
    *,
    backfill: bool = False,
) -> list[str]:
    """Add official top-three results without making result failures destructive."""
    errors: list[str] = []
    uci_by_race = {
        (event_edition(event), str(event.get("race_key") or "")): event
        for event in uci_events if event.get("source") == "UCI"
    }
    detailed_races = {
        (event_edition(event), str(event.get("race_key") or ""))
        for event in events if event.get("stage_number") is not None
    }
    cutoff = today - timedelta(days=RESULT_LOOKBACK_DAYS)
    eligible: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        try:
            event_date = date.fromisoformat(str(event.get("start") or "")[:10])
        except ValueError:
            continue
        race_id = (event_edition(event), str(event.get("race_key") or ""))
        if race_id not in uci_by_race or event_date > today:
            continue
        if not backfill and event_date < cutoff:
            continue
        eligible.setdefault(race_id, []).append(event)

    for race_id, race_events in eligible.items():
        uci_event = uci_by_race[race_id]
        source_url = str(uci_event.get("source_url") or "")
        if not source_url:
            continue
        try:
            response = session.get(source_url, timeout=25)
            response.raise_for_status()
            groups = parse_uci_result_index(response.text)
            if not groups:
                continue
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"Risultati UCI {uci_event.get('race_name')}: {exc}")
            continue

        for event in race_events:
            stage = event.get("stage_number")
            stage_number = int(stage) if stage not in (None, "") else None
            has_detailed_stages = race_id in detailed_races
            if stage_number is None and has_detailed_stages:
                continue
            try:
                if stage_number is not None:
                    stage_descriptor = _result_descriptor(groups, stage_number, general=False)
                    general_descriptor = _result_descriptor(groups, stage_number, general=True)
                    if stage_descriptor:
                        podium = _fetch_uci_podium(session, stage_descriptor, source_url)
                        if len(podium) == 3:
                            event["stage_podium"] = podium
                    if general_descriptor:
                        general = _fetch_uci_podium(session, general_descriptor, source_url)
                        if len(general) == 3:
                            event["general_classification"] = general
                else:
                    descriptor = _result_descriptor(groups, None, general=True)
                    if descriptor:
                        podium = _fetch_uci_podium(session, descriptor, source_url)
                        if len(podium) == 3:
                            field = "stage_podium" if str(event.get("uci_class") or "").startswith("1.") else "general_classification"
                            event[field] = podium
                if event.get("stage_podium") or event.get("general_classification"):
                    event["results_source"] = "UCI — risultati ufficiali"
                    event["results_url"] = source_url
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                label = f" tappa {stage_number}" if stage_number is not None else ""
                errors.append(f"Risultati UCI {uci_event.get('race_name')}{label}: {exc}")
    return errors


def event_identity(event: dict[str, Any]) -> str:
    race_key = event.get("race_key") or canonical_race_key(str(event.get("race_name") or event.get("title") or ""))
    stage = event.get("stage_number")
    suffix = f"stage-{int(stage):02d}" if stage not in (None, "") else "race"
    return f"{race_key}|{suffix}"


def event_edition(event: dict[str, Any]) -> str:
    return str(event.get("edition") or str(event.get("start") or "")[:4])


def dedupe_identity(event: dict[str, Any]) -> str:
    return f"{event_edition(event)}|{event_identity(event)}"


def stable_uid(event: dict[str, Any]) -> str:
    explicit = str(event.get("uid") or "").strip()
    if explicit:
        return explicit
    year = event_edition(event)
    digest = hashlib.sha256(f"{year}|{event_identity(event)}".encode()).hexdigest()[:24]
    return f"{digest}@cycling-calendar"


def deduplicate(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    priority = {"UCI": 10, "manuale": 100}
    for original in events:
        event = deepcopy(original)
        key = dedupe_identity(event)
        current = merged.get(key)
        if current is None:
            merged[key] = event
            continue
        new_priority = priority.get(str(event.get("source") or "").lower(), 50)
        old_priority = priority.get(str(current.get("source") or "").lower(), 50)
        primary, secondary = (event, current) if new_priority >= old_priority else (current, event)
        combined = deepcopy(secondary)
        combined.update({k: v for k, v in primary.items() if v not in (None, "", [])})
        merged[key] = combined

    # A detailed stage list supersedes the broad multi-day overview of the same race.
    detailed = {
        (event_edition(event), event.get("race_key"))
        for event in merged.values() if event.get("stage_number") is not None
    }
    result = [
        event for event in merged.values()
        if not (
            event.get("stage_number") is None
            and (event_edition(event), event.get("race_key")) in detailed
        )
    ]
    return sorted(result, key=lambda event: (str(event.get("start") or ""), dedupe_identity(event)))


def load_manual_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise ValueError("data/manual_events.json deve contenere un array 'events'")
    validated: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict) or not item.get("race_name") or not item.get("start"):
            raise ValueError("Ogni evento manuale richiede almeno race_name e start")
        event = deepcopy(item)
        event.setdefault("race_key", canonical_race_key(str(event["race_name"])))
        event.setdefault("title", str(event["race_name"]))
        event.setdefault("source", "Manuale")
        event.setdefault("all_day", "T" not in str(event["start"]))
        event.setdefault("end_date", str(event["start"])[:10])
        validated.append(event)
    return validated


def preserve_previous_results(path: Path, events: list[dict[str, Any]]) -> None:
    """Carry confirmed results forward when a result source is temporarily unavailable."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("events") or []
    except (OSError, ValueError, TypeError):
        return
    previous_by_identity = {dedupe_identity(event): event for event in previous}
    for event in events:
        old = previous_by_identity.get(dedupe_identity(event)) or {}
        for field in RESULT_FIELDS:
            if field in old and field not in event:
                event[field] = deepcopy(old[field])


def filter_calendar_window(events: Iterable[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Keep the current cycling year and the next one; drop older editions on 1 January."""
    allowed_years = {str(today.year), str(today.year + 1)}
    return [event for event in events if event_edition(event) in allowed_years]


def _normalized_event(event: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(event)
    result["uid"] = stable_uid(result)
    result.setdefault("all_day", "T" not in str(result.get("start") or ""))
    result.setdefault("end_date", str(result.get("start") or "")[:10])
    result.setdefault("notes", "")
    result.setdefault("broadcast_it", "Da confermare")
    return result


def _format_classification(rows: Any) -> str | None:
    if not isinstance(rows, list) or not rows:
        return None
    formatted = []
    for row in rows[:3]:
        if not isinstance(row, dict) or not row.get("rank") or not row.get("rider"):
            continue
        suffix = f" — {row.get('result')}" if row.get("result") else ""
        formatted.append(f"{row['rank']}. {row['rider']}{suffix}")
    return "; ".join(formatted) or None


def _description(event: dict[str, Any]) -> str:
    fields = (
        ("Corsa", event.get("race_name")),
        ("Tappa", f"{event.get('stage_number')} — {event.get('stage_name', '')}" if event.get("stage_number") else None),
        ("Partenza", event.get("start_location")),
        ("Arrivo", event.get("finish_location")),
        ("Nazione", event.get("country")),
        ("Distanza", event.get("distance")),
        ("Tipologia", event.get("stage_type")),
        ("Categoria", event.get("category")),
        ("Classe UCI", event.get("uci_class")),
        ("Circuito", event.get("circuit")),
        ("Dove vederla in Italia", event.get("broadcast_it")),
        ("Podio di tappa", _format_classification(event.get("stage_podium"))),
        ("Top 3 classifica generale", _format_classification(event.get("general_classification"))),
        ("Fonte risultati", event.get("results_url")),
        ("Note", event.get("notes")),
        ("Fonte ufficiale", event.get("official_url") or event.get("source_url")),
        ("Fonte TV", event.get("broadcast_source_url")),
    )
    return "\n".join(f"{label}: {value}" for label, value in fields if value not in (None, ""))


def build_ical(events: Iterable[dict[str, Any]], generated_at: str) -> bytes:
    calendar = Calendar()
    calendar.add("prodid", "-//Dizzle0987//Cycling Calendar//IT")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", "Cycling Calendar")
    calendar.add("x-wr-timezone", "Europe/Rome")
    calendar.add("url", CALENDAR_URL)
    stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    for item in events:
        component = Event()
        component.add("uid", item["uid"])
        component.add("summary", item["title"])
        component.add("description", _description(item))
        if item.get("location"):
            component.add("location", item["location"])
        if item.get("official_url") or item.get("source_url"):
            component.add("url", item.get("official_url") or item.get("source_url"))
        component.add("dtstamp", stamp)
        component.add("last-modified", stamp)
        if item.get("all_day"):
            start_date = date.fromisoformat(str(item["start"])[:10])
            end_date = date.fromisoformat(str(item.get("end_date") or item["start"])[:10])
            component.add("dtstart", start_date)
            component.add("dtend", end_date + timedelta(days=1))
        else:
            start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=ROME)
            start = start.astimezone(ROME)
            component.add("dtstart", start)
            if item.get("end"):
                end = datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=ROME)
                component.add("dtend", end.astimezone(ROME))
            else:
                component.add("dtend", start + timedelta(hours=4))
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"Tra 2 ore: {item['title']}")
        alarm.add("trigger", timedelta(minutes=-120))
        component.add_component(alarm)
        calendar.add_component(component)
    return calendar.to_ical()


def _content_fingerprint(events: list[dict[str, Any]]) -> str:
    clean = [{k: v for k, v in event.items() if k not in {"last_modified"}} for event in events]
    return hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _previous_generated_at(path: Path, events: list[dict[str, Any]]) -> str | None:
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        old_events = previous.get("events") or []
        if _content_fingerprint(old_events) == _content_fingerprint(events):
            return str(previous.get("generated_at") or "") or None
    except (OSError, ValueError, TypeError):
        pass
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def update_calendar(
    root: Path, session: requests.Session | None = None, today: date | None = None,
) -> list[dict[str, Any]]:
    today = today or datetime.now(ROME).date()
    active_session = session or build_session()
    result = fetch_remote_events(active_session, today.year)
    for error in result.errors:
        LOGGER.warning("%s", error)
    if not result.successful_sources:
        raise UpdateError("Tutte le fonti remote hanno fallito: output esistenti conservati")
    manual = load_manual_events(root / "data" / "manual_events.json")
    remote_events = deduplicate(result.events)
    events_path = root / "data" / "events.json"
    preserve_previous_results(events_path, remote_events)
    result.errors.extend(enrich_with_uci_results(
        active_session,
        remote_events,
        result.events,
        today,
        backfill=os.getenv("CYCLING_RESULTS_BACKFILL") == "1",
    ))
    combined = filter_calendar_window(deduplicate([*remote_events, *manual]), today)
    events = [_normalized_event(event) for event in combined]
    if not events:
        raise UpdateError("Le fonti non hanno prodotto eventi validi: output esistenti conservati")
    generated_at = _previous_generated_at(events_path, events) or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    competitions = sorted({str(event.get("race_name") or "") for event in events if event.get("race_name")})
    payload = {
        "generated_at": generated_at,
        "timezone": "Europe/Rome",
        "event_count": len(events),
        "competitions": competitions,
        "successful_sources": result.successful_sources,
        "source_errors": result.errors,
        "events": events,
    }
    json_bytes = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    ical_bytes = build_ical(events, generated_at)
    _atomic_write(events_path, json_bytes)
    _atomic_write(root / "calendar.ics", ical_bytes)
    return events
