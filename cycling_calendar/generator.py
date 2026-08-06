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
    "2026-uci-road-world-championships": "uci-road-world-championships",
    "uec-road-european-championships-2026": "uec-road-european-championships",
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
    return RACE_ALIASES.get(key, key)


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
                    "race_name": name,
                    "title": name,
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
    for race_class in UCI_CLASSES:
        class_events: list[dict[str, Any]] = []
        class_ok = False
        for period in ("past", "upcoming"):
            try:
                response = session.get(
                    UCI_API.format(period=period),
                    params={"discipline": "ROA", "raceCategory": "ME", "raceClass": race_class, "year": year},
                    timeout=25,
                )
                response.raise_for_status()
                class_events.extend(parse_uci_calendar(response.json(), race_class))
                class_ok = True
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"UCI {race_class}/{period}: {exc}")
        if class_ok:
            successes.append(f"UCI {race_class}")
            events.extend(class_events)

    for config in GRAND_TOURS:
        try:
            response = session.get(config["url"], timeout=30)
            response.raise_for_status()
            parsed = (
                parse_aso_route_html(response.text, config)
                if config["parser"] == "aso" else parse_giro_route_html(response.text, config)
            )
            if not parsed:
                raise ValueError("nessuna tappa riconosciuta")
            events.extend(parsed)
            successes.append(f"Sito ufficiale {config['name']}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{config['name']}: {exc}")
    return FetchResult(events, successes, errors)


def event_identity(event: dict[str, Any]) -> str:
    race_key = event.get("race_key") or canonical_race_key(str(event.get("race_name") or event.get("title") or ""))
    stage = event.get("stage_number")
    suffix = f"stage-{int(stage):02d}" if stage not in (None, "") else "race"
    return f"{race_key}|{suffix}"


def stable_uid(event: dict[str, Any]) -> str:
    explicit = str(event.get("uid") or "").strip()
    if explicit:
        return explicit
    year = str(event.get("edition") or str(event.get("start") or "")[:4])
    digest = hashlib.sha256(f"{year}|{event_identity(event)}".encode()).hexdigest()[:24]
    return f"{digest}@cycling-calendar"


def deduplicate(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    priority = {"UCI": 10, "manuale": 100}
    for original in events:
        event = deepcopy(original)
        key = event_identity(event)
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
    detailed = {event.get("race_key") for event in merged.values() if event.get("stage_number") is not None}
    result = [
        event for event in merged.values()
        if not (event.get("stage_number") is None and event.get("race_key") in detailed)
    ]
    return sorted(result, key=lambda event: (str(event.get("start") or ""), event_identity(event)))


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


def _normalized_event(event: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(event)
    result["uid"] = stable_uid(result)
    result.setdefault("all_day", "T" not in str(result.get("start") or ""))
    result.setdefault("end_date", str(result.get("start") or "")[:10])
    result.setdefault("notes", "")
    result.setdefault("broadcast_it", "Da confermare")
    return result


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
    result = fetch_remote_events(session or build_session(), today.year)
    for error in result.errors:
        LOGGER.warning("%s", error)
    if not result.successful_sources:
        raise UpdateError("Tutte le fonti remote hanno fallito: output esistenti conservati")
    manual = load_manual_events(root / "data" / "manual_events.json")
    events = [_normalized_event(event) for event in deduplicate([*result.events, *manual])]
    if not events:
        raise UpdateError("Le fonti non hanno prodotto eventi validi: output esistenti conservati")
    events_path = root / "data" / "events.json"
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
