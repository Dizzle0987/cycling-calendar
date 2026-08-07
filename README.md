# Cycling Calendar

Calendario iCalendar pubblico e sottoscrivibile dedicato alle principali corse maschili professionistiche su strada: UCI WorldTour, Grandi Giri, Monumento, classiche, semiclassiche, principali corse a tappe e campionati.

Ogni aggiornamento interroga automaticamente la stagione corrente e quella successiva. Il passaggio di anno non richiede modifiche al codice: le nuove edizioni vengono aggiunte appena compaiono nel calendario UCI o nei siti ufficiali, mantenendo separate le edizioni tramite UID stabili. I nomi di Mondiali ed Europei restano generici e non incorporano l'anno dell'edizione.

- Pagina: <https://dizzle0987.github.io/cycling-calendar/>
- Feed HTTPS: <https://dizzle0987.github.io/cycling-calendar/calendar.ics>
- Sottoscrizione: <webcal://dizzle0987.github.io/cycling-calendar/calendar.ics>

Il progetto è indipendente e non affiliato all'UCI. Non usa SofaScore.

## Sottoscrizione

### iPhone e iPad

Apri la [pagina pubblica](https://dizzle0987.github.io/cycling-calendar/) in Safari e tocca **Sottoscrivi al calendario**. Se il pulsante non si apre, vai in **Impostazioni → App → Calendario → Account calendario → Aggiungi account → Altro → Aggiungi calendario con sottoscrizione** e incolla il link HTTPS del feed.

### Android

Google Calendar per Android non aggiunge sempre un calendario da URL direttamente. Apri Google Calendar da browser desktop con lo stesso account, scegli **Altri calendari → + → Da URL**, incolla il link HTTPS e abilita poi il calendario nell'app Android.

### Google Calendar

Da <https://calendar.google.com>, scegli **Altri calendari → + → Da URL** e incolla:

```text
https://dizzle0987.github.io/cycling-calendar/calendar.ics
```

Una sottoscrizione, diversamente da un'importazione statica, riceve gli aggiornamenti senza creare un nuovo calendario.

## Fonti e affidabilità

La pipeline privilegia fonti pubbliche e strutturate:

1. **UCI — fonte primaria di scoperta**: endpoint JSON pubblico del calendario Road, filtrato per categoria Men Elite e classi `1.UWT`, `2.UWT`, `1.Pro`, `2.Pro`, `CM`, `CC` e `CN`. Fornisce nome ufficiale, intervallo di date, nazione, classe e identificativo della competizione.
2. **Siti ufficiali Giro d'Italia, Tour de France e La Vuelta — dettaglio tappe**: le tabelle di percorso pubblicate dagli organizzatori forniscono numero, data, partenza, arrivo, distanza e tipologia. Ogni tappa diventa un evento distinto.
3. **Fallback**: se il dettaglio di un Grande Giro non è leggibile ma UCI risponde, rimane l'evento complessivo UCI della corsa; se una sola chiamata UCI fallisce, le altre classi e i siti ufficiali continuano a essere usati.
4. **Correzioni manuali**: `data/manual_events.json` integra o sovrascrive i dati remoti usando la stessa identità stabile.
5. **Ultimo output valido**: se tutte le fonti remote falliscono, lo script termina prima di scrivere. `calendar.ics` e `data/events.json` restano intatti.

Le pagine ufficiali dei Grandi Giri non espongono sempre gli orari nella tabella generale. In quel caso la tappa viene pubblicata correttamente come evento di giornata intera con una nota “orario da confermare”; non vengono dedotti orari da velocità media, palinsesti o edizioni precedenti. Le pagine dei singoli organizzatori sono volutamente un arricchimento: l'indice UCI resta il fallback stabile se il loro markup cambia.

Per la copertura italiana vengono usati comunicati o palinsesti espliciti. Nel feed iniziale:

- Giro, Tour e Vuelta riportano Eurosport e le piattaforme Warner Bros. Discovery confermate per il 2026;
- i Mondiali riportano l'accordo UCI–EBU per la RAI e la copertura Warner Bros. Discovery;
- le altre gare indicano la piattaforma solo se la disponibilità è sufficientemente verificata; altrimenti compare **Da confermare**.

Non vengono inventati canali, orari, percorsi o distanze.

## Copertura

Sono inclusi automaticamente tutti gli eventi `1.UWT` e `2.UWT`, quindi i tre Grandi Giri e le cinque Monumento, più una selezione esplicita di corse `1.Pro` e `2.Pro` di grande rilevanza. Sono inclusi inoltre Mondiali UCI, Europei UEC e Campionati italiani maschili presenti nel calendario UCI. La selezione ProSeries è conservata nel codice e può essere estesa tramite pull request.

## UID e deduplicazione

L'UID è un hash dell'edizione, dell'identità canonica della corsa e del numero di tappa. Data, ora, località, distanza, fonte e copertura TV non ne fanno parte. Un cambio di orario o percorso aggiorna quindi l'evento già sottoscritto.

Gli alias normalizzano i nomi alternativi pubblicati dall'UCI. Gli eventi con la stessa coppia `race_key + stage_number` vengono unificati; i dettagli ufficiali di tappa prevalgono sull'evento generale UCI e i dati manuali prevalgono su tutti. Quando sono disponibili le tappe, l'evento complessivo della stessa corsa viene rimosso per evitare doppioni.

## Eventi senza orario

Gli orari ISO 8601 con offset vengono convertiti in `Europe/Rome`. Un evento con sola data usa `VALUE=DATE` e non riceve un finto orario. Tutti gli eventi includono un `VALARM` 120 minuti prima; per un evento di giornata intera il client applica il promemoria rispetto all'inizio della giornata.

## Correzioni manuali

Modifica `data/manual_events.json`:

```json
{
  "events": [
    {
      "race_key": "vuelta-a-espana",
      "race_name": "La Vuelta a España",
      "stage_number": 7,
      "start": "2026-08-28T12:45:00+02:00",
      "end": "2026-08-28T17:20:00+02:00",
      "broadcast_it": "Eurosport 1 e discovery+",
      "broadcast_source_url": "https://example.org/fonte-verificabile",
      "notes": "Orari confermati dall'organizzatore"
    }
  ]
}
```

Per correggere un evento esistente conserva `race_key` e `stage_number`; così l'UID non cambia. `race_name` e `start` sono obbligatori. Usa una data `YYYY-MM-DD` se l'orario non è confermato oppure un timestamp con offset. È possibile indicare un `uid` esplicito solo per casi eccezionali.

## Esecuzione locale

Richiede Python 3.12 o successivo.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
python update_calendar.py
```

Output:

- `data/events.json`: snapshot normalizzato per debug, con fonti, errori parziali, UID e metadati;
- `calendar.ics`: feed iCalendar validato dalla suite;
- `index.html`: pagina mobile-first che legge conteggio, competizioni e ultimo aggiornamento dallo snapshot.

La scrittura dei due output è atomica. Se i dati non cambiano, `generated_at` rimane stabile: l'automazione non crea commit vuoti o aggiornamenti puramente cosmetici.

## Automazione e Pages

`update.yml` parte ogni 6 ore e manualmente. Usa un gruppo di concorrenza, esegue prima i test, genera gli output e committa soltanto se `calendar.ics` o `data/events.json` cambiano. I permessi sono limitati a `contents: write` per quel job.

`pages.yml` pubblica un artifact statico tramite GitHub Pages con soli permessi `contents: read`, `pages: write` e `id-token: write`. Include pagina, feed e snapshot JSON. Il deployment si avvia su ogni push rilevante o manualmente.

## Test

La suite verifica parsing UCI e percorsi ufficiali, UID stabili dopo cambi di data, deduplicazione, precedenza manuale, soppressione dell'evento generale quando esistono tappe, `Europe/Rome`, eventi senza orario, allarme a 120 minuti, validità iCalendar, UID unici, pagina di sottoscrizione e conservazione degli output in caso di fallimento totale.

## Licenza e contributi

Codice sotto licenza MIT. Consulta [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) e il [Codice di condotta](CODE_OF_CONDUCT.md).
