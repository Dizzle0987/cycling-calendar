from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from icalendar import Calendar

from cycling_calendar.generator import (
    FetchResult,
    GRAND_TOURS,
    UpdateError,
    build_ical,
    deduplicate,
    parse_aso_route_html,
    parse_giro_route_html,
    parse_uci_calendar,
    stable_uid,
    update_calendar,
)


def uci_payload(name: str = "Milano-Sanremo", dates: str = "21 Mar 2026") -> dict:
    return {
        "items": [{
            "items": [{
                "items": [{
                    "name": name,
                    "country": "ITA",
                    "dates": dates,
                    "detailsLink": {"url": "/competition-details/2026/ROA/76902"},
                }]
            }]
        }]
    }


def test_uci_structured_json_and_stable_uid_after_date_change() -> None:
    first = parse_uci_calendar(uci_payload(), "1.UWT")[0]
    moved = parse_uci_calendar(uci_payload(dates="22 Mar 2026"), "1.UWT")[0]
    assert first["source"] == "UCI"
    assert first["race_key"] == "milano-sanremo"
    assert stable_uid(first) == stable_uid(moved)


def test_championship_names_do_not_expose_edition_year() -> None:
    worlds = parse_uci_calendar(
        uci_payload("2027 UCI Road World Championships", "19 Sep 2027"), "CM"
    )[0]
    europeans = parse_uci_calendar(
        uci_payload("UEC Road European Championships 2027", "03 Oct 2027"), "CC"
    )[0]
    assert worlds["race_name"] == "UCI Road World Championships"
    assert worlds["race_key"] == "uci-road-world-championships"
    assert europeans["race_name"] == "UEC Road European Championships"
    assert europeans["race_key"] == "uec-road-european-championships"


def test_uci_date_range_uses_inclusive_end() -> None:
    event = parse_uci_calendar(uci_payload("Giro d'Italia", "08 May - 31 May 2026"), "2.UWT")[0]
    assert event["start"] == "2026-05-08"
    assert event["end_date"] == "2026-05-31"


def test_parse_aso_official_route_table() -> None:
    html = """
    <table><tr><td>1</td><td>Mountain</td><td>Sat 07/04/2026</td>
    <td>Barcelona &gt; Montserrat</td><td>190.5 km</td><td><a href='/en/stage-1'>Stage 1</a></td></tr></table>
    """
    events = parse_aso_route_html(html, GRAND_TOURS[1])
    assert len(events) == 1
    assert events[0]["stage_number"] == 1
    assert events[0]["stage_type"] == "montagna"
    assert events[0]["official_url"].endswith("/en/stage-1")


def test_parse_giro_official_cards() -> None:
    html = """
    <div class='row single-tappa' id='tappa-10' data-tipologia='crono'>
      <div>Tue. 19/05/2026</div><span class='partenza-value'>Viareggio</span> -
      <span class='arrivo-value'>Massa TUDOR ITT</span><span class='distanza-value'>42,0</span>
      <a href='/en/tappe/stage-10-of-the-giro-ditalia-2026/'></a>
    </div>
    """
    event = parse_giro_route_html(html, GRAND_TOURS[0])[0]
    assert event["distance"] == "42.0 km"
    assert event["stage_type"] == "cronometro"
    assert event["start"] == "2026-05-19"


def test_deduplication_suppresses_race_overview_when_stages_exist() -> None:
    overview = {"race_key": "tour-de-france", "race_name": "Tour de France", "start": "2026-07-04"}
    stage = {**overview, "stage_number": 1, "title": "Tappa 1"}
    assert deduplicate([overview, stage]) == [stage]


def test_deduplication_keeps_different_editions() -> None:
    editions = [
        {"race_key": "milano-sanremo", "race_name": "Milano-Sanremo", "start": "2026-03-21"},
        {"race_key": "milano-sanremo", "race_name": "Milano-Sanremo", "start": "2027-03-20"},
    ]
    result = deduplicate(editions)
    assert len(result) == 2
    assert stable_uid(result[0]) != stable_uid(result[1])


def test_manual_override_wins_without_duplicate() -> None:
    remote = {"race_key": "strade-bianche", "race_name": "Strade Bianche", "start": "2026-03-07", "source": "UCI", "broadcast_it": "Da confermare"}
    manual = {**remote, "source": "Manuale", "broadcast_it": "Rai Sport", "notes": "Correzione verificata"}
    events = deduplicate([remote, manual])
    assert len(events) == 1
    assert events[0]["broadcast_it"] == "Rai Sport"


def test_ical_timezone_alarm_and_all_day_handling() -> None:
    timed = {
        "uid": "timed@cycling-calendar", "title": "Tappa con orario", "race_name": "Test",
        "start": "2026-08-22T12:30:00+02:00", "all_day": False,
    }
    all_day = {
        "uid": "all-day@cycling-calendar", "title": "Orario da confermare", "race_name": "Test 2",
        "start": "2026-08-23", "end_date": "2026-08-23", "all_day": True,
    }
    raw = build_ical([timed, all_day], "2026-08-06T10:00:00Z")
    calendar = Calendar.from_ical(raw)
    parsed = [component for component in calendar.walk() if component.name == "VEVENT"]
    assert len(parsed) == 2
    assert getattr(parsed[0].decoded("dtstart").tzinfo, "key", None) == "Europe/Rome"
    assert parsed[1].decoded("dtstart") == date(2026, 8, 23)
    assert all(next(c for c in event.subcomponents if c.name == "VALARM").decoded("trigger").total_seconds() == -7200 for event in parsed)
    assert b"X-WR-TIMEZONE:Europe/Rome" in raw


def test_update_failure_preserves_previous_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "manual_events.json").write_text('{"events": []}', encoding="utf-8")
    events_path = tmp_path / "data" / "events.json"
    calendar_path = tmp_path / "calendar.ics"
    events_path.write_text("OLD JSON\n", encoding="utf-8")
    calendar_path.write_text("OLD ICS\n", encoding="utf-8")
    monkeypatch.setattr("cycling_calendar.generator.fetch_remote_events", lambda session, year: FetchResult([], [], ["offline"]))
    with pytest.raises(UpdateError):
        update_calendar(tmp_path, session=object(), today=date(2026, 8, 6))
    assert events_path.read_text() == "OLD JSON\n"
    assert calendar_path.read_text() == "OLD ICS\n"


def test_unchanged_data_keeps_generated_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "manual_events.json").write_text('{"events": []}', encoding="utf-8")
    source = parse_uci_calendar(uci_payload(), "1.UWT")
    monkeypatch.setattr("cycling_calendar.generator.fetch_remote_events", lambda session, year: FetchResult(source, ["UCI"], []))
    update_calendar(tmp_path, session=object(), today=date(2026, 8, 6))
    first = json.loads((tmp_path / "data" / "events.json").read_text())
    update_calendar(tmp_path, session=object(), today=date(2026, 8, 6))
    second = json.loads((tmp_path / "data" / "events.json").read_text())
    assert first["generated_at"] == second["generated_at"]


def test_generated_feed_has_unique_uids_and_valid_calendar() -> None:
    root = Path(__file__).parents[1]
    data = json.loads((root / "data" / "events.json").read_text(encoding="utf-8"))
    calendar = Calendar.from_ical((root / "calendar.ics").read_bytes())
    uid_values = [str(component["uid"]) for component in calendar.walk() if component.name == "VEVENT"]
    assert len(uid_values) == data["event_count"]
    assert len(uid_values) == len(set(uid_values))
    assert all(uid.endswith("@cycling-calendar") for uid in uid_values)


def test_mobile_page_has_subscription_and_manual_fallback() -> None:
    html = (Path(__file__).parents[1] / "index.html").read_text(encoding="utf-8")
    assert "webcal://dizzle0987.github.io/cycling-calendar/calendar.ics" in html
    assert "Google Calendar" in html and "Android" in html and "iPhone" in html
    assert "navigator.clipboard.writeText(calendarUrl)" in html
