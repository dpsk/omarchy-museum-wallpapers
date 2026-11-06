#!/usr/bin/env python3
"""Museum Wallpapers helper: endless painting wallpapers from Wikimedia Commons.

Finds a random high-resolution, landscape-oriented painting on Wikimedia
Commons, sets it as the Omarchy background with omarchy-theme-bg-set, and
records what it is (title, artist, year, museum, a Wikipedia summary) so the
shell plugin can show it. Only the Python standard library is used.

Commands:
  tick                  switch if the rotation interval has passed; print status
  next                  switch to the next painting now (prefetched, instant)
  prefetch              download the next painting without switching
  refresh-pools         rebuild the cached painter lists from Wikidata
  status                print the status JSON the shell plugin renders
  notify                show the current painting as a desktop notification
  open                  open the current painting's article in the browser
  keep                  copy the current painting into saveDir
  filter set [--artist NAME] [--movement NAME] [--genre NAME] [--years FROM-TO]
  filter clear          drop the temporary filter set from the popup

Settings live in the plugin's entry in ~/.config/omarchy/shell.json.
"""

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN_ID = "io.github.dpsk.museum-wallpapers"
VERSION = "0.1.4"
USER_AGENT = f"omarchy-museum-wallpapers/{VERSION} (https://github.com/dpsk/omarchy-museum-wallpapers)"

COMMONS_API = os.environ.get("MUSEUM_WALLPAPERS_COMMONS_API", "https://commons.wikimedia.org/w/api.php")
WIKIDATA_API = os.environ.get("MUSEUM_WALLPAPERS_WIKIDATA_API", "https://www.wikidata.org/w/api.php")
SPARQL_API = os.environ.get("MUSEUM_WALLPAPERS_SPARQL_API", "https://query.wikidata.org/sparql")

PAINTING = "Q3305213"
# Google Art Project uploads are clean, frameless museum scans from hundreds
# of collections; they are the pool for random picks with `fame: any` and no
# other filter.
RANDOM_SEARCH = 'intitle:"Google Art Project" filew:>{width} filetype:bitmap'
# Commons serves thumbnails up to this width; larger requests are capped.
MAX_THUMB_WIDTH = 3840
# Thumbnail widths Wikimedia's CDN keeps ready ($wgThumbnailSteps, the large
# end); other widths are rendered on demand, slowly, and rate limited.
THUMB_STEPS = (1280, 1920, 3840)
# Paintings downloaded at the same time while the queue fills.
PREFETCH_WORKERS = 2
# A claimed painting with no image yet counts as downloading for this long
# when another process claimed it (a killed process must not hold anything up).
IN_FLIGHT_AGE = 120
# Commons also hosts tiles cut from very large scans, e.g. "…-x0-y2.jpg".
TILE_RE = re.compile(r"-x\d+-y\d+\.\w+$", re.IGNORECASE)
POOL_MAX_AGE = 7 * 24 * 3600
# After a failed background refresh of a painter list, wait this long.
REFRESH_RETRY = 600
# Paintings shown are public domain: a date after this on the file page is
# the photograph's or the upload's (e.g. "10 April 2008"), not the painting's.
LATEST_PAINTING_YEAR = 1950
SEARCH_ROUNDS = 6
# A filtered pool this small is checked whole in one round.
POOL_SAMPLE = 150
# A queue sidecar without its image this old belongs to a killed download.
ORPHAN_AGE = 1800
# After a failed prefetch the tick waits this long before trying again.
PREFETCH_BACKOFF = 600

GENRES = {
    "landscape": "Q191163",
    "portrait": "Q134307",
    "still life": "Q170571",
    "marine": "Q158607",
    "seascape": "Q158607",
    "genre": "Q1047337",
    "history": "Q742333",
    "religious": "Q2864737",
    "cityscape": "Q1935974",
}

DEFAULTS = {
    "intervalMinutes": 60,
    # "auto" follows the system language (LC_ALL / LC_MESSAGES / LANG).
    "language": "auto",
    "filters": {"yearFrom": None, "yearTo": None, "artists": [], "movements": [], "genres": []},
    "notifyOnChange": False,
    "placard": "bottom-right",
    "saveDir": "~/Pictures/Wallpapers/Museum",
    "cacheSize": 24,
    "prefetchCount": 6,
    "fit": "blur",
    "fame": "famous",
    # Paintings with a title (or a Wikipedia article) in the chosen language:
    # shown first ("prefer"), exclusively ("only"), or no preference ("all").
    "localized": "prefer",
    "localizedBy": "title",
    "minRatio": 1.3,
    "maxRatio": 2.0,
}
PLACARDS = {"off", "bottom-left", "bottom-right", "top-left", "top-right"}
# How a painting whose shape differs from the screen is shown: framed by a
# blurred copy of itself, on a dark wall, filling the screen (cropped), or
# blurred only when filling would crop more than AUTO_CROP of it.
FITS = {"blur", "auto", "wall", "cover"}
AUTO_CROP = 0.10
# Fame of a painter = how many Wikipedia language editions have an article on
# them (Wikidata sitelinks): Leonardo ~300, Rembrandt ~200. "famous" is about
# the 280 most-covered painters, "known" about 1200.
FAME = {"any": 0, "known": 30, "famous": 60}
PAINTER = "Q1028181"
WALL_COLOR = "#1c1b19"

HOME = Path.home()
SHELL_JSON = Path(os.environ.get("MUSEUM_WALLPAPERS_SHELL_JSON", HOME / ".config/omarchy/shell.json"))
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "museum-wallpapers"
# Resolved, because omarchy-theme-bg-set links the realpath of the image and
# comparisons with the current background must survive a symlinked ~/.cache.
CACHE = Path(os.path.realpath(Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "museum-wallpapers"))
QUEUE = CACHE / "queue"
# Framed copies of the current painting; the downloaded original is kept as is.
DISPLAY = CACHE / "display"
CURRENT_LINK = HOME / ".local/state/omarchy/current/background"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


# Interface text (strings.json, next to this file): popup, notifications and
# messages follow the `language` setting; a language without a table gets
# English, while Wikipedia titles and stories still come in that language.
def load_strings():
    """The interface tables; English labels from the code (via t()'s fallback
    to the key) if strings.json is missing or broken, rather than no plugin."""
    try:
        tables = json.loads((Path(__file__).resolve().parent / "strings.json").read_text(encoding="utf-8"))
        if isinstance(tables, dict) and isinstance(tables.get("en"), dict):
            return tables
    except (OSError, ValueError) as exc:
        print(f"museum-wallpapers: strings.json unreadable, interface in English keys: {exc}", file=sys.stderr)
    return {"en": {}}


STRINGS = load_strings()
# The interface languages, in their own names (BarWidget.qml offers the same list).
UI_LANGUAGES = {
    "en": "English", "ru": "Русский", "uk": "Українська", "de": "Deutsch", "fr": "Français",
    "es": "Español", "it": "Italiano", "nl": "Nederlands", "pl": "Polski", "pt": "Português",
}


def system_language():
    """The language of the session's locale, as a Wikipedia code ("en" if unknown)."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if not value:
            continue
        # The first variable set decides, as in POSIX; C or POSIX means English.
        code = re.match(r"[a-z]{2,3}(?=[_.@]|$)", value.lower())
        return code.group(0) if code and value not in ("C", "POSIX") and not value.startswith("C.") else "en"
    return "en"


def ui_strings(language):
    """The interface text for a language, English where it has none."""
    return {**STRINGS["en"], **STRINGS.get(str(language).split("-")[0], {})}


def t(key, language="en", **values):
    text = ui_strings(language).get(key, key)
    return text.format(**values) if values else text


class NoMatch(Exception):
    """The active filter matched no usable painting."""


class OnlyCurrent(NoMatch):
    """The only painting matching the filter is the one already showing."""


class AllQueued(NoMatch):
    """Every painting matching the filter is already queued or showing."""


# ----------------------------------------------------------------- config


def config_entry(text, plugin_id=PLUGIN_ID):
    """Settings from the plugin's `plugins` entry, overlaid with its bar
    widget entry (which the Omarchy widget settings editor writes)."""
    try:
        parsed = json.loads(text or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    entries = parsed.get("plugins") if isinstance(parsed.get("plugins"), list) else []
    layout = parsed.get("bar", {}).get("layout", {}) if isinstance(parsed.get("bar"), dict) else {}
    for section in ("left", "center", "right"):
        widgets = layout.get(section) if isinstance(layout, dict) else None
        entries = entries + (widgets if isinstance(widgets, list) else [])
    merged = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == plugin_id:
            merged.update(entry)
    return merged


def as_list(value):
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def as_year(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_filters(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "yearFrom": as_year(raw.get("yearFrom")),
        "yearTo": as_year(raw.get("yearTo")),
        "artists": as_list(raw.get("artists")),
        "movements": as_list(raw.get("movements")),
        "genres": as_list(raw.get("genres")),
    }


def normalize_config(entry):
    config = {key: entry.get(key, default) for key, default in DEFAULTS.items()}
    try:
        config["intervalMinutes"] = max(0, int(config["intervalMinutes"]))
    except (TypeError, ValueError):
        config["intervalMinutes"] = DEFAULTS["intervalMinutes"]
    try:
        config["cacheSize"] = max(2, int(config["cacheSize"]))
    except (TypeError, ValueError):
        config["cacheSize"] = DEFAULTS["cacheSize"]
    try:
        config["prefetchCount"] = min(10, max(1, int(config["prefetchCount"])))
    except (TypeError, ValueError):
        config["prefetchCount"] = DEFAULTS["prefetchCount"]
    for key in ("minRatio", "maxRatio"):
        try:
            config[key] = float(config[key])
        except (TypeError, ValueError):
            config[key] = DEFAULTS[key]
    # `languageSetting` is what was picked ("auto" included), `language` the resolved code.
    config["languageSetting"] = re.sub(r"[^a-z-]", "", str(config["language"]).lower()) or "auto"
    config["language"] = system_language() if config["languageSetting"] == "auto" else config["languageSetting"]
    config["notifyOnChange"] = config["notifyOnChange"] is True
    if not isinstance(config["placard"], str) or config["placard"] not in PLACARDS:
        config["placard"] = DEFAULTS["placard"]
    if not isinstance(config["fit"], str) or config["fit"] not in FITS:
        config["fit"] = DEFAULTS["fit"]
    if not isinstance(config["fame"], str) or config["fame"] not in FAME:
        config["fame"] = DEFAULTS["fame"]
    for key, allowed in (("localized", ("all", "prefer", "only")), ("localizedBy", ("title", "article"))):
        if not isinstance(config[key], str) or config[key] not in allowed:
            config[key] = DEFAULTS[key]
    config["filters"] = normalize_filters(config["filters"])
    config["saveDir"] = os.path.realpath(os.path.expanduser(str(config["saveDir"])))
    return config


def load_config():
    try:
        text = SHELL_JSON.read_text()
    except OSError:
        text = ""
    return normalize_config(config_entry(text))


def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return fallback


def write_json(path, data, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per thread too: the prefetcher writes from several threads.
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact \
        else json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text)
    tmp.replace(path)


def override_filters():
    """Temporary filter chosen in the popup; it replaces the configured one."""
    data = read_json(STATE / "filter.json")
    if not isinstance(data, dict):
        return None
    lists = (data.get("artists"), data.get("movements"), data.get("genres"))
    if any(isinstance(x, list) and x for x in lists) or data.get("yearFrom") is not None or data.get("yearTo") is not None:
        return data
    return None


def active_filters(config):
    override = override_filters()
    if override:
        return {
            "yearFrom": as_year(override.get("yearFrom")),
            "yearTo": as_year(override.get("yearTo")),
            "artists": [a["id"] for a in override.get("artists", []) if isinstance(a, dict) and a.get("id")],
            "movements": [m["id"] for m in override.get("movements", []) if isinstance(m, dict) and m.get("id")],
            "genres": [g["id"] for g in override.get("genres", []) if isinstance(g, dict) and g.get("id")],
        }
    return config["filters"]


def filter_key(filters):
    return hashlib.sha1(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:12]


def queue_key(config):
    """What a queued painting was fetched for: the active filter, the fame and
    language-preference settings, and the language its title and summary are in."""
    only = [config["localized"], config["localizedBy"]] if config["localized"] == "only" else None
    return filter_key({"filters": active_filters(config), "fame": config["fame"], "language": config["language"],
                       "localized": only})


def has_entity_filters(filters):
    """Filters that need the Wikidata pool rather than random Commons scans."""
    return bool(filters["artists"] or filters["movements"] or filters["genres"] or filters.get("fameArtists")
                or filters.get("fameLevel"))


@contextlib.contextmanager
def timed(stage):
    """Log how long a stage took (the journal), to see what is slow."""
    start = time.monotonic()
    try:
        yield
    finally:
        print(f"museum-wallpapers: timing {stage}: {time.monotonic() - start:.2f}s "
              f"[{threading.current_thread().name}]", file=sys.stderr)


# ----------------------------------------------------------------- network


def http_json(url, params=None, data=None, accept="application/json", timeout=40):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data else None
    request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                                              "Accept-Encoding": "gzip"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Wikidata's query service hiccups now and then; one retry.
            if attempt == 2 or exc.code not in (429, 502, 503, 504):
                raise
            time.sleep(3)


def mw_api(url, **params):
    return http_json(url, {**params, "format": "json", "formatversion": 2})


def download(url, dest):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    try:
        with urllib.request.urlopen(request, timeout=180) as response, open(tmp, "wb") as out:
            shutil.copyfileobj(response, out)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ----------------------------------------------------------------- wikidata


def entities(ids, props="labels|descriptions|claims|sitelinks", languages="en", site=None):
    """Batch wbgetentities on Wikidata (Q ids) or Commons (M ids)."""
    result = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for batch in chunks(ids, 50):
        api = COMMONS_API if batch[0].startswith("M") else WIKIDATA_API
        params = {"action": "wbgetentities", "ids": "|".join(batch), "props": props}
        if languages:
            params["languages"] = languages
        if site:
            params["sitefilter"] = site
        result.update(mw_api(api, **params).get("entities", {}))
    return result


def claim_ids(entity, prop):
    claims = entity.get("claims") or entity.get("statements") or {}
    ids = []
    for claim in claims.get(prop, []) if isinstance(claims, dict) else []:
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and value.get("id"):
            ids.append(value["id"])
    return ids


def claim_year(entity, prop="P571"):
    for claim in entity.get("claims", {}).get(prop, []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and "time" in value:
            match = re.match(r"([+-])0*(\d+)", value["time"])
            if match:
                year = int(match.group(2))
                return -year if match.group(1) == "-" else year
    return None


def label(entity, language):
    """In the wanted language, else English, else the multilingual label,
    else whatever language the item has (many paintings are labelled only in
    their museum's language)."""
    labels = entity.get("labels", {}) if entity else {}
    for lang in (language, "en", "mul"):
        if labels.get(lang, {}).get("value"):
            return labels[lang]["value"]
    return next((v["value"] for v in labels.values() if v.get("value")), None)


def is_painting(entity):
    return PAINTING in claim_ids(entity, "P31")


def year_matches(year, filters):
    if filters["yearFrom"] is None and filters["yearTo"] is None:
        return True
    if year is None:
        return False
    if filters["yearFrom"] is not None and year < filters["yearFrom"]:
        return False
    return filters["yearTo"] is None or year <= filters["yearTo"]


def resolve(name, kind, language="en"):
    """Turn an artist, movement or genre name into a Wikidata id (cached)."""
    name = str(name).strip()
    if re.fullmatch(r"Q\d+", name):
        return name
    if kind == "genre" and name.lower() in GENRES:
        return GENRES[name.lower()]
    cache_path = STATE / "resolved.json"
    cache = read_json(cache_path, {})
    key = f"{kind}:{name.lower()}"
    if cache.get(key):
        return cache[key]
    for lang in dict.fromkeys((language, "en")):
        found = mw_api(WIKIDATA_API, action="wbsearchentities", search=name, language=lang,
                       uselang=lang, type="item", limit=1).get("search", [])
        if found and re.fullmatch(r"Q\d+", found[0].get("id", "")):
            cache[key] = found[0]["id"]
            write_json(cache_path, cache)
            return cache[key]
    return None


def resolve_filters(filters, language):
    """Replace names with ids; unknown names are dropped and reported."""
    resolved, unknown = dict(filters), []
    for key, kind in (("artists", "artist"), ("movements", "movement"), ("genres", "genre")):
        ids = []
        for name in filters[key]:
            qid = resolve(name, kind, language)
            if qid:
                ids.append(qid)
            else:
                unknown.append(name)
        resolved[key] = ids
    return resolved, unknown


def sparql_query(filters):
    for qid in filters["artists"] + filters["movements"] + filters["genres"] + filters.get("fameArtists", []):
        if not re.fullmatch(r"Q\d+", qid):
            raise ValueError(f"not a Wikidata id: {qid!r}")
    lines = [
        "SELECT DISTINCT ?p ?img WHERE {",
        f"  ?p wdt:P31 wd:{PAINTING}; wdt:P18 ?img.",
    ]
    if filters["artists"]:
        lines.append("  VALUES ?artist { %s } ?p wdt:P170 ?artist." % " ".join(f"wd:{q}" for q in filters["artists"]))
    if filters["movements"]:
        # Movements are tagged far more often on artists than on paintings.
        lines.append("  VALUES ?movement { %s }" % " ".join(f"wd:{q}" for q in filters["movements"]))
        lines.append("  { ?p wdt:P135 ?movement } UNION { ?p wdt:P170 ?creator. ?creator wdt:P135 ?movement }")
    if filters["genres"]:
        lines.append("  VALUES ?genre { %s } ?p wdt:P136 ?genre." % " ".join(f"wd:{q}" for q in filters["genres"]))
    if filters.get("fameArtists"):
        # A list rather than a sitelinks filter in the query: Wikidata answers
        # this in ~20 s for 90k paintings instead of timing out.
        lines.append("  VALUES ?famed { %s } ?p wdt:P170 ?famed." % " ".join(f"wd:{q}" for q in filters["fameArtists"]))
    if filters["yearFrom"] is not None or filters["yearTo"] is not None:
        lines.append("  ?p wdt:P571 ?date.")
        if filters["yearFrom"] is not None:
            lines.append(f"  FILTER(YEAR(?date) >= {int(filters['yearFrom'])})")
        if filters["yearTo"] is not None:
            lines.append(f"  FILTER(YEAR(?date) <= {int(filters['yearTo'])})")
    lines.append("} LIMIT 100000")
    return "\n".join(lines)


def painter_ids(min_sitelinks):
    """Painters with articles in at least this many Wikipedias (~30 s for 60)."""
    query = (f"SELECT ?famed WHERE {{ ?famed wdt:P106 wd:{PAINTER}; wikibase:sitelinks ?links. "
             f"FILTER(?links >= {int(min_sitelinks)}) }}")
    data = http_json(SPARQL_API, data={"query": query}, accept="application/sparql-results+json", timeout=90)
    ids = sorted(row["famed"]["value"].rsplit("/", 1)[-1] for row in data.get("results", {}).get("bindings", []))
    ids = [qid for qid in ids if re.fullmatch(r"Q\d+", qid)]
    if not ids:
        # Never keep this: an empty list would quietly turn `fame` off.
        raise ValueError("Wikidata returned no painters")
    return ids


# The painters of a `fame` level and all their paintings ("fame bundle"), built
# into the plugin (tools/seed.py, run by hand before a release) so the first
# painting does not wait 30-60 s for Wikidata. The copy in CACHE/pools is
# refreshed by a detached `refresh-pools` process when missing or older than
# POOL_MAX_AGE and the old copy is used meanwhile; only with neither a cached
# nor a built-in copy does a pick wait for Wikidata.
SEED = Path(os.environ.get("MUSEUM_WALLPAPERS_SEED") or Path(__file__).resolve().parent / "seed")
_fame_lock = threading.Lock()
_fame_memory = {}  # path -> (mtime_ns, bundle): the parsed copy, re-read when the file changes
_refresh_spawned = set()  # levels this process has started a refresh for


def fame_path(level):
    return CACHE / "pools" / f"fame-{int(level)}.json"


def seed_path(level):
    return SEED / f"fame-{int(level)}.json.gz"


def refresh_failed_path(level):
    return CACHE / "pools" / f".fame-{int(level)}.failed"


def valid_bundle(data):
    return (isinstance(data, dict) and isinstance(data.get("painters"), list) and bool(data["painters"])
            and isinstance(data.get("items"), list) and bool(data["items"]))


def build_fame_bundle(level):
    """Ask Wikidata for the painters at this fame level and their paintings."""
    with timed(f"painter list {level}"):
        painters = painter_ids(level)
    filters = normalize_filters({})
    filters["fameArtists"] = painters
    with timed(f"painting pool {level}"):
        items = query_pool(filters)
    if not items:
        raise ValueError("Wikidata returned no paintings")
    return {"at": time.time(), "level": int(level), "painters": painters, "items": items}


def _read_bundle(path, opener=open):
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    with _fame_lock:
        remembered = _fame_memory.get(path)
        if remembered and remembered[0] == stamp:
            return remembered[1]
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, EOFError):
        return None
    if not valid_bundle(data):
        return None
    with _fame_lock:
        _fame_memory[path] = (stamp, data)
    return data


def cached_bundle(level):
    return _read_bundle(fame_path(level))


def seed_bundle(level):
    return _read_bundle(seed_path(level), gzip.open)


def refresh_fame(level):
    """Rebuild the cached bundle from Wikidata now. One process at a time;
    returns the bundle, or None when another process is already at it. A
    failure is stamped, so nothing retries before REFRESH_RETRY has passed."""
    held = lock(f"pools/.fame-{int(level)}.lock", wait=False)
    if held is None:
        return None
    with held:
        try:
            bundle = build_fame_bundle(level)
        except Exception:
            write_json(refresh_failed_path(level), {"at": time.time()})
            raise
        write_json(fame_path(level), bundle, compact=True)
        refresh_failed_path(level).unlink(missing_ok=True)
        return bundle


def refresh_fame_in_background(level):
    """Start `refresh-pools` for a level as a detached process (CLI calls are
    short-lived, a thread would die with them), unless this process already
    did, one is running, or one failed lately."""
    with _fame_lock:
        if level in _refresh_spawned:
            return False
        _refresh_spawned.add(level)
    failed = read_json(refresh_failed_path(level), {})
    if time.time() - failed.get("at", 0) < REFRESH_RETRY:
        return False
    probe = lock(f"pools/.fame-{int(level)}.lock", wait=False)
    if probe is None:
        return False  # another process is already refreshing
    probe.close()
    spawn("refresh-pools", str(int(level)))
    return True


def fame_bundle(level):
    """The painters of a fame level and their paintings, without waiting for
    Wikidata whenever a cached or built-in copy exists."""
    bundle = cached_bundle(level)
    if bundle is not None:
        with _fame_lock:
            _fame_memory.pop(seed_path(level), None)  # the built-in copy is no longer needed
        if time.time() - bundle.get("at", 0) >= POOL_MAX_AGE:
            refresh_fame_in_background(level)
        return bundle
    seed = seed_bundle(level)
    if seed is not None:
        refresh_fame_in_background(level)
        return seed
    # Nothing to start from (a checkout without seed data): wait for Wikidata.
    with timed("painter list and pool (no built-in copy)"), lock(f"pools/.fame-{int(level)}.lock"):
        bundle = cached_bundle(level)  # another process may have just built it
        if bundle is None:
            bundle = build_fame_bundle(level)
            write_json(fame_path(level), bundle, compact=True)
        return bundle


def famous_painters(min_sitelinks):
    """Painters with articles in at least this many Wikipedias."""
    return fame_bundle(min_sitelinks)["painters"]


def fame_only(filters):
    """A pick limited only by the `fame` setting: its bundle has the pool."""
    return bool(filters.get("fameLevel")) and not (filters["artists"] or filters["movements"] or filters["genres"]) \
        and filters["yearFrom"] is None and filters["yearTo"] is None


def file_title_from_url(url):
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    return "File:" + name.replace("_", " ")


_pools = {}


def entity_pool(filters):
    """(painting id, Commons file title) pairs for an entity filter, cached a
    week on disk and kept in memory for the rest of the run."""
    if fame_only(filters):
        return fame_bundle(filters["fameLevel"])["items"]
    path = CACHE / "pools" / f"{filter_key(filters)}.json"
    if path in _pools:
        return _pools[path]
    _pools[path] = items = _load_pool(path, filters)
    return items


def query_pool(filters):
    data = http_json(SPARQL_API, data={"query": sparql_query(filters)},
                     accept="application/sparql-results+json", timeout=90)
    items = []
    for row in data.get("results", {}).get("bindings", []):
        qid = row["p"]["value"].rsplit("/", 1)[-1]
        items.append([qid, file_title_from_url(row["img"]["value"])])
    return items


def _load_pool(path, filters):
    def fresh():
        pool = read_json(path)
        if isinstance(pool, dict) and time.time() - pool.get("at", 0) < POOL_MAX_AGE:
            return pool["items"]
        return None

    items = fresh()
    if items is not None:
        return items
    # Prefetch threads and an inline Next may all want a new pool: one asks
    # Wikidata (a ~20 s query), the others read what it wrote.
    with lock(f"pools/.{path.stem}.lock"):
        items = fresh()
        if items is None:
            items = query_pool(filters)
            write_json(path, {"at": time.time(), "items": items}, compact=True)
    return items


# ----------------------------------------------------------------- picking


def screen_size():
    """Largest connected monitor in pixels, 3840x2160 when unknown."""
    try:
        out = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5).stdout
        monitors = json.loads(out)
        return max((int(m["width"]), int(m["height"])) for m in monitors)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return 3840, 2160


def usable(title, width, height, config, screen):
    """Landscape, large enough for the screen, not a tile of a bigger scan."""
    if not width or not height or TILE_RE.search(title):
        return False
    ratio = width / height
    if not config["minRatio"] <= ratio <= config["maxRatio"]:
        return False
    # Judge the thumbnail Commons will actually serve, cover-scaled to the
    # screen: allow a little upscaling, not a blurry one.
    thumb_w = min(width, MAX_THUMB_WIDTH)
    return thumb_w >= min(screen[0], MAX_THUMB_WIDTH) * 0.9 and thumb_w / ratio >= screen[1] * 0.85


EXTMETADATA = "ObjectName|Artist|DateTimeOriginal|ImageDescription"


def image_infos(titles):
    """Size and page URL of Commons files. Without `extmetadata`: asking for
    it makes the same request ~20x slower (30 s instead of 1.5 s for 150
    files), and only the painting finally picked needs it (see describe)."""
    infos = {}
    for batch in chunks(titles, 50):
        data = mw_api(COMMONS_API, action="query", prop="imageinfo", iiprop="size|url",
                      titles="|".join(batch))
        # Titles come back normalized; key the result by what was asked for.
        asked = {n["to"]: n["from"] for n in data.get("query", {}).get("normalized", [])}
        for page in data.get("query", {}).get("pages", []):
            if page.get("imageinfo"):
                infos[asked.get(page["title"], page["title"])] = page
    return infos


def random_candidates(filters, config, screen):
    """Random Google Art Project scans, as (page, painting entity) pairs."""
    width = int(min(screen[0], MAX_THUMB_WIDTH) * 0.95)
    found = mw_api(COMMONS_API, action="query", list="search", srnamespace=6, srsort="random",
                   srsearch=RANDOM_SEARCH.format(width=width), srlimit=50, srprop="")
    titles = [hit["title"] for hit in found.get("query", {}).get("search", [])]
    pages = [p for p in image_infos(titles).values()
             if usable(p["title"], p["imageinfo"][0]["width"], p["imageinfo"][0]["height"], config, screen)]
    media = entities([f"M{p['pageid']}" for p in pages], props="claims")
    painting_of = {}
    for page in pages:
        items = claim_ids(media.get(f"M{page['pageid']}", {}), "P6243")
        if items:
            painting_of[page["pageid"]] = items[0]
    paintings = entities(list(painting_of.values()), props="claims")
    result = []
    for page in pages:
        entity = paintings.get(painting_of.get(page["pageid"]), {})
        if entity and is_painting(entity) and year_matches(claim_year(entity), filters):
            result.append((page, painting_of[page["pageid"]]))
    random.shuffle(result)
    return result


def filtered_candidates(filters, config, screen):
    pool = entity_pool(filters)
    if not pool:
        raise NoMatch(t("nm_no_match", config["language"]))
    sample = random.sample(pool, min(len(pool), POOL_SAMPLE))
    infos = image_infos([title for _, title in sample])
    result = []
    for qid, title in sample:
        page = infos.get(title)
        if page and usable(title, page["imageinfo"][0]["width"], page["imageinfo"][0]["height"], config, screen):
            result.append((page, qid))
    if not result and len(pool) <= POOL_SAMPLE:
        raise NoMatch(t("nm_pool_small", config["language"], n=len(pool)))
    return result


def clean_text(text):
    """Text as a notification can show it: compatibility forms folded (NFKC),
    every kind of space a plain space, invisible format characters (zero-width
    spaces, soft hyphens, direction marks) and control characters dropped."""
    text = unicodedata.normalize("NFKC", text or "")
    out = []
    for char in text:
        category = unicodedata.category(char)
        if category in ("Cf", "Cc", "Co", "Cs", "Cn"):
            continue
        out.append(" " if category.startswith("Z") else char)
    return " ".join("".join(out).split())


def plain(markup, limit=None):
    text = re.sub(r"<[^>]+>", " ", markup or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


LANGUAGE_NAMES = (
    "English", "German", "French", "Italian", "Spanish", "Dutch", "Russian", "Polish", "Portuguese",
    "Swedish", "Danish", "Norwegian", "Norwegian Bokmål", "Finnish", "Czech", "Hungarian", "Catalan",
    "Greek", "Japanese", "Chinese", "Korean", "Ukrainian", "Romanian", "Croatian", "Serbian",
    "Serbo-Croatian", "Slovak", "Slovenian", "Turkish", "Hebrew", "Arabic", "Latin", "Basque",
)
LANGUAGE_PREFIX = re.compile(r"(?:^|\s)(%s):\s+" % "|".join(
    re.escape(name) for name in sorted(LANGUAGE_NAMES, key=len, reverse=True)))


def object_name(markup):
    """Commons' ObjectName, without the QuickStatements text ("title
    QS:P1476,…") some file pages carry. "German: X English: Y" gives the
    English part, else the first; other colons (as in "Nocturne: Blue and
    Silver") are kept."""
    text = without_quickstatements(plain(markup))
    text = re.split(r"\s+(?:title|label) QS:", text)[0]
    parts = LANGUAGE_PREFIX.split(text)
    if len(parts) > 1 and not parts[0].strip():  # only when it starts with a language
        named = dict(zip(parts[1::2], parts[2::2]))
        text = named.get("English") or next(iter(named.values()))
    text = text.strip().rstrip(".")
    if len(text) > 120:  # on a word boundary, not "…At Sunrise, o"
        text = text[:120].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return text or None


def painting_date(markup):
    """Commons' DateTimeOriginal as the painting's date, or "" when it is
    plainly the date of the scan."""
    text = plain(markup, 40)
    years = [int(y) for y in re.findall(r"\b(\d{4})\b", text)]
    if years and min(years) > LATEST_PAINTING_YEAR:
        return ""
    return text


def without_quickstatements(text):
    """Drop `label QS:Lde,"…"`-style fragments some Commons pages carry."""
    return re.sub(r'\s*(?:title|label|description) QS:[^,\s]*,(?:[a-z-]+:)?"[^"]*"', "", text).strip()


def commons_description(markup):
    """The file page's description, when it says more than the title."""
    text = without_quickstatements(plain(markup))
    if len(text) < 40:
        return ""
    return text[:600].rsplit(" ", 1)[0] + "…" if len(text) > 600 else text


def first_sentences(text, count):
    # Not after initials ("J. M. W. Turner") or "St.", "Mt.", "Dr." and the like.
    parts = re.split(r"(?<!\b[A-Z]\.)(?<!\b(?:St|Mt|Dr|Mr|Jr|Sr)\.)(?<=[.!?])\s+(?=[A-ZА-ЯЁ])", text.strip())
    return " ".join(parts[:count])


def wikipedia_summary(language, title):
    url = (f"https://{language}.wikipedia.org/api/rest_v1/page/summary/"
           + urllib.parse.quote(title.replace(" ", "_"), safe=""))
    data = http_json(url)
    if data.get("type") == "disambiguation":
        return "", None
    return first_sentences(data.get("extract", ""), 6), data.get("content_urls", {}).get("desktop", {}).get("page")


def article(entity, language):
    """(language, title) of the best Wikipedia article for an entity."""
    sitelinks = entity.get("sitelinks", {}) if entity else {}
    for lang in dict.fromkeys((language, "en")):
        link = sitelinks.get(f"{lang.replace('-', '_')}wiki")
        if link and link.get("title"):
            return lang, link["title"]
    return None


def file_metadata(pageid):
    """Commons' own description of one file (title, artist, date, text)."""
    data = mw_api(COMMONS_API, action="query", prop="imageinfo", iiprop="extmetadata",
                  iiextmetadatafilter=EXTMETADATA, pageids=pageid)
    pages = data.get("query", {}).get("pages", [])
    info = pages[0].get("imageinfo", [{}])[0] if pages else {}
    return info.get("extmetadata", {})


def describe(page, qid, language):
    """Everything the plugin shows about a painting."""
    image = page["imageinfo"][0]
    meta = image["extmetadata"] if "extmetadata" in image else file_metadata(page["pageid"])
    languages = "|".join(dict.fromkeys((language, "en")))
    sites = "|".join(f"{lang.replace('-', '_')}wiki" for lang in dict.fromkeys((language, "en")))
    # All languages: a painting labelled only in German still gets its title.
    painting = entities([qid], languages=None, site=sites).get(qid, {})
    creators = claim_ids(painting, "P170")
    collections = claim_ids(painting, "P195")
    movements = claim_ids(painting, "P135")
    genres = claim_ids(painting, "P136")
    # The rest depends only on the painting's item: fetched side by side.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="describe") as pool:
        related_job = pool.submit(entities, creators[:1] + collections[:1] + genres[:1],
                                  props="labels|claims|sitelinks", languages=languages, site=sites)
        movement_job = pool.submit(entities, movements[:1], props="labels", languages=languages) \
            if movements else None
        own_article = article(painting, language)
        summary_job = pool.submit(_summary, own_article) if own_article else None
        related = related_job.result()
        movement = movement_job.result().get(movements[0], {}) if movement_job else {}
        own_summary = summary_job.result() if summary_job else ("", None)
    artist = related.get(creators[0], {}) if creators else {}
    if not movements and artist:
        # Movements are tagged on artists far more often than on paintings.
        movements = claim_ids(artist, "P135")
        movement = entities(movements[:1], props="labels", languages=languages).get(movements[0], {}) \
            if movements else {}

    year = claim_year(painting)
    info = {
        "id": qid,
        "language": language,
        "title": clean_text(label(painting, language) or object_name(meta.get("ObjectName", {}).get("value"))
                            or page["title"][5:]),
        "artist": label(artist, language) or plain(meta.get("Artist", {}).get("value"), 80) or "Unknown artist",
        "artistId": creators[0] if creators else None,
        "year": str(year) if year is not None else painting_date(meta.get("DateTimeOriginal", {}).get("value")),
        "museum": label(related.get(collections[0], {}), language) if collections else None,
        "movement": label(movement, language),
        "movementId": movements[0] if movements else None,
        "genre": label(related.get(genres[0], {}), language) if genres else None,
        "summary": "",
        "url": image["descriptionurl"],
        "commons": image["descriptionurl"],
        "wikidata": f"https://www.wikidata.org/wiki/{qid}",
    }

    # Best first: the painting's own article, the Commons description, then
    # a few lines about the artist.
    summary, url = own_summary
    if summary:
        info["summary"], info["url"] = summary, url or info["url"]
    else:
        info["summary"] = commons_description(meta.get("ImageDescription", {}).get("value"))
    if not info["summary"]:
        summary, url = _summary(article(artist, language))
        if summary:
            info["summary"], info["url"] = t("about_artist", language) + summary, url or info["url"]
    return info


def _summary(found):
    """(summary, url) of a Wikipedia article, or ("", None)."""
    if not found:
        return "", None
    try:
        return wikipedia_summary(*found)
    except (OSError, ValueError):
        return "", None


def thumb_width(screen, ratio=None):
    """The thumbnail width to ask Commons for: the smallest standard step whose
    image covers the screen, height included (a 2:1 painting on a 5:4 screen
    needs more width than the screen has). Wider than the original, Commons
    serves the original. `screen` is (width, height) or a width alone."""
    width, height = screen if isinstance(screen, tuple) else (screen, 0)
    needed = max(width, height * ratio) if ratio else width
    target = min(needed, MAX_THUMB_WIDTH) * 0.95
    return next((step for step in THUMB_STEPS if step >= target), THUMB_STEPS[-1])


def claim(name, key, allow_shown=False):
    """Reserve a painting for one download, so parallel downloads (and other
    processes) never take the same one: "claimed", or why not ("queued" when
    it is queued or being downloaded, "shown" when shown recently)."""
    QUEUE.mkdir(parents=True, exist_ok=True)
    with lock(".claim.lock"):
        if (QUEUE / f"{name}.json").exists() or (QUEUE / f"{name}.jpg").exists():
            return "queued"
        if not allow_shown and (CACHE / f"{name}.jpg").exists():
            return "shown"
        # The queue lists only *.jpg; a sidecar alone marks a download in flight.
        write_json(QUEUE / f"{name}.json", {"filter": key, "claimedBy": os.getpid()})
        return "claimed"


def in_flight(key):
    """Downloads under way for this queue key (claimed, image not there yet):
    this process's, or another process's from the last two minutes (a claim
    left by a process that was killed must not hold anything up)."""
    if not QUEUE.exists():
        return 0
    count = 0
    for sidecar in QUEUE.glob("*.json"):
        try:
            age = time.time() - sidecar.stat().st_mtime
        except OSError:
            continue
        if sidecar.with_suffix(".jpg").exists():
            continue
        data = read_json(sidecar, {})
        owner = data.get("claimedBy")
        if data.get("filter") == key and (owner == os.getpid() or (age < IN_FLIGHT_AGE and alive(owner))):
            count += 1
    return count


def alive(pid):
    """Whether a claiming process still runs (unknown counts as running)."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True
    return True


def enqueue(page, qid, config, key, note=None):
    """Download a claimed painting into the queue; returns its path, or None
    to skip it (the claim is dropped). `note` explains a random painting
    queued because the filter failed."""
    name = f"commons-{page['pageid']}"
    QUEUE.mkdir(parents=True, exist_ok=True)
    dest = QUEUE / f"{name}.jpg"
    try:
        cached = CACHE / f"{name}.jpg"
        info = read_json(cached.with_suffix(".json")) if cached.exists() else None
        thumb = None
        if info and info.get("language") != config["language"]:
            with timed("describe"):
                info = describe(page, qid, config["language"])
        if not info:
            with timed("thumbnail url"):
                data = mw_api(COMMONS_API, action="query", prop="imageinfo", iiprop="url",
                              iiurlwidth=thumb_width(screen_size(), page["imageinfo"][0]["width"]
                                                     / page["imageinfo"][0]["height"]),
                              pageids=page["pageid"])
            thumb = data["query"]["pages"][0]["imageinfo"][0]
            # Without a rendered thumbnail the original could be hundreds of MB.
            if not thumb.get("thumburl"):
                dest.with_suffix(".json").unlink(missing_ok=True)
                return None
            with timed("describe"):
                info = describe(page, qid, config["language"])
        info["filter"] = key
        info["claimedBy"] = os.getpid()
        if note:
            info["filterError"] = note
        else:
            info.pop("filterError", None)
        # Replaces the claim; the queue only lists *.jpg, so the sidecar must
        # exist before the image or the image would be taken for a stale one.
        write_json(dest.with_suffix(".json"), info)
        if thumb:
            with timed(f"download {thumb.get('thumbwidth') or '?'}px"):
                download(thumb["thumburl"], dest)
        else:
            tmp = dest.with_name(f"{dest.name}.{os.getpid()}-{threading.get_ident()}.part")
            shutil.copyfile(cached, tmp)
            tmp.replace(dest)
    except BaseException:
        dest.with_suffix(".json").unlink(missing_ok=True)
        raise
    os.utime(dest)  # queue order is download order
    os.utime(dest.with_suffix(".json"))  # a slow download is not an orphan
    return dest


def described_in(qids, config):
    """The paintings among `qids` with a title (or, with localizedBy "article",
    a Wikipedia article) in the chosen language; None if Wikidata cannot say."""
    language = config["language"]
    site = f"{language.replace('-', '_')}wiki"
    try:
        found = entities(list(qids), props="labels|sitelinks", languages=language, site=site)
    except Exception as exc:  # noqa: BLE001 - no preference rather than no painting
        print(f"museum-wallpapers: language check failed: {exc}", file=sys.stderr)
        return None
    if not found:
        return None  # an error answer (no entities at all)
    if config["localizedBy"] == "article":
        links = [e.get("sitelinks") or {} for e in found.values()]
        # A wiki Wikidata does not know (nbwiki, pt_brwiki) is ignored by the
        # filter, and every sitelink comes back: then it cannot say.
        if any(set(link) - {site} for link in links):
            return None
        return {q for q, e in found.items() if site in (e.get("sitelinks") or {})}
    return {q for q, e in found.items() if language in (e.get("labels") or {})}


def prefer_described(found, config):
    """Candidates reordered (or, with "only", narrowed) by the language setting."""
    if config["localized"] == "all" or not found:
        return found
    if config["language"] == "en" and config["localized"] == "prefer" and config["localizedBy"] == "title":
        return found  # nearly every painting has an English title: skip the request
    described = described_in({qid for _, qid in found}, config)
    if described is None:
        return found
    first = [c for c in found if c[1] in described]
    return first if config["localized"] == "only" else first + [c for c in found if c[1] not in described]


def fetch_next(config, unfiltered=False, note=None):
    """Download one new painting into the queue; returns its image path."""
    filters = active_filters(config)
    key = queue_key(config)
    screen = screen_size()
    if unfiltered:
        filters = normalize_filters({})
        # The fallback shows something: "only" becomes "prefer", rather than
        # repeating the same search that just found nothing in the language.
        if config["localized"] == "only":
            config = {**config, "localized": "prefer"}
    filters, unknown = resolve_filters(filters, config["language"])
    if unknown:
        raise NoMatch(t("nm_unknown", config["language"], names=", ".join(unknown)))
    # An explicit artist (or "More by …") is shown however famous they are.
    if config["fame"] != "any" and not filters["artists"]:
        filters["fameLevel"] = FAME[config["fame"]]
        if not fame_only(filters):
            filters["fameArtists"] = famous_painters(filters["fameLevel"])
    QUEUE.mkdir(parents=True, exist_ok=True)
    current = current_painting()
    repeats, current_matched, queued_matched, dropped = [], False, False, 0
    for _ in range(SEARCH_ROUNDS):
        with timed("search"):
            if has_entity_filters(filters):
                found = filtered_candidates(filters, config, screen)
                exhaustive = len(entity_pool(filters)) <= POOL_SAMPLE
            else:
                found = random_candidates(filters, config, screen)
                exhaustive = False
            before = len(found)
            found = prefer_described(found, config)
            dropped += before - len(found)
        for page, qid in found:
            name = f"commons-{page['pageid']}"
            state = claim(name, key)
            if state == "queued":
                queued_matched = True
                continue
            if state == "shown":
                if current and current.stem == name:
                    current_matched = True
                else:
                    repeats.append((page, qid))
                continue
            dest = enqueue(page, qid, config, key, note)
            if dest:
                return dest
        if exhaustive:
            break
    # Every match was shown recently (a small pool): show one of them again,
    # but never one that is still waiting in the queue.
    random.shuffle(repeats)
    for page, qid in repeats:
        if claim(f"commons-{page['pageid']}", key, allow_shown=True) != "claimed":
            queued_matched = True
            continue
        dest = enqueue(page, qid, config, key, note)
        if dest:
            return dest
    if queued_matched:
        raise AllQueued(t("nm_all_queued", config["language"]))
    if current_matched:
        raise OnlyCurrent(t("nm_only_current", config["language"]))
    if config["localized"] == "only" and dropped:
        raise NoMatch(t("nm_localized", config["language"]))
    if has_entity_filters(filters):
        raise NoMatch(t("nm_too_small", config["language"]))
    if filters["yearFrom"] is not None or filters["yearTo"] is not None:
        raise NoMatch(t("nm_years", config["language"]))
    raise NoMatch(t("nm_none", config["language"]))


def queue_items(config):
    """Prefetched images for the active filter, oldest first."""
    key = queue_key(config)

    def age(path):
        try:
            return path.stat().st_mtime
        except OSError:  # taken by a switch meanwhile
            return 0

    images = sorted(QUEUE.glob("*.jpg"), key=age) if QUEUE.exists() else []
    return [image for image in images if read_json(image.with_suffix(".json"), {}).get("filter") == key]


def queued(config):
    """The oldest prefetched image for the active filter; drops stale ones
    and sidecars left by killed downloads. Call with lock() held."""
    keep = queue_items(config)
    for image in QUEUE.glob("*.jpg") if QUEUE.exists() else []:
        if image not in keep:
            image.unlink(missing_ok=True)
            image.with_suffix(".json").unlink(missing_ok=True)
    leftovers = [p for pattern in ("*.json", "*.part", "*.tmp") for p in QUEUE.glob(pattern)] if QUEUE.exists() else []
    for path in leftovers:
        try:
            old = time.time() - path.stat().st_mtime > ORPHAN_AGE
        except OSError:
            continue
        if old and (path.suffix in (".part", ".tmp") or not path.with_suffix(".jpg").exists()):
            path.unlink(missing_ok=True)
        elif path.suffix == ".json" and not path.with_suffix(".jpg").exists():
            # A claim whose process is gone frees its painting at once.
            owner = read_json(path, {}).get("claimedBy")
            if owner != os.getpid() and not alive(owner):
                path.unlink(missing_ok=True)
    return keep[0] if keep else None


# ----------------------------------------------------------------- state


def current_background():
    try:
        return CURRENT_LINK.resolve(strict=True)
    except OSError:
        return None


def crop_fraction(width, height, screen):
    """How much of a painting filling the screen would be cut off."""
    ratio, screen_ratio = width / height, screen[0] / screen[1]
    return 1 - min(ratio / screen_ratio, screen_ratio / ratio)


def image_size(path):
    out = subprocess.run(["magick", "identify", "-format", "%w %h", f"{path}[0]"],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    width, height = (int(x) for x in out.split()[:2])
    return width, height


def render(original, config):
    """The file to show for a painting under the `fit` setting: the original
    itself, or a screen-sized copy framed by ImageMagick (which Omarchy ships).
    Falls back to the original when ImageMagick is missing or fails."""
    fit = config["fit"]
    if fit == "cover":
        return original
    screen = screen_size()
    # The size is in the name, so a docked or undocked laptop re-frames.
    out = DISPLAY / f"{original.stem}.{fit}.{screen[0]}x{screen[1]}.jpg"
    if out.exists():
        return out  # framed ahead by the prefetcher: made from this very original
    try:
        if fit == "auto" and crop_fraction(*image_size(original), screen) <= AUTO_CROP:
            return original
        width, height = screen
        src = str(original)
        if fit == "wall":
            command = ["magick", "-size", f"{width}x{height}", f"xc:{WALL_COLOR}",
                       "(", src, "-resize", f"{int(width * 0.94)}x{int(height * 0.92)}", ")"]
        else:
            # Blurred at a tenth of the size: as smooth, and ~0.2 s instead of seconds.
            small = f"{max(1, width // 10)}x{max(1, height // 10)}"
            command = ["magick", "(", src, "-resize", f"{small}^", "-gravity", "center", "-extent", small,
                       "-blur", "0x6", "-resize", f"{width}x{height}!", "-modulate", "62", ")",
                       "(", src, "-resize", f"{width}x{height}", ")"]
        DISPLAY.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.name}.{os.getpid()}.part")
        try:
            with timed("frame"):
                subprocess.run(command + ["-gravity", "center", "-composite", "-quality", "92", f"jpg:{tmp}"],
                               check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
        return out
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"museum-wallpapers: could not frame {original.name}: {exc}", file=sys.stderr)
        return original


def current_painting():
    """The painting on screen: the background itself, or the original behind
    a framed copy in DISPLAY."""
    image = current_background()
    if image and image.parent == DISPLAY:
        state = read_json(STATE / "state.json", {})
        if state.get("original") and os.path.realpath(state.get("image") or "/") == str(image):
            return Path(state["original"])
        candidate = CACHE / f"{image.name.split('.')[0]}.jpg"
        return candidate if candidate.exists() else image
    return image


def show(painting, config):
    """Frame (per `fit`) and set a painting; returns the file now shown."""
    shown = render(painting, config)
    set_background(shown)
    return shown


def set_background(path):
    subprocess.run(["omarchy-theme-bg-set", str(path)], check=True, stdout=subprocess.DEVNULL)


def saved_index(config):
    return read_json(Path(config["saveDir"]) / ".museum-wallpapers.json", {})


def local_credit(image):
    """A row of the collection's CREDITS.md table, if it has one for this file."""
    try:
        lines = (image.parent / "CREDITS.md").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) >= 2 and cells[0] == image.name:
            return plain(re.sub(r"\s*\(\[source\]\(.*?\)\)", "", cells[1]).replace("*", ""))
    return None


def info_for(image, config):
    sidecar = read_json(image.with_suffix(".json")) if image.parent in (CACHE, QUEUE) else None
    if sidecar:
        return sidecar
    saved = saved_index(config).get(image.name)
    if saved:
        return saved
    title = local_credit(image) or image.stem.replace("-", " ").replace("_", " ").title()
    return {"title": title, "artist": "", "year": "", "museum": None, "summary": "",
            "url": "https://commons.wikimedia.org/w/index.php?search=" + urllib.parse.quote(title)}


def status(config, error=None):
    state = read_json(STATE / "state.json", {})
    image = current_background()
    ours = bool(image) and (image.parent in (CACHE, DISPLAY)
                            or str(image) == os.path.realpath(state.get("image") or "/"))
    painting = current_painting() if ours else image
    override = override_filters()
    return {
        "active": ours,
        "image": str(painting) if painting else "",
        "fromCache": bool(painting) and painting.parent == CACHE,
        "info": info_for(painting, config) if ours else None,
        "setAt": state.get("setAt", 0),
        "filter": override,
        "config": {key: config[key] for key in ("intervalMinutes", "notifyOnChange", "placard", "language",
                                                "languageSetting", "fit", "fame", "localized",
                                                "localizedBy")},
        "ui": ui_strings(config["language"]),
        "error": error or state.get("error") or "",
    }


def prune(config, previous=None):
    images = sorted(CACHE.glob("commons-*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    current, shown = current_painting(), current_background()
    for image in images[config["cacheSize"]:]:
        if image != current:
            image.unlink(missing_ok=True)
            image.with_suffix(".json").unlink(missing_ok=True)
    # Framed copies stay for the painting on screen and for every queued one
    # (framed ahead by the prefetcher).
    keep = {current.stem} if current else set()
    keep |= {queued.stem for queued in QUEUE.glob("*.jpg")} if QUEUE.exists() else set()
    for framed in DISPLAY.glob("*.jpg") if DISPLAY.exists() else []:
        # `previous` was on screen a moment ago: the shell still fades from it.
        if framed not in (shown, previous) and framed.name.split(".")[0] not in keep:
            framed.unlink(missing_ok=True)
    # Pools whose filter or painter list changed are never read again.
    pools = CACHE / "pools"
    for pool in pools.glob("*.json") if pools.exists() else []:
        try:
            if time.time() - pool.stat().st_mtime > 2 * POOL_MAX_AGE:
                pool.unlink(missing_ok=True)
        except OSError:
            pass
    for part in DISPLAY.glob("*.part") if DISPLAY.exists() else []:
        try:
            if time.time() - part.stat().st_mtime > ORPHAN_AGE:
                part.unlink(missing_ok=True)
        except OSError:
            pass


def screen_key():
    width, height = screen_size()
    return f"{width}x{height}"


def record(image, error="", error_kind="", original=None, fit=None):
    """Remember what was set and when; setAt also delays the next try after a
    failure. `original` is the painting behind a framed copy, `fit` the mode
    it was framed with (kept from before when not given), `screen` the size it
    was shown for."""
    previous = read_json(STATE / "state.json", {})
    write_json(STATE / "state.json", {"image": os.path.realpath(image) if image else "",
                                      "original": os.path.realpath(original or image) if (original or image) else "",
                                      "fit": fit or previous.get("fit"), "screen": screen_key(),
                                      "setAt": time.time(), "error": error, "errorKind": error_kind})


def framed_for_another_screen(image):
    """A framed copy (`<stem>.<fit>.<W>x<H>.jpg`) made for a screen size other
    than the current one: the laptop was docked or undocked."""
    parts = image.name.split(".") if image and image.parent == DISPLAY else []
    if len(parts) != 4:
        return False
    width, height = screen_size()
    return parts[2] != f"{width}x{height}"


def refit(config):
    """Re-frame the painting on screen after the `fit` setting or the screen
    size changed, without restarting the rotation interval."""
    state = read_json(STATE / "state.json", {})
    # Paintings set before `fit` existed were shown unframed.
    same_fit = (state.get("fit") or "cover") == config["fit"]
    # `cover` shows the original whatever the screen; `auto` may now need a frame.
    new_screen = config["fit"] != "cover" and (
        state.get("screen", screen_key()) != screen_key() or framed_for_another_screen(current_background()))
    if (same_fit and not new_screen) or not status(config)["active"]:
        return
    painting = current_painting()
    if not painting or not painting.exists() or painting.parent not in (CACHE, Path(config["saveDir"])):
        return
    try:
        shown = show(painting, config)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"museum-wallpapers: could not re-frame: {exc}", file=sys.stderr)
        return
    write_json(STATE / "state.json", {**state, "image": os.path.realpath(shown), "original": os.path.realpath(painting),
                                      "fit": config["fit"], "screen": screen_key()})
    prune(config)


def owns(path, config):
    """A file this plugin shows: downloaded, framed, or from saveDir."""
    return bool(path) and path.parent in (CACHE, DISPLAY, Path(config["saveDir"]))


def record_current(config, error="", error_kind=""):
    """Record that nothing changed. A background this plugin did not set (a
    theme's, say) is never claimed; only the time and the error are kept."""
    image = current_background()
    if owns(image, config):
        record(image, error, error_kind, original=current_painting())
    else:
        state = read_json(STATE / "state.json", {})
        write_json(STATE / "state.json", {**state, "setAt": time.time(), "error": error, "errorKind": error_kind})


def notify(summary, body):
    """A plain notification. The Omarchy notification body is StyledText, so
    markup characters (as in "<urlopen error …>") are escaped."""
    subprocess.run(["notify-send", "-a", "Museum Wallpapers", "--", summary, html.escape(body, quote=False)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def report(error, error_kind):
    """Notify about an error once per kind, not on every retry."""
    if error and error_kind != read_json(STATE / "state.json", {}).get("errorKind"):
        notify("Museum Wallpapers", error)


def local_fallback(config, error, error_kind):
    """Offline: another saved or already downloaded painting."""
    current = current_painting()
    folder = Path(config["saveDir"])
    saved = [p for p in folder.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES] if folder.is_dir() else []
    images = [p for p in saved + list(CACHE.glob("commons-*.jpg")) if p != current]
    if images:
        image = random.choice(images)
        try:
            record(show(image, config), error, error_kind, original=image, fit=config["fit"])
            return
        except (OSError, subprocess.SubprocessError):
            pass
    record_current(config, error, error_kind)


def lock(name=".lock", wait=True):
    """An exclusive file lock, or None when wait=False and it is taken.

    `.lock` guards switching and the queue's contents and is held only
    briefly. `.prefetcher.lock` keeps a single process filling the queue (with
    PREFETCH_WORKERS threads). `.claim.lock` is held only while a painting is
    reserved for a download (a sidecar without its image), so parallel
    downloads, in threads or processes, never pick the same painting; a switch
    that finds the queue empty waits for a download in flight rather than
    starting its own. `pools/.fame-<level>.lock` keeps one refresh of a fame
    bundle at a time. flock locks belong to the open file, so each call
    excludes threads as well as processes."""
    path = CACHE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        handle.close()
        return None
    return handle


def switch(config):
    """Set the next painting; returns an error message or ''. Never raises
    for network or Wikimedia trouble: every failure is recorded, so the
    minute tick does not retry until the next interval."""
    error, error_kind = "", ""
    previous = current_background()
    image = queued(config) or wait_for_download(config)
    if not image:
        try:
            with timed("fetch on demand (queue empty)"):
                image = fetch_next(config)
        except (OnlyCurrent, AllQueued):
            # Nothing to change to yet; keep it without an error until the next interval.
            record_current(config)
            return ""
        except NoMatch as exc:
            error, error_kind = t("err_filter", config["language"], detail=exc), "filter"
            try:
                image = fetch_next(config, unfiltered=True)
            except NoMatch as inner:
                error = t("err_filter_nothing", config["language"], detail=exc, inner=inner)
            except Exception as inner:  # noqa: BLE001 - any failure falls back below
                error, error_kind = t("err_filter_network", config["language"], detail=exc, inner=inner), "network"
        except Exception as exc:  # noqa: BLE001 - truncated reads, HTTP errors, bad JSON
            error, error_kind = t("err_network", config["language"], detail=exc), "network"
    report(error, error_kind)
    if not image:
        local_fallback(config, error, error_kind)
        return error
    target = CACHE / image.name
    try:
        image.with_suffix(".json").replace(target.with_suffix(".json"))
        image.replace(target)
        os.utime(target)
        with timed("show"):
            shown = show(target, config)
    except (OSError, subprocess.SubprocessError) as exc:
        error, error_kind = t("err_background", config["language"], detail=exc), "background"
        report(error, error_kind)
        record_current(config, error, error_kind)
        return error
    filter_error = read_json(target.with_suffix(".json"), {}).get("filterError")
    if filter_error and not error:
        error, error_kind = t("err_filter", config["language"], detail=filter_error), "filter"
        report(error, error_kind)
    record(shown, error, error_kind, original=target, fit=config["fit"])
    prune(config, previous)
    if config["notifyOnChange"]:
        spawn_notify()
    return error


def wait_for_download(config, timeout=60):
    """With the queue empty but a download for this filter under way (the
    prefetcher's), waiting for it is faster than starting a new one."""
    key = queue_key(config)
    deadline = time.monotonic() + timeout
    waited = False
    while in_flight(key) and time.monotonic() < deadline:
        waited = True
        time.sleep(0.25)
        if queue_items(config):
            break
    if waited:
        print("museum-wallpapers: waited for a download in flight", file=sys.stderr)
    return queued(config) if waited else None


def _prefetch_one(config):
    """One painting into the queue: "ok", "small" (a small pool has nothing
    more to offer) or "failed"."""
    try:
        try:
            image = fetch_next(config)
        except (OnlyCurrent, AllQueued):
            return "small"
        except NoMatch as exc:
            image = fetch_next(config, unfiltered=True, note=str(exc))
        prerender(image, config)
        return "ok"
    except Exception as exc:  # noqa: BLE001 - retried after the backoff
        print(f"museum-wallpapers: prefetch failed: {exc}", file=sys.stderr)
        return "failed"


def prerender(image, config):
    """Frame a queued painting now, so switching to it only sets the
    wallpaper (render reuses the copy)."""
    if not image or config["fit"] == "cover":
        return
    with timed("frame ahead"):
        render(image, config)


def prefetch(config):
    """Fill the queue up to prefetchCount for the active filter, up to
    PREFETCH_WORKERS paintings at a time, each framed right away so a switch
    only sets it. Like a switch, a filter that matches nothing gets random
    paintings; a failure is stamped so the tick backs off instead of retrying
    every minute."""
    stamp = CACHE / ".prefetch-failed.json"
    attempts = config["prefetchCount"] * 2
    while attempts > 0:
        # Taken per round: a failure under filter A must not hold back
        # filter B set from the popup while A was downloading.
        key = queue_key(config)
        missing = config["prefetchCount"] - len(queue_items(config)) - in_flight(key)
        if missing <= 0:
            break
        workers = min(PREFETCH_WORKERS, missing, attempts)
        attempts -= workers
        with timed(f"prefetch round of {workers}"):
            if workers == 1:
                results = [_prefetch_one(config)]
            else:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prefetch") as pool:
                    results = list(pool.map(lambda _n: _prefetch_one(config), range(workers)))
        if "small" in results:
            # A small pool: all it has is queued or showing. The next switch
            # prefetches again; the tick need not ask every minute.
            write_json(stamp, {"at": time.time(), "filter": key, "reason": "small pool"})
            return
        if "failed" in results:
            write_json(stamp, {"at": time.time(), "filter": key})
            return
    stamp.unlink(missing_ok=True)


def prefetch_wanted(config):
    """The queue is short, and no prefetch for this filter failed recently."""
    if len(queue_items(config)) >= config["prefetchCount"]:
        return False
    failed = read_json(CACHE / ".prefetch-failed.json", {})
    recent = time.time() - failed.get("at", 0) < PREFETCH_BACKOFF
    return not (recent and failed.get("filter") == queue_key(config))


# ----------------------------------------------------------------- commands


def spawn(command, *args):
    """Run another helper command detached, so the caller gets its answer now.
    Its output goes to the journal through systemd-cat (`journalctl --user -t
    museum-wallpapers`); inheriting the caller's stderr would keep the shell's
    pipe open, and the shell would wait for the detached process."""
    argv = [sys.executable, os.path.abspath(__file__), command, *args]
    journal = shutil.which("systemd-cat") if Path("/run/systemd/journal/stdout").exists() else None
    if journal:
        argv = [journal, "-t", "museum-wallpapers", *argv]
    subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL if not journal else None,
                     stderr=subprocess.DEVNULL if not journal else None)


def spawn_notify():
    spawn("notify")


def is_due(config):
    state = read_json(STATE / "state.json", {})
    return config["intervalMinutes"] > 0 and time.time() - state.get("setAt", 0) >= config["intervalMinutes"] * 60


def cmd_next(config, _args, only_if_due=False):
    switched, error = False, None
    with lock():
        # Checked again under the lock: a hotkey may have switched meanwhile.
        if not only_if_due or is_due(config):
            error = switch(config)
            switched = True
    print(json.dumps(status(config, error), ensure_ascii=False))
    if switched:
        spawn("prefetch")


def cmd_tick(config, _args):
    if is_due(config):
        cmd_next(config, _args, only_if_due=True)
        return
    handle = lock(wait=False)
    if handle:  # busy switching: re-frame on a later tick
        with handle:
            refit(config)
    print(json.dumps(status(config), ensure_ascii=False))
    # Top the queue up after a start, a failed download or a filter change.
    if prefetch_wanted(config):
        spawn("prefetch")


def cmd_prefetch(config, _args):
    handle = lock(".prefetcher.lock", wait=False)
    if handle is None:
        return  # another process is already filling the queue
    with handle:
        prefetch(config)


def cmd_refresh_pools(config, args):
    """Rebuild the cached painter lists (spawned detached when one is stale);
    without levels, the one for the `fame` setting."""
    levels = args.levels or ([FAME[config["fame"]]] if FAME[config["fame"]] else [])
    for level in levels:
        with timed(f"refresh painters and pool {level}"):
            refresh_fame(level)


def cmd_status(config, _args):
    print(json.dumps(status(config), ensure_ascii=False))


def byline(info):
    return " · ".join(x for x in (info.get("artist"), info.get("year")) if x)


def cmd_notify(config, _args):
    current = status(config)
    info = current["info"]
    if not info:
        painting = current_painting()
        info = info_for(painting, config) if painting else None
    if not info:
        return
    body = "\n".join(x for x in (byline(info), info.get("museum"), "", plain(info.get("summary"), 320)) if x is not None)
    # The Omarchy notification body is StyledText, so escape markup characters.
    result = subprocess.run(
        ["notify-send", "-a", "Museum Wallpapers", "-t", "15000", "-A", f"open={t('read_more', config['language'])}", "--",
         info["title"], html.escape(body.strip(), quote=False)],
        capture_output=True, text=True,
    )
    if result.stdout.strip() == "open":
        cmd_open(config, _args)


def cmd_open(config, _args):
    image = current_painting()
    if image:
        url = info_for(image, config).get("url")
        if url:
            subprocess.Popen(["xdg-open", url], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def table_cell(text):
    return str(text).replace("|", "\\|")


def credit_line(name, info):
    credit = ", ".join(x for x in (info.get("artist"), f"*{info['title']}*", info.get("year"), info.get("museum")) if x)
    source = info.get("commons") or info.get("url")
    return f"| {table_cell(name)} | {table_cell(credit)} ([source]({table_cell(source)})) |\n"


def cmd_keep(config, _args):
    # Each Save click runs its own helper; one at a time, so a double click
    # cannot save twice or lose an index entry.
    with lock(".save.lock"):
        keep(config)


def keep(config):
    image = current_painting()
    if not image or image.parent != CACHE:
        return
    info = info_for(image, config)
    folder = Path(config["saveDir"])
    folder.mkdir(parents=True, exist_ok=True)
    index = saved_index(config)
    if info.get("id") and any(saved.get("id") == info["id"] for saved in index.values() if isinstance(saved, dict)):
        notify(t("already_saved", config["language"]), info["title"])
        return
    # Letters of any script are kept; anything else becomes a dash.
    slug = re.sub(r"[\W_]+", "-", f"{info.get('artist', '')} {info['title']}".lower()).strip("-")[:80] or image.stem
    dest, n = folder / f"{slug}.jpg", 2
    while dest.exists():
        dest, n = folder / f"{slug}-{n}.jpg", n + 1
    shutil.copy2(image, dest)
    index[dest.name] = info
    write_json(folder / ".museum-wallpapers.json", index)
    credits = folder / "CREDITS.md"
    if not credits.exists():
        credits.write_text("# Saved paintings\n\n| File | Painting |\n| --- | --- |\n")
    with open(credits, "a") as out:
        out.write(credit_line(dest.name, info))
    notify(t("saved", config["language"]), info["title"])


def parse_years(text):
    match = re.fullmatch(r"\s*(-?\d+)?\s*(?:-|–|\.\.)\s*(-?\d+)?\s*", text or "")
    if not match:
        raise ValueError(f"years must look like 1850-1900, 1850- or -1900, not {text!r}")
    return as_year(match.group(1)), as_year(match.group(2))


def cmd_filter(config, args):
    path = STATE / "filter.json"
    if args.action == "clear":
        path.unlink(missing_ok=True)
    else:
        override = {"artists": [], "movements": [], "genres": [], "yearFrom": None, "yearTo": None}
        for key, kind, values in (("artists", "artist", args.artist), ("movements", "movement", args.movement),
                                  ("genres", "genre", args.genre)):
            for value in values or []:
                qid = resolve(value, kind, config["language"])
                if not qid:
                    raise NoMatch(f"unknown {kind}: {value}")
                name = args.label if args.label and re.fullmatch(r"Q\d+", value) else value
                override[key].append({"id": qid, "label": name})
        if args.years:
            override["yearFrom"], override["yearTo"] = parse_years(args.years)
        write_json(path, override)
    # The queue is not emptied here: paintings fetched for the old filter no
    # longer match the queue key and are dropped by the next switch, and a
    # download in progress finishes instead of failing half way.
    (CACHE / ".prefetch-failed.json").unlink(missing_ok=True)
    if args.switch:
        cmd_next(config, args)
    else:
        print(json.dumps(status(config), ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="museum.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    for name in ("tick", "next", "prefetch", "status", "notify", "open", "keep"):
        sub.add_parser(name)
    refresh = sub.add_parser("refresh-pools")
    refresh.add_argument("levels", nargs="*", type=int, help="fame levels (sitelink counts), default: the setting's")
    filt = sub.add_parser("filter")
    filt.add_argument("action", choices=["set", "clear"])
    filt.add_argument("--artist", action="append")
    filt.add_argument("--movement", action="append")
    filt.add_argument("--genre", action="append")
    filt.add_argument("--years")
    filt.add_argument("--label", help="display name when a Wikidata id is passed")
    filt.add_argument("--switch", action="store_true", help="switch to a matching painting right away")
    args = parser.parse_args(argv)
    commands = {"tick": cmd_tick, "next": cmd_next, "prefetch": cmd_prefetch, "status": cmd_status,
                "notify": cmd_notify, "open": cmd_open, "keep": cmd_keep, "filter": cmd_filter,
                "refresh-pools": cmd_refresh_pools}
    if args.command not in commands:
        parser.print_help()
        return 2
    config = load_config()
    try:
        commands[args.command](config, args)
    except Exception as exc:  # noqa: BLE001 - the shell still gets a status to show
        print(f"museum-wallpapers: {args.command} failed: {exc!r}", file=sys.stderr)
        if args.command in ("tick", "next", "status", "filter"):
            print(json.dumps(status(config, f"Unexpected error: {exc}"), ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
