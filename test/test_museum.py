import http.client
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("museum", ROOT / "museum.py")
museum = importlib.util.module_from_spec(spec)
spec.loader.exec_module(museum)

CONFIG = museum.normalize_config({})
SCREEN = (3840, 2160)


def claim(prop, value):
    return {prop: [{"mainsnak": {"datavalue": {"value": value}}}]}


class ConfigTest(unittest.TestCase):
    def test_entry_is_found_by_id(self):
        text = json.dumps({"plugins": [{"id": "other"}, {"id": museum.PLUGIN_ID, "placard": "bottom-left"}]})
        self.assertEqual(museum.config_entry(text)["placard"], "bottom-left")

    def test_bar_widget_entry_overrides_plugins_entry(self):
        text = json.dumps({
            "plugins": [{"id": museum.PLUGIN_ID, "intervalMinutes": 60, "placard": "bottom-left"}],
            "bar": {"layout": {"right": [{"id": "omarchy.clock"}, {"id": museum.PLUGIN_ID, "intervalMinutes": 15}]}},
        })
        entry = museum.config_entry(text)
        self.assertEqual(entry["intervalMinutes"], 15)
        self.assertEqual(entry["placard"], "bottom-left")

    def test_missing_or_broken_shell_json_gives_defaults(self):
        self.assertEqual(museum.config_entry("not json"), {})
        self.assertEqual(museum.config_entry(json.dumps({"plugins": "nope"})), {})
        self.assertEqual(museum.normalize_config({})["intervalMinutes"], 60)

    def test_values_are_normalized(self):
        config = museum.normalize_config({
            "intervalMinutes": "-5", "placard": "middle", "language": "RU",
            "notifyOnChange": "yes", "cacheSize": 0, "saveDir": "~/Art",
            "filters": {"artists": "Claude Monet", "yearFrom": "1850", "genres": ["", "landscape"]},
        })
        self.assertEqual(config["intervalMinutes"], 0)
        self.assertEqual(config["placard"], "bottom-right")
        self.assertEqual(config["language"], "ru")
        self.assertFalse(config["notifyOnChange"])
        self.assertEqual(config["cacheSize"], 2)
        self.assertEqual(config["saveDir"], str(Path.home() / "Art"))
        self.assertEqual(config["filters"]["artists"], ["Claude Monet"])
        self.assertEqual(config["filters"]["yearFrom"], 1850)
        self.assertEqual(config["filters"]["genres"], ["landscape"])

    def test_filter_key_is_stable(self):
        a = museum.normalize_filters({"artists": ["Q296"]})
        b = museum.normalize_filters({"artists": ["Q296"]})
        self.assertEqual(museum.filter_key(a), museum.filter_key(b))
        self.assertNotEqual(museum.filter_key(a), museum.filter_key(museum.normalize_filters({})))


class OverrideTest(unittest.TestCase):
    def test_popup_filter_replaces_configured_filter(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(museum, "STATE", Path(tmp)):
            config = museum.normalize_config({"filters": {"artists": ["Rembrandt"]}})
            self.assertEqual(museum.active_filters(config)["artists"], ["Rembrandt"])
            museum.write_json(Path(tmp) / "filter.json", {"artists": [{"id": "Q296", "label": "Claude Monet"}]})
            active = museum.active_filters(config)
            self.assertEqual(active["artists"], ["Q296"])
            self.assertEqual(active["movements"], [])


class PickingTest(unittest.TestCase):
    def test_landscape_scan_is_usable(self):
        self.assertTrue(museum.usable("File:A - Google Art Project.jpg", 6271, 4332, CONFIG, SCREEN))
        self.assertTrue(museum.usable("File:Wide.jpg", 7600, 3800, CONFIG, SCREEN))

    def test_portrait_panorama_small_and_tiles_are_not(self):
        self.assertFalse(museum.usable("File:Portrait.jpg", 4413, 5338, CONFIG, SCREEN))
        self.assertFalse(museum.usable("File:Saudade.jpg", 29696, 1254, CONFIG, SCREEN))
        self.assertFalse(museum.usable("File:Small.jpg", 2048, 1400, CONFIG, SCREEN))
        self.assertFalse(museum.usable("File:Stuart - Google Art Project-x0-y2.jpg", 3840, 2505, CONFIG, SCREEN))
        self.assertFalse(museum.usable("File:Empty.jpg", 0, 0, CONFIG, SCREEN))

    def test_smaller_screen_accepts_smaller_scans(self):
        self.assertTrue(museum.usable("File:Small.jpg", 2048, 1300, CONFIG, (1920, 1080)))

    def test_year_range(self):
        filters = museum.normalize_filters({"yearFrom": 1850, "yearTo": 1900})
        self.assertTrue(museum.year_matches(1873, filters))
        self.assertFalse(museum.year_matches(1901, filters))
        self.assertFalse(museum.year_matches(None, filters))
        self.assertTrue(museum.year_matches(None, museum.normalize_filters({})))

    def test_sparql_combines_filters(self):
        query = museum.sparql_query(museum.normalize_filters(
            {"artists": ["Q296"], "movements": ["Q40415"], "genres": ["Q191163"], "yearFrom": 1870}))
        self.assertIn("VALUES ?artist { wd:Q296 }", query)
        self.assertIn("UNION { ?p wdt:P170 ?creator. ?creator wdt:P135 ?movement }", query)
        self.assertIn("VALUES ?genre { wd:Q191163 }", query)
        self.assertIn("FILTER(YEAR(?date) >= 1870)", query)
        self.assertNotIn("<=", query)

    def test_commons_file_title_from_sparql_url(self):
        url = "http://commons.wikimedia.org/wiki/Special:FilePath/Claude%20Monet%20-%20L%27%C3%89t%C3%A9.jpg"
        self.assertEqual(museum.file_title_from_url(url), "File:Claude Monet - L'Été.jpg")

    def test_genre_names_resolve_without_network(self):
        self.assertEqual(museum.resolve("Landscape", "genre"), "Q191163")
        self.assertEqual(museum.resolve("Q296", "artist"), "Q296")


class WikidataTest(unittest.TestCase):
    def test_claims_and_years(self):
        entity = {"claims": {**claim("P31", {"id": "Q3305213"}), **claim("P571", {"time": "+1873-00-00T00:00:00Z"})}}
        self.assertTrue(museum.is_painting(entity))
        self.assertEqual(museum.claim_year(entity), 1873)
        self.assertEqual(museum.claim_year({"claims": claim("P571", {"time": "-0350-00-00T00:00:00Z"})}), -350)

    def test_commons_media_info_uses_statements(self):
        media = {"statements": claim("P6243", {"id": "Q23758876"})}
        self.assertEqual(museum.claim_ids(media, "P6243"), ["Q23758876"])

    def test_label_falls_back_to_any_language(self):
        entity = {"labels": {"de": {"value": "Stürmische See im Abendrot"}}}
        self.assertEqual(museum.label(entity, "en"), "Stürmische See im Abendrot")
        self.assertEqual(museum.label({"labels": {"mul": {"value": "X"}, "de": {"value": "Y"}}}, "en"), "X")

    def test_object_name_drops_quickstatements(self):
        raw = ('German: Stürmische See im Abendrot. title QS:P1476,de:"Stürmische See im Abendrot." '
               'label QS:Lde,"Stürmische See im Abendrot."')
        self.assertEqual(museum.object_name(raw), "Stürmische See im Abendrot")
        self.assertEqual(museum.object_name("The Ninth Wave"), "The Ninth Wave")
        self.assertEqual(museum.object_name("Nocturne: Blue and Silver – Chelsea"), "Nocturne: Blue and Silver – Chelsea")
        self.assertEqual(museum.object_name("German: Die Welle English: The Wave"), "The Wave")
        self.assertEqual(museum.object_name("Norwegian Bokmål: Skrik"), "Skrik")
        self.assertEqual(museum.object_name("The English: A Portrait"), "The English: A Portrait")
        self.assertIsNone(museum.object_name(""))

    def test_commons_description_without_quickstatements(self):
        raw = ('Reval (Tallinn) label QS:Len,"Reval (Tallinn)" label QS:Lde,"Reval (heute Tallinn) in Estland" '
               'label QS:Lru,"Ревель (Таллин)"')
        self.assertEqual(museum.without_quickstatements(raw), "Reval (Tallinn)")
        self.assertEqual(museum.commons_description(raw), "")
        long = "A calm evening over the harbour, with fishing boats returning under a pale yellow sky."
        self.assertEqual(museum.commons_description(long), long)

    def test_label_and_article_fall_back_to_english(self):
        entity = {"labels": {"en": {"value": "The Hay Wain"}}, "sitelinks": {"enwiki": {"title": "The Hay Wain"}}}
        self.assertEqual(museum.label(entity, "ru"), "The Hay Wain")
        self.assertEqual(museum.article(entity, "ru"), ("en", "The Hay Wain"))
        self.assertIsNone(museum.article({}, "en"))


class TextTest(unittest.TestCase):
    def test_plain_strips_markup(self):
        self.assertEqual(museum.plain('<div class="fn">Man of <b>War</b> &amp; Rocks</div>'), "Man of War & Rocks")
        self.assertEqual(museum.plain("one two three", 8), "one two…")

    def test_first_sentences(self):
        text = "First one. Second one! Third? Fourth."
        self.assertEqual(museum.first_sentences(text, 2), "First one. Second one!")
        self.assertEqual(museum.first_sentences("Painted c. 1870 in St. Petersburg. Next.", 1),
                         "Painted c. 1870 in St. Petersburg.")
        self.assertEqual(museum.first_sentences("By J. M. W. Turner. Next.", 1), "By J. M. W. Turner.")

    def test_parse_years(self):
        self.assertEqual(museum.parse_years("1850-1900"), (1850, 1900))
        self.assertEqual(museum.parse_years("1850-"), (1850, None))
        self.assertEqual(museum.parse_years("-1900"), (None, 1900))
        with self.assertRaises(ValueError):
            museum.parse_years("eighteen fifty")

    def test_credit_line(self):
        line = museum.credit_line("monet.jpg", {"artist": "Claude Monet", "title": "Poppies", "year": "1873",
                                                "commons": "https://commons.wikimedia.org/wiki/File:X.jpg"})
        self.assertEqual(line, "| monet.jpg | Claude Monet, *Poppies*, 1873 "
                               "([source](https://commons.wikimedia.org/wiki/File:X.jpg)) |\n")

    def test_local_credit_reads_credits_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "CREDITS.md").write_text("| File | Painting |\n| --- | --- |\n"
                                               "| hay.jpg | John Constable, *The Hay Wain*, 1821 |\n")
            self.assertEqual(museum.local_credit(folder / "hay.jpg"), "John Constable, The Hay Wain, 1821")
            self.assertIsNone(museum.local_credit(folder / "other.jpg"))


class EdgeCaseTest(unittest.TestCase):
    def test_fit_setting(self):
        self.assertEqual(museum.normalize_config({})["fit"], "blur")
        self.assertEqual(museum.normalize_config({"fit": "wall"})["fit"], "wall")
        self.assertEqual(museum.normalize_config({"fit": "stretch"})["fit"], "blur")

    def test_fame_setting(self):
        self.assertEqual(museum.normalize_config({})["fame"], "famous")
        self.assertEqual(museum.normalize_config({"fame": "known"})["fame"], "known")
        self.assertEqual(museum.normalize_config({"fame": "legendary"})["fame"], "famous")

    def test_fame_is_part_of_the_queue_key(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(museum, "STATE", Path(tmp)):
            famous = museum.queue_key(museum.normalize_config({"fame": "famous"}))
            self.assertNotEqual(famous, museum.queue_key(museum.normalize_config({"fame": "any"})))

    def test_fame_list_goes_into_the_pool_query(self):
        filters = museum.normalize_filters({"movements": ["Q40415"]})
        filters["fameArtists"] = ["Q5582", "Q41264"]
        query = museum.sparql_query(filters)
        self.assertIn("VALUES ?famed { wd:Q5582 wd:Q41264 } ?p wdt:P170 ?famed.", query)
        self.assertTrue(museum.has_entity_filters(filters))
        filters["fameArtists"] = ["Q1 }"]
        with self.assertRaises(ValueError):
            museum.sparql_query(filters)

    def test_named_artists_skip_the_fame_list(self):
        config = museum.normalize_config({"fame": "famous"})
        with mock.patch.object(museum, "active_filters", return_value=museum.normalize_filters({"artists": ["Q296"]})), \
             mock.patch.object(museum, "famous_painters") as famous, \
             mock.patch.object(museum, "screen_size", return_value=SCREEN), \
             mock.patch.object(museum, "filtered_candidates", side_effect=museum.NoMatch("stop")):
            with self.assertRaises(museum.NoMatch):
                museum.fetch_next(config)
        famous.assert_not_called()

    def test_an_empty_painter_list_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(museum, "CACHE", Path(tmp)), \
             mock.patch.object(museum, "http_json", return_value={"results": {"bindings": []}}):
            with self.assertRaises(ValueError):
                museum.famous_painters(60)
            self.assertFalse((Path(tmp) / "pools" / "painters-60.json").exists())

    def test_crop_fraction(self):
        self.assertAlmostEqual(museum.crop_fraction(3840, 2160, SCREEN), 0)
        self.assertAlmostEqual(museum.crop_fraction(5090, 3420, SCREEN), 0.163, places=3)

    def test_bad_placard_type_does_not_crash(self):
        self.assertEqual(museum.normalize_config({"placard": []})["placard"], "bottom-right")
        self.assertEqual(museum.normalize_config({"placard": "off"})["placard"], "off")

    def test_sparql_rejects_non_ids(self):
        with self.assertRaises(ValueError):
            museum.sparql_query(museum.normalize_filters({"artists": ["Q1 } ?x ?y ?z {"]}))

    def test_empty_popup_filter_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(museum, "STATE", Path(tmp)):
            museum.write_json(Path(tmp) / "filter.json", {"artists": [], "movements": [], "genres": [],
                                                          "yearFrom": None, "yearTo": None})
            self.assertIsNone(museum.override_filters())

    def test_pipes_survive_the_credits_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            info = {"artist": "A", "title": "Left | Right", "commons": "https://example.org/x"}
            (folder / "CREDITS.md").write_text("| File | Painting |\n| --- | --- |\n" + museum.credit_line("x.jpg", info))
            self.assertEqual(museum.local_credit(folder / "x.jpg"), "A, Left | Right")


class SwitchTest(unittest.TestCase):
    """switch() and fetch_next() against temporary state, without network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.cache, self.state = root / "cache", root / "state"
        self.cache.mkdir()
        self.link = root / "background"
        self.patches = [
            mock.patch.object(museum, "CACHE", self.cache),
            mock.patch.object(museum, "QUEUE", self.cache / "queue"),
            mock.patch.object(museum, "DISPLAY", self.cache / "display"),
            mock.patch.object(museum, "STATE", self.state),
            mock.patch.object(museum, "CURRENT_LINK", self.link),
            mock.patch.object(museum, "screen_size", return_value=SCREEN),
            mock.patch.object(museum, "spawn_notify"),
        ]
        for patch in self.patches:
            patch.start()
        self.notify = mock.patch.object(museum.subprocess, "run").start()
        # Framing has its own tests; here the painting is shown as is.
        self.config = museum.normalize_config({"saveDir": str(root / "saved"), "fit": "cover", "fame": "any"})

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def set_link(self, target):
        self.link.unlink(missing_ok=True)
        self.link.symlink_to(target)

    def test_truncated_download_is_recorded_not_raised(self):
        with mock.patch.object(museum, "fetch_next", side_effect=http.client.IncompleteRead(b"")):
            error = museum.switch(self.config)
            self.assertIn("Could not reach Wikimedia", error)
            state = museum.read_json(self.state / "state.json")
            self.assertEqual(state["errorKind"], "network")
            self.assertLess(time.time() - state["setAt"], 5)
            self.assertFalse(museum.is_due(museum.normalize_config({})))
            museum.switch(self.config)
        notifications = [c for c in self.notify.call_args_list if c.args[0][0] == "notify-send"]
        self.assertEqual(len(notifications), 1)

    def test_failing_background_setter_is_recorded(self):
        queue = self.cache / "queue"
        queue.mkdir()
        (queue / "commons-1.jpg").write_bytes(b"jpg")
        museum.write_json(queue / "commons-1.json", {"title": "T", "filter": museum.queue_key(self.config)})
        failure = subprocess.CalledProcessError(1, ["omarchy-theme-bg-set"])
        with mock.patch.object(museum, "set_background", side_effect=failure):
            error = museum.switch(self.config)
        self.assertIn("Could not set the background", error)
        self.assertEqual(museum.read_json(self.state / "state.json")["errorKind"], "background")

    def test_symlinked_paths_still_count_as_ours(self):
        saved = Path(os.path.realpath(self.tmp.name)) / "saved"
        saved.mkdir()
        (saved / "hay.jpg").write_bytes(b"jpg")
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(saved)
        museum.record(alias / "hay.jpg")
        self.set_link(saved / "hay.jpg")
        self.assertTrue(museum.status(self.config)["active"])
        self.assertFalse(museum.status(self.config)["fromCache"])

    def test_small_pool_repeats_instead_of_failing(self):
        (self.cache / "commons-7.jpg").write_bytes(b"jpg")
        page = {"pageid": 7, "title": "File:X.jpg", "imageinfo": [{"width": 4000, "height": 2500}]}
        filters = museum.normalize_filters({"artists": ["Q296"]})
        with mock.patch.object(museum, "active_filters", return_value=filters), \
             mock.patch.object(museum, "filtered_candidates", return_value=[(page, "Q1")]), \
             mock.patch.object(museum, "entity_pool", return_value=[["Q1", "File:X.jpg"]]), \
             mock.patch.object(museum, "enqueue", return_value=self.cache / "queue" / "commons-7.jpg") as enqueue:
            self.assertEqual(museum.fetch_next(self.config).name, "commons-7.jpg")
        enqueue.assert_called_once()

    def test_single_match_already_showing_keeps_it_quietly(self):
        current = self.cache / "commons-7.jpg"
        current.write_bytes(b"jpg")
        self.set_link(current)
        page = {"pageid": 7, "title": "File:X.jpg", "imageinfo": [{"width": 4000, "height": 2500}]}
        filters = museum.normalize_filters({"artists": ["Q296"]})
        with mock.patch.object(museum, "active_filters", return_value=filters), \
             mock.patch.object(museum, "filtered_candidates", return_value=[(page, "Q1")]), \
             mock.patch.object(museum, "entity_pool", return_value=[["Q1", "File:X.jpg"]]), \
             mock.patch.object(museum, "set_background") as set_background:
            self.assertEqual(museum.switch(self.config), "")
        set_background.assert_not_called()
        self.assertEqual(museum.read_json(self.state / "state.json")["errorKind"], "")

    def test_offline_fallback_cycles_downloaded_paintings(self):
        for name in ("commons-1.jpg", "commons-2.jpg"):
            (self.cache / name).write_bytes(b"jpg")
        self.set_link(self.cache / "commons-1.jpg")
        with mock.patch.object(museum, "fetch_next", side_effect=OSError("offline")), \
             mock.patch.object(museum, "set_background") as set_background:
            museum.switch(self.config)
        set_background.assert_called_once_with(self.cache / "commons-2.jpg")

    def queue_image(self, name, key, age):
        queue = self.cache / "queue"
        queue.mkdir(exist_ok=True)
        image = queue / f"{name}.jpg"
        museum.write_json(image.with_suffix(".json"), {"title": name, "filter": key})
        image.write_bytes(b"jpg")
        os.utime(image, (age, age))
        return image

    def test_queue_serves_oldest_and_drops_stale(self):
        key = museum.queue_key(self.config)
        newer = self.queue_image("commons-2", key, 2000)
        older = self.queue_image("commons-1", key, 1000)
        stale = self.queue_image("commons-3", "other-filter", 500)
        self.assertEqual(museum.queue_items(self.config), [older, newer])
        self.assertEqual(museum.queued(self.config), older)
        self.assertFalse(stale.exists())

    def test_prefetch_fills_the_queue_to_count(self):
        key = museum.queue_key(self.config)
        made = []

        def fake_fetch(_config):
            made.append(self.queue_image(f"commons-{len(made) + 1}", key, 1000 + len(made)))
            return made[-1]

        with mock.patch.object(museum, "fetch_next", side_effect=fake_fetch):
            museum.prefetch(self.config)
        self.assertEqual(len(museum.queue_items(self.config)), 3)

    def test_second_prefetcher_backs_off(self):
        first = museum.lock(".prefetcher.lock", wait=False)
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(museum.lock(".prefetcher.lock", wait=False))
        finally:
            first.close()

    def small_pool(self, *ids):
        pages = [({"pageid": i, "title": f"File:{i}.jpg", "imageinfo": [{"width": 4000, "height": 2500}]}, f"Q{i}")
                 for i in ids]
        filters = museum.normalize_filters({"artists": ["Q296"]})
        return [mock.patch.object(museum, "active_filters", return_value=filters),
                mock.patch.object(museum, "filtered_candidates", return_value=pages),
                mock.patch.object(museum, "entity_pool", return_value=[[q, p["title"]] for p, q in pages])]

    def test_queued_paintings_are_not_fetched_again(self):
        (self.cache / "commons-8.jpg").write_bytes(b"jpg")
        self.set_link(self.cache / "commons-8.jpg")
        self.queue_image("commons-7", "any", 1000)
        patches = self.small_pool(7, 8)
        for patch in patches:
            patch.start()
        with mock.patch.object(museum, "enqueue") as enqueue:
            with self.assertRaises(museum.AllQueued):
                museum.fetch_next(self.config)
            museum.prefetch(self.config)
        enqueue.assert_not_called()

    def test_a_download_in_flight_claims_its_painting(self):
        queue = self.cache / "queue"
        queue.mkdir()
        museum.write_json(queue / "commons-7.json", {"title": "in flight"})
        for patch in self.small_pool(7):
            patch.start()
        with mock.patch.object(museum, "enqueue") as enqueue, self.assertRaises(museum.AllQueued):
            museum.fetch_next(self.config)
        enqueue.assert_not_called()

    def test_orphan_sidecars_are_cleaned(self):
        queue = self.cache / "queue"
        queue.mkdir()
        orphan = queue / "commons-9.json"
        museum.write_json(orphan, {"title": "killed"})
        os.utime(orphan, (1000, 1000))
        fresh = queue / "commons-10.json"
        museum.write_json(fresh, {"title": "downloading"})
        museum.queued(self.config)
        self.assertFalse(orphan.exists())
        self.assertTrue(fresh.exists())

    def test_prefetch_falls_back_to_random_and_backs_off_on_failure(self):
        calls = []

        def fetch(_config, unfiltered=False, note=None):
            calls.append(unfiltered)
            if not unfiltered:
                raise museum.NoMatch("nothing")
            raise OSError("offline")

        with mock.patch.object(museum, "fetch_next", side_effect=fetch):
            museum.prefetch(self.config)
        self.assertEqual(calls, [False, True])
        self.assertFalse(museum.prefetch_wanted(self.config))
        museum.write_json(self.cache / ".prefetch-failed.json", {"at": time.time() - 3600, "filter": "x"})
        self.assertTrue(museum.prefetch_wanted(self.config))

    def test_small_pool_prefetch_backs_off(self):
        with mock.patch.object(museum, "fetch_next", side_effect=museum.AllQueued("all queued")):
            museum.prefetch(self.config)
        self.assertFalse(museum.prefetch_wanted(self.config))

    def test_queued_random_painting_still_reports_the_filter(self):
        key = museum.queue_key(self.config)
        image = self.queue_image("commons-99", key, 1000)
        museum.write_json(image.with_suffix(".json"), {"title": "R", "filter": key, "filterError": "nothing matches"})
        with mock.patch.object(museum, "set_background"):
            error = museum.switch(self.config)
        self.assertIn("nothing matches", error)
        self.assertEqual(museum.read_json(self.state / "state.json")["errorKind"], "filter")

    def test_failure_is_stamped_for_the_filter_it_happened_under(self):
        def fetch(_config, unfiltered=False, note=None):
            museum.write_json(self.state / "filter.json", {"artists": [{"id": "Q296", "label": "Monet"}]})
            raise OSError("queue wiped")

        old_key = museum.queue_key(self.config)
        with mock.patch.object(museum, "fetch_next", side_effect=fetch):
            museum.prefetch(self.config)
        self.assertEqual(museum.read_json(self.cache / ".prefetch-failed.json")["filter"], old_key)
        self.assertTrue(museum.prefetch_wanted(self.config))

    def test_language_change_retires_the_queue(self):
        image = self.queue_image("commons-5", museum.queue_key(self.config), 1000)
        german = museum.normalize_config({"saveDir": self.config["saveDir"], "language": "de"})
        self.assertEqual(museum.queue_items(self.config), [image])
        self.assertEqual(museum.queue_items(german), [])

    def fake_magick(self, command, **_kwargs):
        if command[:2] == ["magick", "identify"]:
            return subprocess.CompletedProcess(command, 0, stdout="3840 2580")
        Path(command[-1].removeprefix("jpg:")).write_bytes(b"framed")
        return subprocess.CompletedProcess(command, 0)

    def test_fit_modes(self):
        painting = self.cache / "commons-5.jpg"
        painting.write_bytes(b"jpg")
        cover = museum.normalize_config({"fit": "cover"})
        self.assertEqual(museum.render(painting, cover), painting)
        with mock.patch.object(museum.subprocess, "run", side_effect=self.fake_magick) as run:
            blurred = museum.render(painting, museum.normalize_config({"fit": "blur"}))
            self.assertEqual(blurred, self.cache / "display" / "commons-5.blur.jpg")
            self.assertIn("-blur", run.call_args.args[0])
            wall = museum.render(painting, museum.normalize_config({"fit": "wall"}))
            self.assertIn(f"xc:{museum.WALL_COLOR}", run.call_args.args[0])
            self.assertEqual(wall.name, "commons-5.wall.jpg")
        self.assertEqual(list((self.cache / "display").glob("*.part")), [])

    def test_auto_fit_frames_only_heavy_crops(self):
        painting = self.cache / "commons-5.jpg"
        painting.write_bytes(b"jpg")
        auto = museum.normalize_config({"fit": "auto"})
        with mock.patch.object(museum, "image_size", return_value=(3840, 2200)):
            self.assertEqual(museum.render(painting, auto), painting)
        with mock.patch.object(museum, "image_size", return_value=(3840, 2925)), \
             mock.patch.object(museum.subprocess, "run", side_effect=self.fake_magick):
            self.assertEqual(museum.render(painting, auto).name, "commons-5.auto.jpg")

    def test_missing_imagemagick_shows_the_original(self):
        painting = self.cache / "commons-5.jpg"
        with mock.patch.object(museum.subprocess, "run", side_effect=FileNotFoundError("magick")):
            self.assertEqual(museum.render(painting, museum.normalize_config({})), painting)

    def test_status_describes_the_painting_behind_a_framed_copy(self):
        painting = self.cache / "commons-5.jpg"
        painting.write_bytes(b"jpg")
        museum.write_json(painting.with_suffix(".json"), {"title": "The Ninth Wave"})
        framed = self.cache / "display" / "commons-5.blur.jpg"
        framed.parent.mkdir()
        framed.write_bytes(b"framed")
        stale = self.cache / "display" / "commons-4.blur.jpg"
        stale.write_bytes(b"old")
        self.set_link(framed)
        museum.record(framed, original=painting)
        current = museum.status(self.config)
        self.assertTrue(current["active"])
        self.assertTrue(current["fromCache"])
        self.assertEqual(current["image"], str(painting))
        self.assertEqual(current["info"]["title"], "The Ninth Wave")
        museum.prune(self.config)
        self.assertTrue(framed.exists())
        self.assertFalse(stale.exists())

    def test_changed_fit_reframes_without_restarting_the_interval(self):
        painting = self.cache / "commons-5.jpg"
        painting.write_bytes(b"jpg")
        self.set_link(painting)
        museum.record(painting, original=painting)  # as before `fit` existed
        set_at = museum.read_json(self.state / "state.json")["setAt"] - 100
        museum.write_json(self.state / "state.json", {**museum.read_json(self.state / "state.json"), "setAt": set_at})
        blur = museum.normalize_config({"fit": "blur"})
        with mock.patch.object(museum.subprocess, "run", side_effect=self.fake_magick), \
             mock.patch.object(museum, "set_background") as set_background:
            museum.refit(blur)
            museum.refit(blur)
        set_background.assert_called_once_with(self.cache / "display" / "commons-5.blur.jpg")
        state = museum.read_json(self.state / "state.json")
        self.assertEqual((state["fit"], state["setAt"]), ("blur", set_at))

    def test_a_theme_background_is_never_claimed(self):
        theme = Path(self.tmp.name) / "theme-forest.jpg"
        theme.write_bytes(b"jpg")
        self.set_link(theme)
        with mock.patch.object(museum, "fetch_next", side_effect=OSError("offline")), \
             mock.patch.object(museum, "set_background") as set_background:
            museum.switch(self.config)
            museum.refit(museum.normalize_config({"saveDir": self.config["saveDir"], "fit": "blur"}))
        set_background.assert_not_called()
        self.assertFalse(museum.status(self.config)["active"])


if __name__ == "__main__":
    unittest.main()
