# Changelog

## 0.1.4 — 2026-11-06

- Prefer paintings described in your language: a new setting shows those with
  a title (or a Wikipedia article) in the chosen language first, only, or
  with no preference; on by default ("prefer").
- The card is about the painting: settings moved to their own panel, opened
  with a right click on the bar icon, the gear in the card, or
  `omarchy-shell museum-wallpapers settings`; the card shows a longer story
  (up to six sentences).

## 0.1.3 — 2026-11-06

- The interface speaks ten languages: English, Русский, Українська, Deutsch, Français, Español, Italiano, Nederlands, Polski and Português. The popup,
  notifications and messages follow the `language` setting, which now
  defaults to `auto` (the system language) and can be picked in the popup.
  Wikipedia titles and stories still come in any language Wikipedia has.

## 0.1.2 — 2026-11-06

- Next is fast: paintings are downloaded two at a time, framed ahead of time,
  and a Next with an empty queue waits for the download already under way
  instead of starting another. Thumbnails are requested at the standard
  widths Wikimedia keeps ready (1280, 1920, 3840 px), which avoids slow,
  rate-limited on-demand renders. Candidates are checked by size alone, and
  Commons' file description is fetched only for the painting picked, which
  makes a search round about 20 times faster (2 s instead of 30 s).
- A framed painting is re-framed within a minute when the screen size changes
  (a docked or undocked laptop), paintings framed ahead are re-framed for the
  new size, and the previous one is kept while the background fades.
- No wait on first run: the painter lists for `fame: "famous"` and `"known"`
  now ship with the plugin and are refreshed weekly by a detached helper
  (`museum.py refresh-pools`).
- `prefetchCount` now defaults to 6.
- Painting details load in parallel; titles lose invisible and odd space
  characters; long titles are cut on a word boundary; a scan or upload date
  on Commons is no longer shown as the painting's year.
- Stage timings go to the journal, including the background downloads:
  `journalctl --user | grep "museum-wallpapers: timing"`.

## 0.1.1 — 2026-11-04

- A filter change no longer cuts off a painting that is being downloaded.
- Two quick Save clicks no longer save the painting twice or lose an entry
  from the collection index.

## 0.1.0 — 2026-11-04

- Endless public-domain paintings listed on Wikidata and shown from their
  Wikimedia Commons scans (many from the Google Art Project), landscape-only,
  sized for the largest connected monitor, with a queue of `prefetchCount`
  (default 3) downloaded ahead so Next is instant.
- Painting details from Wikidata and Wikipedia: title, artist, year, museum,
  movement, and a short summary in a configurable language.
- Optional ways to show them: bar widget with a popup card and a desktop
  placard (both on by default), notification on change (off by default), IPC
  for hotkeys.
- Filters by years, artists, movements and genres, plus temporary
  "More by …" / "More …" filters from the popup.
- `fame`: famous painters only by default (~280, by Wikipedia coverage), or
  ~1200 known painters, or everyone; settable from the popup.
- `fit` for paintings that are not 16:9 (also in the popup): blurred frame (default), auto, dark
  wall, or fill and crop.
- Change interval in settings or from the popup; Save copies a painting into a
  local folder, which doubles as the offline fallback.
