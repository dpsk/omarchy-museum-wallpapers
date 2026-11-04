# Changelog

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
