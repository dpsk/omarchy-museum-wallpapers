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
  status                print the status JSON the shell plugin renders
  notify                show the current painting as a desktop notification
  open                  open the current painting's article in the browser
  keep                  copy the current painting into saveDir
  filter set [--artist NAME] [--movement NAME] [--genre NAME] [--years FROM-TO]
  filter clear          drop the temporary filter set from the popup

Settings live in the plugin's entry in ~/.config/omarchy/shell.json.
"""

import argparse
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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLUGIN_ID = "io.github.dpsk.museum-wallpapers"
VERSION = "0.1.0"
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
# Commons also hosts tiles cut from very large scans, e.g. "…-x0-y2.jpg".
TILE_RE = re.compile(r"-x\d+-y\d+\.\w+$", re.IGNORECASE)
POOL_MAX_AGE = 7 * 24 * 3600
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
    "language": "en",
    "filters": {"yearFrom": None, "yearTo": None, "artists": [], "movements": [], "genres": []},
    "notifyOnChange": False,
    "placard": "bottom-right",
    "saveDir": "~/Pictures/Wallpapers/Museum",
    "cacheSize": 24,
    "prefetchCount": 3,
    "fit": "blur",
    "fame": "famous",
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
    config["language"] = re.sub(r"[^a-z-]", "", str(config["language"]).lower()) or "en"
    config["notifyOnChange"] = config["notifyOnChange"] is True
    if not isinstance(config["placard"], str) or config["placard"] not in PLACARDS:
        config["placard"] = DEFAULTS["placard"]
    if not isinstance(config["fit"], str) or config["fit"] not in FITS:
        config["fit"] = DEFAULTS["fit"]
    if not isinstance(config["fame"], str) or config["fame"] not in FAME:
        config["fame"] = DEFAULTS["fame"]
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


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
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
    """What a queued painting was fetched for: the active filter, the fame
    setting and the language its title and summary are in."""
    return filter_key({"filters": active_filters(config), "fame": config["fame"], "language": config["language"]})


def has_entity_filters(filters):
    """Filters that need the Wikidata pool rather than random Commons scans."""
    return bool(filters["artists"] or filters["movements"] or filters["genres"] or filters.get("fameArtists"))


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


def famous_painters(min_sitelinks):
    """Painters with articles in at least this many Wikipedias, cached a week."""
    path = CACHE / "pools" / f"painters-{min_sitelinks}.json"
    cached = read_json(path)
    if isinstance(cached, dict) and time.time() - cached.get("at", 0) < POOL_MAX_AGE:
        return cached["ids"]
    query = (f"SELECT ?famed WHERE {{ ?famed wdt:P106 wd:{PAINTER}; wikibase:sitelinks ?links. "
             f"FILTER(?links >= {int(min_sitelinks)}) }}")
    data = http_json(SPARQL_API, data={"query": query}, accept="application/sparql-results+json", timeout=90)
    ids = sorted(row["famed"]["value"].rsplit("/", 1)[-1] for row in data.get("results", {}).get("bindings", []))
    ids = [qid for qid in ids if re.fullmatch(r"Q\d+", qid)]
    if not ids:
        # Never cache this: an empty list would quietly turn `fame` off for a week.
        raise ValueError("Wikidata returned no painters")
    write_json(path, {"at": time.time(), "ids": ids})
    return ids


def file_title_from_url(url):
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    return "File:" + name.replace("_", " ")


_pools = {}


def entity_pool(filters):
    """(painting id, Commons file title) pairs for an entity filter, cached a
    week on disk and kept in memory for the rest of the run."""
    path = CACHE / "pools" / f"{filter_key(filters)}.json"
    if path in _pools:
        return _pools[path]
    _pools[path] = items = _load_pool(path, filters)
    return items


def _load_pool(path, filters):
    pool = read_json(path)
    if isinstance(pool, dict) and time.time() - pool.get("at", 0) < POOL_MAX_AGE:
        return pool["items"]
    data = http_json(SPARQL_API, data={"query": sparql_query(filters)},
                     accept="application/sparql-results+json", timeout=90)
    items = []
    for row in data.get("results", {}).get("bindings", []):
        qid = row["p"]["value"].rsplit("/", 1)[-1]
        items.append([qid, file_title_from_url(row["img"]["value"])])
    write_json(path, {"at": time.time(), "items": items})
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


def image_infos(titles):
    infos = {}
    for batch in chunks(titles, 50):
        data = mw_api(COMMONS_API, action="query", prop="imageinfo", iiprop="size|url|extmetadata",
                      iiextmetadatafilter="ObjectName|Artist|DateTimeOriginal|ImageDescription",
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
        raise NoMatch("no paintings match the filter")
    sample = random.sample(pool, min(len(pool), POOL_SAMPLE))
    infos = image_infos([title for _, title in sample])
    result = []
    for qid, title in sample:
        page = infos.get(title)
        if page and usable(title, page["imageinfo"][0]["width"], page["imageinfo"][0]["height"], config, screen):
            result.append((page, qid))
    if not result and len(pool) <= POOL_SAMPLE:
        raise NoMatch(f"none of the {len(pool)} matching paintings is large enough for this screen")
    return result


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
    return text[:120] or None


def without_quickstatements(text):
    """Drop `label QS:Lde,"…"`-style fragments some Commons pages carry."""
    return re.sub(r'\s*(?:title|label|description) QS:[^,\s]*,(?:[a-z-]+:)?"[^"]*"', "", text).strip()


def commons_description(markup):
    """The file page's description, when it says more than the title."""
    text = without_quickstatements(plain(markup))
    if len(text) < 40:
        return ""
    return text[:400].rsplit(" ", 1)[0] + "…" if len(text) > 400 else text


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
    return first_sentences(data.get("extract", ""), 4), data.get("content_urls", {}).get("desktop", {}).get("page")


def article(entity, language):
    """(language, title) of the best Wikipedia article for an entity."""
    sitelinks = entity.get("sitelinks", {}) if entity else {}
    for lang in dict.fromkeys((language, "en")):
        link = sitelinks.get(f"{lang.replace('-', '_')}wiki")
        if link and link.get("title"):
            return lang, link["title"]
    return None


def describe(page, qid, language):
    """Everything the plugin shows about a painting."""
    image = page["imageinfo"][0]
    meta = image.get("extmetadata", {})
    languages = "|".join(dict.fromkeys((language, "en")))
    sites = "|".join(f"{lang.replace('-', '_')}wiki" for lang in dict.fromkeys((language, "en")))
    # All languages: a painting labelled only in German still gets its title.
    painting = entities([qid], languages=None, site=sites).get(qid, {})
    creators = claim_ids(painting, "P170")
    collections = claim_ids(painting, "P195")
    movements = claim_ids(painting, "P135")
    genres = claim_ids(painting, "P136")
    related = entities(creators[:1] + collections[:1] + genres[:1], props="labels|claims|sitelinks",
                       languages=languages, site=sites)
    artist = related.get(creators[0], {}) if creators else {}
    if not movements and artist:
        movements = claim_ids(artist, "P135")
    movement = entities(movements[:1], props="labels", languages=languages).get(movements[0], {}) if movements else {}

    year = claim_year(painting)
    info = {
        "id": qid,
        "language": language,
        "title": label(painting, language) or object_name(meta.get("ObjectName", {}).get("value"))
        or page["title"][5:],
        "artist": label(artist, language) or plain(meta.get("Artist", {}).get("value"), 80) or "Unknown artist",
        "artistId": creators[0] if creators else None,
        "year": str(year) if year is not None else plain(meta.get("DateTimeOriginal", {}).get("value"), 40),
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
    sources = [("article", painting, ""), ("commons", None, ""), ("article", artist, "About the artist: ")]
    for kind, entity, prefix in sources:
        if kind == "commons":
            info["summary"] = commons_description(meta.get("ImageDescription", {}).get("value"))
        else:
            found = article(entity, language)
            if not found:
                continue
            try:
                summary, url = wikipedia_summary(*found)
            except (OSError, ValueError):
                continue
            if summary:
                info["summary"] = prefix + summary
                info["url"] = url or info["url"]
        if info["summary"]:
            break
    return info


def enqueue(page, qid, config, key, note=None):
    """Put one painting into the queue; returns its path, or None to skip it.
    `note` explains a random painting queued because the filter failed."""
    name = f"commons-{page['pageid']}"
    QUEUE.mkdir(parents=True, exist_ok=True)
    dest = QUEUE / f"{name}.jpg"
    cached = CACHE / f"{name}.jpg"
    info = read_json(cached.with_suffix(".json")) if cached.exists() else None
    thumb = None
    if info and info.get("language") != config["language"]:
        info = describe(page, qid, config["language"])
    if not info:
        data = mw_api(COMMONS_API, action="query", prop="imageinfo", iiprop="url",
                      iiurlwidth=min(page["imageinfo"][0]["width"], MAX_THUMB_WIDTH), pageids=page["pageid"])
        thumb = data["query"]["pages"][0]["imageinfo"][0]
        # Without a rendered thumbnail the original could be hundreds of MB.
        if not thumb.get("thumburl"):
            return None
        info = describe(page, qid, config["language"])
    # The sidecar goes first: it claims the painting for this process (see
    # fetch_next), and the queue only lists *.jpg, so an image whose sidecar
    # is missing would be taken for a stale one and deleted.
    info["filter"] = key
    if note:
        info["filterError"] = note
    else:
        info.pop("filterError", None)
    write_json(dest.with_suffix(".json"), info)
    try:
        if thumb:
            download(thumb["thumburl"], dest)
        else:
            tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
            shutil.copyfile(cached, tmp)
            tmp.replace(dest)
    except BaseException:
        dest.with_suffix(".json").unlink(missing_ok=True)
        raise
    os.utime(dest)  # queue order is download order
    os.utime(dest.with_suffix(".json"))  # a slow download is not an orphan
    return dest


def fetch_next(config, unfiltered=False, note=None):
    """Download one new painting into the queue; returns its image path."""
    filters = active_filters(config)
    key = queue_key(config)
    screen = screen_size()
    if unfiltered:
        filters = normalize_filters({})
    filters, unknown = resolve_filters(filters, config["language"])
    if unknown:
        raise NoMatch("unknown filter value: " + ", ".join(unknown))
    # An explicit artist (or "More by …") is shown however famous they are.
    if config["fame"] != "any" and not filters["artists"]:
        filters["fameArtists"] = famous_painters(FAME[config["fame"]])
    # The queue first: a switch moving an image from the queue to the cache
    # between the two globs then still leaves it in one of them. Sidecars
    # count, so a painting another process is downloading is not taken twice.
    queue_claims = {p.stem for p in QUEUE.glob("*.json")} | {p.stem for p in QUEUE.glob("*.jpg")}
    shown = {p.stem for p in CACHE.glob("*.jpg")}
    seen = queue_claims | shown
    current = current_painting()
    repeats, current_matched, queued_matched = [], False, False
    for _ in range(SEARCH_ROUNDS):
        if has_entity_filters(filters):
            found = filtered_candidates(filters, config, screen)
            exhaustive = len(entity_pool(filters)) <= POOL_SAMPLE
        else:
            found = random_candidates(filters, config, screen)
            exhaustive = False
        for page, qid in found:
            name = f"commons-{page['pageid']}"
            if name in seen:
                if name in queue_claims:
                    queued_matched = True
                elif current and current.stem == name:
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
        dest = enqueue(page, qid, config, key, note)
        if dest:
            return dest
    if queued_matched:
        raise AllQueued("every matching painting is already queued")
    if current_matched:
        raise OnlyCurrent("the only matching painting is already showing")
    if has_entity_filters(filters):
        raise NoMatch("no matching painting is large enough for this screen")
    if filters["yearFrom"] is not None or filters["yearTo"] is not None:
        raise NoMatch("no painting from these years turned up among random scans")
    raise NoMatch("no suitable painting found")


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
    try:
        screen = screen_size()
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
        out = DISPLAY / f"{original.stem}.{fit}.jpg"
        tmp = out.with_name(f"{out.name}.{os.getpid()}.part")
        try:
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
    sidecar = read_json(image.with_suffix(".json")) if image.parent == CACHE else None
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
        "config": {key: config[key] for key in ("intervalMinutes", "notifyOnChange", "placard", "language", "fit",
                                                "fame")},
        "error": error or state.get("error") or "",
    }


def prune(config):
    images = sorted(CACHE.glob("commons-*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    current, shown = current_painting(), current_background()
    for image in images[config["cacheSize"]:]:
        if image != current:
            image.unlink(missing_ok=True)
            image.with_suffix(".json").unlink(missing_ok=True)
    for framed in DISPLAY.glob("*.jpg") if DISPLAY.exists() else []:
        if framed != shown:
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


def record(image, error="", error_kind="", original=None, fit=None):
    """Remember what was set and when; setAt also delays the next try after a
    failure. `original` is the painting behind a framed copy, `fit` the mode
    it was framed with (kept from before when not given)."""
    previous = read_json(STATE / "state.json", {})
    write_json(STATE / "state.json", {"image": os.path.realpath(image) if image else "",
                                      "original": os.path.realpath(original or image) if (original or image) else "",
                                      "fit": fit or previous.get("fit"),
                                      "setAt": time.time(), "error": error, "errorKind": error_kind})


def refit(config):
    """Re-frame the painting on screen after the `fit` setting changed,
    without restarting the rotation interval."""
    state = read_json(STATE / "state.json", {})
    # Paintings set before `fit` existed were shown unframed.
    if (state.get("fit") or "cover") == config["fit"] or not status(config)["active"]:
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
                                      "fit": config["fit"]})
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
    briefly. `.prefetcher.lock` keeps a single process filling the queue.
    `.download.lock` is held around each fetch, by the prefetcher and by a
    switch that finds the queue empty, so two processes never pick the same
    painting; a switch waits for at most one download, and for none while the
    queue has an image."""
    CACHE.mkdir(parents=True, exist_ok=True)
    handle = open(CACHE / name, "w")
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
    image = queued(config)
    downloader = None
    if not image:
        # Never download beside the prefetcher: wait for the painting it is
        # fetching, or fetch while holding its lock, so both cannot pick the
        # same painting.
        downloader = lock(".download.lock")
        image = queued(config)
    try:
        if not image:
            try:
                image = fetch_next(config)
            except (OnlyCurrent, AllQueued):
                # Nothing to change to yet; keep it without an error until the next interval.
                record_current(config)
                return ""
            except NoMatch as exc:
                error, error_kind = f"Filter: {exc}; showing a random painting", "filter"
                try:
                    image = fetch_next(config, unfiltered=True)
                except NoMatch as inner:
                    error = f"Filter: {exc}; and no other painting either: {inner}"
                except Exception as inner:  # noqa: BLE001 - any failure falls back below
                    error, error_kind = f"Filter: {exc}; and could not reach Wikimedia: {inner}", "network"
            except Exception as exc:  # noqa: BLE001 - truncated reads, HTTP errors, bad JSON
                error, error_kind = f"Could not reach Wikimedia: {exc}", "network"
    finally:
        if downloader:
            downloader.close()
    report(error, error_kind)
    if not image:
        local_fallback(config, error, error_kind)
        return error
    target = CACHE / image.name
    try:
        image.with_suffix(".json").replace(target.with_suffix(".json"))
        image.replace(target)
        os.utime(target)
        shown = show(target, config)
    except (OSError, subprocess.SubprocessError) as exc:
        error, error_kind = f"Could not set the background: {exc}", "background"
        report(error, error_kind)
        record_current(config, error, error_kind)
        return error
    filter_error = read_json(target.with_suffix(".json"), {}).get("filterError")
    if filter_error and not error:
        error, error_kind = f"Filter: {filter_error}; showing a random painting", "filter"
        report(error, error_kind)
    record(shown, error, error_kind, original=target, fit=config["fit"])
    prune(config)
    if config["notifyOnChange"]:
        spawn_notify()
    return error


def prefetch(config):
    """Fill the queue up to prefetchCount for the active filter. Like a
    switch, a filter that matches nothing gets random paintings; a failure
    is stamped so the tick backs off instead of retrying every minute."""
    stamp = CACHE / ".prefetch-failed.json"
    for _ in range(config["prefetchCount"] * 2):
        if len(queue_items(config)) >= config["prefetchCount"]:
            break
        # Taken per attempt: a failure under filter A must not hold back
        # filter B set from the popup while A was downloading.
        key = queue_key(config)
        try:
            try:
                with lock(".download.lock"):
                    fetch_next(config)
            except (OnlyCurrent, AllQueued):
                # A small pool: all it has is queued or showing. The next
                # switch prefetches again; the tick need not ask every minute.
                write_json(stamp, {"at": time.time(), "filter": key, "reason": "small pool"})
                return
            except NoMatch as exc:
                with lock(".download.lock"):
                    fetch_next(config, unfiltered=True, note=str(exc))
        except Exception as exc:  # noqa: BLE001 - retried after the backoff
            print(f"museum-wallpapers: prefetch failed: {exc}", file=sys.stderr)
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


def spawn(command):
    """Run another helper command detached, so the caller gets its answer now."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), command], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        ["notify-send", "-a", "Museum Wallpapers", "-t", "15000", "-A", "open=Read more", "--",
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
    image = current_painting()
    if not image or image.parent != CACHE:
        return
    info = info_for(image, config)
    folder = Path(config["saveDir"])
    folder.mkdir(parents=True, exist_ok=True)
    index = saved_index(config)
    if info.get("id") and any(saved.get("id") == info["id"] for saved in index.values() if isinstance(saved, dict)):
        notify("Already in your collection", info["title"])
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
    notify("Saved to your collection", info["title"])


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
    with lock():
        for image in QUEUE.glob("*") if QUEUE.exists() else []:
            image.unlink(missing_ok=True)
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
                "notify": cmd_notify, "open": cmd_open, "keep": cmd_keep, "filter": cmd_filter}
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
