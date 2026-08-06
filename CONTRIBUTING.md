# Contribuire

Grazie per contribuire a Cycling Calendar.

## Segnalare o correggere una gara

Apri una issue indicando fonte ufficiale, URL, edizione, tappa e campo da correggere. Per una pull request, usa `data/manual_events.json` quando la fonte automatica non espone ancora il dato; conserva `race_key` e `stage_number` per mantenere l'UID.

Non inserire dati dedotti, indiscrezioni non confermate, link pirata o dati provenienti da SofaScore. Per orari, percorso e copertura TV allega una fonte pubblica affidabile, preferibilmente UCI, organizzatore, federazione o broadcaster.

## Sviluppo

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
python update_calendar.py
```

Verifica che `calendar.ics` sia ancora leggibile da `icalendar.Calendar.from_ical`, che gli UID non cambino per semplici variazioni di data/ora e che un fallimento totale non tocchi gli output esistenti.

Le pull request devono essere piccole, motivate e non includere modifiche non correlate.

