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
    fetch_remote_events,
    parse_aso_route_html,
    parse_giro_route_html,
    parse_uci_calendar,
    parse_uci_podium,
    parse_uci_result_index,
    preserve_previous_results,
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


def test_parse_official_uci_result_metadata_and_podium() -> None:
    props = json.dumps({"results": {"accordion": [{
        "label": "Stage 4",
        "results": [{"title": "Stage Classification", "eventCode": "D2EV1", "raceType": "A"}],
    }]}})
    groups = parse_uci_result_index(
        f"<div data-component='CompetitionDetailsModule' data-props='{props}'></div>"
    )
    assert groups[0]["results"][0]["eventCode"] == "D2EV1"
    podium = parse_uci_podium({"results": [
        {"headerType": "rider", "values": {"rank": "1", "firstname": "Tadej", "lastname": "POGAČAR", "team": "UAE", "result": "4:20:10"}},
        {"headerType": "rider", "values": {"rank": "2", "firstname": "Remco", "lastname": "EVENEPOEL", "team": "RBH", "result": "00:00:04"}},
        {"headerType": "rider", "values": {"rank": "3", "firstname": "Jonas", "lastname": "VINGEGAARD", "team": "TVL", "result": "00:00:00"}},
    ]})
    assert [row["rider"] for row in podium] == ["Tadej POGAČAR", "Remco EVENEPOEL", "Jonas VINGEGAARD"]
    assert podium[1]["result"] == "+4''"
    assert podium[2]["result"] == "stesso tempo"
    absolute_times = parse_uci_podium({"results": [
        {"headerType": "rider", "values": {"rank": "1", "firstname": "One", "result": "04:15:25"}},
        {"headerType": "rider", "values": {"rank": "2", "firstname": "Two", "result": "04:15:25"}},
        {"headerType": "rider", "values": {"rank": "3", "firstname": "Three", "result": "04:15:29"}},
    ]})
    assert absolute_times[1]["result"] == "stesso tempo"
    assert absolute_times[2]["result"] == "+4''"
    zero_gap = parse_uci_podium({"results": [
        {"headerType": "rider", "values": {"rank": "1", "firstname": "One", "result": "1:00:00"}},
        {"headerType": "rider", "values": {"rank": "2", "firstname": "Two", "result": "+00"}},
        {"headerType": "rider", "values": {"rank": "3", "firstname": "Three", "result": "+0"}},
    ]})
    assert zero_gap[1]["result"] == zero_gap[2]["result"] == "stesso tempo"


def test_fetch_includes_current_and_next_uci_season(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_years: list[int] = []

    class EmptyResponse:
        text = ""

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"items": []}

    class RecordingSession:
        def get(self, url: str, **kwargs: object) -> EmptyResponse:
            requested_years.append(int(dict(kwargs.get("params") or {}).get("year", 0)))
            return EmptyResponse()

    monkeypatch.setattr("cycling_calendar.generator.GRAND_TOURS", ())
    result = fetch_remote_events(RecordingSession(), 2026)  # type: ignore[arg-type]
    assert set(requested_years) == {2026, 2027}
    assert not result.errors


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


def test_ical_description_contains_compact_results() -> None:
    event = {
        "uid": "result@cycling-calendar", "title": "Tappa 1", "race_name": "Test",
        "start": "2026-08-22", "all_day": True,
        "stage_podium": [
            {"rank": "1", "rider": "Rider One", "result": "4:00:00"},
            {"rank": "2", "rider": "Rider Two", "result": "+4''"},
            {"rank": "3", "rider": "Rider Three", "result": "+8''"},
        ],
        "general_classification": [
            {"rank": "1", "rider": "Leader One", "result": "20:00:00"},
            {"rank": "2", "rider": "Leader Two", "result": "+30''"},
            {"rank": "3", "rider": "Leader Three", "result": "+45''"},
        ],
        "results_url": "https://www.uci.org/results",
    }
    calendar = Calendar.from_ical(build_ical([event], "2026-08-22T18:00:00Z"))
    parsed = next(component for component in calendar.walk() if component.name == "VEVENT")
    description = str(parsed["description"])
    assert "Podio di tappa: 1. Rider One" in description
    assert "Top 3 classifica generale: 1. Leader One" in description
    assert "Fonte risultati: https://www.uci.org/results" in description


def test_previous_results_survive_source_failure(tmp_path: Path) -> None:
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"events": [{
        "race_key": "tour-test", "race_name": "Tour Test", "start": "2026-08-22",
        "stage_number": 1, "stage_podium": [{"rank": "1", "rider": "Winner"}],
        "results_source": "UCI", "results_url": "https://uci.example/results",
    }]}), encoding="utf-8")
    current = [{
        "race_key": "tour-test", "race_name": "Tour Test", "start": "2026-08-22",
        "stage_number": 1,
    }]
    preserve_previous_results(path, current)
    assert current[0]["stage_podium"][0]["rider"] == "Winner"
    assert current[0]["results_url"] == "https://uci.example/results"


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
