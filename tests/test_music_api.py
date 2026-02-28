"""
Tests for the music API wrapper.
"""

import unittest

from utils.music_api import (
    DOWNLOAD_QUALITIES,
    QUALITY_TIERS,
    _edit_marker_count,
    _extract_artist,
    _extract_download_urls,
    _extract_image,
    _extract_url,
    _format_duration,
    _get_url_for_quality,
    _has_edit_markers,
    _pick_best_url,
    _safe_unescape,
    normalize_song,
    normalize_songs,
    pick_best_match,
)


class TestSafeUnescape(unittest.TestCase):
    def test_html_entities(self):
        self.assertEqual(_safe_unescape("hello &amp; world"), "hello & world")

    def test_none_returns_empty(self):
        self.assertEqual(_safe_unescape(None), "")

    def test_empty_returns_empty(self):
        self.assertEqual(_safe_unescape(""), "")

    def test_non_string_returns_str(self):
        self.assertEqual(_safe_unescape(42), "42")

    def test_plain_text_unchanged(self):
        self.assertEqual(_safe_unescape("hello world"), "hello world")

    def test_multiple_entities(self):
        self.assertEqual(
            _safe_unescape("a &lt; b &gt; c &amp; d"),
            "a < b > c & d",
        )


class TestExtractUrl(unittest.TestCase):
    def test_url_key(self):
        self.assertEqual(_extract_url({"url": "https://a.com"}), "https://a.com")

    def test_link_key(self):
        self.assertEqual(_extract_url({"link": "https://b.com"}), "https://b.com")

    def test_url_preferred_over_link(self):
        self.assertEqual(
            _extract_url({"url": "https://a.com", "link": "https://b.com"}),
            "https://a.com",
        )

    def test_empty_dict(self):
        self.assertEqual(_extract_url({}), "")


class TestExtractArtist(unittest.TestCase):
    def test_primary_artists_string(self):
        song = {"primaryArtists": "Artist A, Artist B"}
        self.assertEqual(_extract_artist(song), "Artist A, Artist B")

    def test_primary_artists_list(self):
        song = {"primaryArtists": [{"name": "X"}, {"name": "Y"}]}
        self.assertEqual(_extract_artist(song), "X, Y")

    def test_artists_primary_format_b(self):
        song = {"artists": {"primary": [{"name": "Alpha"}, {"name": "Beta"}]}}
        self.assertEqual(_extract_artist(song), "Alpha, Beta")

    def test_artist_fallback(self):
        song = {"artist": "Solo Artist"}
        self.assertEqual(_extract_artist(song), "Solo Artist")

    def test_unknown_fallback(self):
        song = {}
        self.assertEqual(_extract_artist(song), "Unknown")

    def test_html_entities_unescaped(self):
        song = {"primaryArtists": "Tom &amp; Jerry"}
        self.assertEqual(_extract_artist(song), "Tom & Jerry")

    def test_empty_primary_artists_string(self):
        song = {"primaryArtists": "", "artist": "Fallback"}
        self.assertEqual(_extract_artist(song), "Fallback")

    def test_primary_artists_list_with_empty_names(self):
        song = {"primaryArtists": [{"name": ""}, {"name": "Valid"}]}
        self.assertEqual(_extract_artist(song), "Valid")


class TestExtractImage(unittest.TestCase):
    def test_prefer_500x500(self):
        song = {
            "image": [
                {"quality": "150x150", "url": "http://small.jpg"},
                {"quality": "500x500", "url": "http://big.jpg"},
            ]
        }
        self.assertEqual(_extract_image(song), "http://big.jpg")

    def test_fallback_to_last(self):
        song = {
            "image": [
                {"quality": "150x150", "url": "http://small.jpg"},
                {"quality": "250x250", "url": "http://med.jpg"},
            ]
        }
        self.assertEqual(_extract_image(song), "http://med.jpg")

    def test_no_images(self):
        self.assertEqual(_extract_image({}), "")
        self.assertEqual(_extract_image({"image": []}), "")
        self.assertEqual(_extract_image({"image": "not a list"}), "")

    def test_link_key_variant(self):
        song = {"image": [{"quality": "500x500", "link": "http://big.jpg"}]}
        self.assertEqual(_extract_image(song), "http://big.jpg")


class TestExtractDownloadUrls(unittest.TestCase):
    def test_download_url_key(self):
        song = {
            "downloadUrl": [
                {"quality": "320kbps", "url": "http://320.mp3"},
                {"quality": "160kbps", "url": "http://160.mp3"},
            ]
        }
        result = _extract_download_urls(song)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["quality"], "320kbps")

    def test_download_url_alternate_key(self):
        song = {
            "download_url": [
                {"quality": "96kbps", "link": "http://96.mp3"},
            ]
        }
        result = _extract_download_urls(song)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "http://96.mp3")

    def test_empty(self):
        self.assertEqual(_extract_download_urls({}), [])

    def test_non_list_ignored(self):
        self.assertEqual(_extract_download_urls({"downloadUrl": "not a list"}), [])

    def test_non_dict_entries_skipped(self):
        song = {"downloadUrl": ["not a dict", {"quality": "320kbps", "url": "http://x.mp3"}]}
        result = _extract_download_urls(song)
        self.assertEqual(len(result), 1)

    def test_missing_quality_or_url_skipped(self):
        song = {
            "downloadUrl": [
                {"quality": "", "url": "http://x.mp3"},
                {"quality": "320kbps", "url": ""},
                {"quality": "160kbps", "url": "http://good.mp3"},
            ]
        }
        result = _extract_download_urls(song)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["quality"], "160kbps")


class TestPickBestUrl(unittest.TestCase):
    def setUp(self):
        self.urls = [
            {"quality": "96kbps", "url": "http://96.mp3"},
            {"quality": "160kbps", "url": "http://160.mp3"},
            {"quality": "320kbps", "url": "http://320.mp3"},
        ]

    def test_preferred_found(self):
        self.assertEqual(_pick_best_url(self.urls, "320kbps"), "http://320.mp3")

    def test_fallback_to_lower(self):
        urls = [
            {"quality": "96kbps", "url": "http://96.mp3"},
            {"quality": "160kbps", "url": "http://160.mp3"},
        ]
        self.assertEqual(_pick_best_url(urls, "320kbps"), "http://160.mp3")

    def test_empty_list(self):
        self.assertEqual(_pick_best_url([]), "")

    def test_unknown_quality_picks_highest(self):
        self.assertEqual(_pick_best_url(self.urls, "999kbps"), "http://320.mp3")

    def test_single_entry(self):
        urls = [{"quality": "48kbps", "url": "http://48.mp3"}]
        self.assertEqual(_pick_best_url(urls, "320kbps"), "http://48.mp3")


class TestGetUrlForQuality(unittest.TestCase):
    def test_exact_match(self):
        urls = [
            {"quality": "96kbps", "url": "http://96.mp3"},
            {"quality": "320kbps", "url": "http://320.mp3"},
        ]
        self.assertEqual(_get_url_for_quality(urls, "96kbps"), "http://96.mp3")

    def test_fallback_when_missing(self):
        urls = [{"quality": "160kbps", "url": "http://160.mp3"}]
        self.assertEqual(_get_url_for_quality(urls, "320kbps"), "http://160.mp3")


class TestFormatDuration(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(_format_duration(125), "2:05")

    def test_zero(self):
        self.assertEqual(_format_duration(0), "0:00")

    def test_string_input(self):
        self.assertEqual(_format_duration("300"), "5:00")

    def test_invalid_returns_zero(self):
        self.assertEqual(_format_duration(None), "0:00")
        self.assertEqual(_format_duration("abc"), "0:00")

    def test_exactly_one_minute(self):
        self.assertEqual(_format_duration(60), "1:00")


class TestNormalizeSong(unittest.TestCase):
    def test_basic_format_a(self):
        song = {
            "id": "abc123",
            "name": "Test Song &amp; More",
            "primaryArtists": "Test Artist",
            "album": "Test Album",
            "year": 2024,
            "duration": "240",
            "language": "english",
            "hasLyrics": True,
            "image": [{"quality": "500x500", "url": "http://img.jpg"}],
            "downloadUrl": [{"quality": "320kbps", "url": "http://song.mp3"}],
        }
        result = normalize_song(song)
        self.assertEqual(result["id"], "abc123")
        self.assertEqual(result["name"], "Test Song & More")
        self.assertEqual(result["artist"], "Test Artist")
        self.assertEqual(result["album"], "Test Album")
        self.assertEqual(result["year"], "2024")
        self.assertEqual(result["duration"], 240)
        self.assertEqual(result["duration_formatted"], "4:00")
        self.assertEqual(result["language"], "english")
        self.assertTrue(result["has_lyrics"])
        self.assertEqual(result["image"], "http://img.jpg")
        self.assertEqual(len(result["download_urls"]), 1)
        self.assertEqual(result["best_url"], "http://song.mp3")

    def test_format_b_artists(self):
        song = {
            "id": "xyz",
            "name": "Song B",
            "artists": {"primary": [{"name": "A1"}, {"name": "A2"}]},
            "album": {"name": "Album B"},
            "duration": 0,
        }
        result = normalize_song(song)
        self.assertEqual(result["artist"], "A1, A2")
        self.assertEqual(result["album"], "Album B")

    def test_missing_fields(self):
        result = normalize_song({})
        self.assertEqual(result["id"], "")
        self.assertEqual(result["name"], "Unknown")
        self.assertEqual(result["artist"], "Unknown")
        self.assertEqual(result["duration"], 0)
        self.assertEqual(result["best_url"], "")

    def test_none_year_does_not_produce_none_string(self):
        """year=None should become empty string, not 'None'."""
        result = normalize_song({"year": None})
        self.assertEqual(result["year"], "")

    def test_missing_year_returns_empty(self):
        """Missing year key should produce empty string."""
        result = normalize_song({})
        self.assertEqual(result["year"], "")


class TestNormalizeSongs(unittest.TestCase):
    def test_filters_non_dicts(self):
        songs = [
            {"id": "1", "name": "Song 1"},
            "not a dict",
            42,
            {"id": "2", "name": "Song 2"},
        ]
        result = normalize_songs(songs)
        self.assertEqual(len(result), 2)

    def test_empty(self):
        self.assertEqual(normalize_songs([]), [])


class TestConstants(unittest.TestCase):
    def test_quality_tiers_order(self):
        self.assertEqual(QUALITY_TIERS[0], "12kbps")
        self.assertEqual(QUALITY_TIERS[-1], "320kbps")

    def test_download_qualities_subset(self):
        for q in DOWNLOAD_QUALITIES:
            self.assertIn(q, QUALITY_TIERS)


class TestHasEditMarkers(unittest.TestCase):
    def test_slowed_reverb_detected(self):
        self.assertTrue(_has_edit_markers("Song Name (slowed + reverb)"))

    def test_nightcore_detected(self):
        self.assertTrue(_has_edit_markers("Song Name [Nightcore]"))

    def test_sped_up_detected(self):
        self.assertTrue(_has_edit_markers("Song Name (sped up)"))

    def test_bass_boosted_detected(self):
        self.assertTrue(_has_edit_markers("Song Name bass boosted"))

    def test_8d_audio_detected(self):
        self.assertTrue(_has_edit_markers("Song Name (8D Audio)"))

    def test_clean_title_not_detected(self):
        self.assertFalse(_has_edit_markers("My Beautiful Song"))

    def test_ignore_markers_in_query(self):
        """If the query itself says 'slowed', don't penalise."""
        self.assertFalse(
            _has_edit_markers("Song (slowed)", ignore_markers_in="Song slowed")
        )

    def test_partial_ignore(self):
        """Only ignore markers present in the query."""
        self.assertTrue(
            _has_edit_markers(
                "Song (slowed + reverb)", ignore_markers_in="Song slowed"
            )
        )


class TestEditMarkerCount(unittest.TestCase):
    def test_multiple_markers(self):
        self.assertEqual(
            _edit_marker_count("Song (slowed + reverb + bass boosted)"), 3
        )

    def test_no_markers(self):
        self.assertEqual(_edit_marker_count("Normal Song Title"), 0)

    def test_ignore_query_markers(self):
        self.assertEqual(
            _edit_marker_count(
                "Song (slowed + reverb)", ignore_markers_in="Song slowed"
            ),
            1,  # only reverb counted, slowed is in query
        )


class TestPickBestMatch(unittest.TestCase):
    """Test the official-song ranking logic."""

    def _song(self, name: str, artist: str = "Artist") -> dict:
        return {"name": name, "artist": artist, "id": name}

    def test_prefers_original_over_slowed(self):
        results = [
            self._song("My Song (slowed + reverb)"),
            self._song("My Song"),
        ]
        best = pick_best_match(results, "My Song")
        self.assertEqual(best["name"], "My Song")

    def test_prefers_original_over_nightcore(self):
        results = [
            self._song("Hit Track [Nightcore]"),
            self._song("Hit Track"),
            self._song("Hit Track (8D Audio)"),
        ]
        best = pick_best_match(results, "Hit Track")
        self.assertEqual(best["name"], "Hit Track")

    def test_respects_intentional_slowed_query(self):
        """When the query asks for 'slowed', prefer the slowed version."""
        results = [
            self._song("My Song (slowed + reverb)"),
            self._song("My Song"),
        ]
        best = pick_best_match(results, "My Song slowed reverb")
        # Should NOT penalise the slowed version when query says "slowed reverb"
        self.assertEqual(best["name"], "My Song (slowed + reverb)")

    def test_single_result_returned(self):
        results = [self._song("Only Song")]
        best = pick_best_match(results, "Only Song")
        self.assertEqual(best["name"], "Only Song")

    def test_empty_results_raises(self):
        with self.assertRaises(ValueError):
            pick_best_match([], "query")

    def test_edit_in_artist_name_penalised(self):
        """Edit markers in the artist field should also be penalised."""
        results = [
            self._song("Cool Song", artist="DJ Slowed & Reverb"),
            self._song("Cool Song", artist="Original Artist"),
        ]
        best = pick_best_match(results, "Cool Song")
        self.assertEqual(best["artist"], "Original Artist")

    def test_all_edits_falls_back_to_first(self):
        """When every result is an edit, still return something."""
        results = [
            self._song("Song (slowed)"),
            self._song("Song (reverb)"),
            self._song("Song (nightcore)"),
        ]
        best = pick_best_match(results, "Song")
        # Should return one of them (first one with equal-worst score)
        self.assertIn(best["name"], [r["name"] for r in results])

    def test_real_world_bbygirl_case(self):
        """Reproduces the reported bug: slowed+reverb edit picked over original."""
        results = [
            self._song(
                "✻H+3+ЯД✻7luCJIo0T6... (slowed + reverb)",
                artist="BbyGirl",
            ),
            self._song("✻H+3+ЯД✻7luCJIo0T6...", artist="BbyGirl"),
            self._song(
                "✻H+3+ЯД✻7luCJIo0T6... (sped up)",
                artist="BbyGirl",
            ),
        ]
        best = pick_best_match(results, "BbyGirl ✻H+3+ЯД✻7luCJIo0T6...")
        self.assertEqual(best["name"], "✻H+3+ЯД✻7luCJIo0T6...")

    # ── Ripoff / cover detection ─────────────────────────────────────

    def test_prefers_original_over_cover(self):
        """Cover versions should be penalised."""
        results = [
            self._song("Tum Hi Ho (Cover)", artist="Random Singer"),
            self._song("Tum Hi Ho", artist="Arijit Singh"),
        ]
        best = pick_best_match(results, "Arijit Singh Tum Hi Ho")
        self.assertEqual(best["name"], "Tum Hi Ho")
        self.assertEqual(best["artist"], "Arijit Singh")

    def test_prefers_original_over_karaoke(self):
        results = [
            self._song("Shape of You (Karaoke Version)", artist="Karaoke Hits"),
            self._song("Shape of You", artist="Ed Sheeran"),
        ]
        best = pick_best_match(results, "Shape of You Ed Sheeran")
        self.assertEqual(best["name"], "Shape of You")

    def test_prefers_original_over_tribute(self):
        results = [
            self._song("Bohemian Rhapsody (Tribute to Queen)", artist="Tribute Band"),
            self._song("Bohemian Rhapsody", artist="Queen"),
        ]
        best = pick_best_match(results, "Bohemian Rhapsody Queen")
        self.assertEqual(best["artist"], "Queen")

    def test_prefers_original_over_instrumental(self):
        results = [
            self._song("Let It Be (Instrumental)", artist="Piano Covers"),
            self._song("Let It Be", artist="The Beatles"),
        ]
        best = pick_best_match(results, "Let It Be The Beatles")
        self.assertEqual(best["artist"], "The Beatles")

    def test_prefers_original_over_remix(self):
        results = [
            self._song("Blinding Lights (Remix)", artist="DJ Whatever"),
            self._song("Blinding Lights", artist="The Weeknd"),
        ]
        best = pick_best_match(results, "Blinding Lights The Weeknd")
        self.assertEqual(best["artist"], "The Weeknd")

    def test_respects_intentional_remix_query(self):
        """When the query asks for a remix, prefer the remix."""
        results = [
            self._song("Blinding Lights (Remix)", artist="DJ Whatever"),
            self._song("Blinding Lights", artist="The Weeknd"),
        ]
        best = pick_best_match(results, "Blinding Lights Remix")
        self.assertEqual(best["name"], "Blinding Lights (Remix)")

    def test_respects_intentional_karaoke_query(self):
        results = [
            self._song("Shape of You (Karaoke)", artist="Karaoke Hits"),
            self._song("Shape of You", artist="Ed Sheeran"),
        ]
        best = pick_best_match(results, "Shape of You karaoke")
        self.assertEqual(best["name"], "Shape of You (Karaoke)")

    # ── Exact / similar name matching ────────────────────────────────

    def test_exact_name_match_wins_over_similar(self):
        """Exact name match should strongly beat partial matches."""
        results = [
            self._song("Tum Hi Ho Bandhu", artist="Neeraj Shridhar"),
            self._song("Tum Hi Ho", artist="Arijit Singh"),
            self._song("Tum Hi Ho Meri Aashiqui", artist="Some Artist"),
        ]
        best = pick_best_match(results, "Tum Hi Ho")
        self.assertEqual(best["name"], "Tum Hi Ho")

    def test_correct_song_picked_when_third_in_results(self):
        """The correct song should be picked even when it's not first."""
        results = [
            self._song("Somebody That I Used to Know (Remix)", artist="DJ Mix"),
            self._song("Somebody Else", artist="The 1975"),
            self._song("Somebody That I Used to Know", artist="Gotye"),
        ]
        best = pick_best_match(results, "Somebody That I Used to Know Gotye")
        self.assertEqual(best["name"], "Somebody That I Used to Know")
        self.assertEqual(best["artist"], "Gotye")

    def test_artist_matching_helps_disambiguation(self):
        """When song names are similar, artist match should help pick the right one."""
        results = [
            self._song("Love Story", artist="Taylor Swift"),
            self._song("Love Story", artist="Indila"),
            self._song("Love Story (Cover)", artist="Unknown Artist"),
        ]
        best = pick_best_match(results, "Love Story Taylor Swift")
        self.assertEqual(best["artist"], "Taylor Swift")

    def test_wrong_artist_not_picked(self):
        """A song by the wrong artist shouldn't be preferred."""
        results = [
            self._song("Tum Hi Ho", artist="Some Cover Artist"),
            self._song("Tum Hi Ho", artist="Arijit Singh"),
        ]
        best = pick_best_match(results, "Arijit Singh Tum Hi Ho")
        self.assertEqual(best["artist"], "Arijit Singh")

    def test_different_song_same_words_not_preferred(self):
        """A song that shares words but is a different song should not win."""
        results = [
            self._song("Tum Hi Ho Bandhu Sakha Tumhi", artist="Neeraj Shridhar"),
            self._song("Tum Hi Ho", artist="Arijit Singh"),
        ]
        best = pick_best_match(results, "Arijit Singh Tum Hi Ho")
        self.assertEqual(best["name"], "Tum Hi Ho")

    def test_mashup_penalised(self):
        results = [
            self._song("Love Mashup 2024", artist="DJ Mix"),
            self._song("Love Me Like You Do", artist="Ellie Goulding"),
        ]
        best = pick_best_match(results, "Love Me Like You Do")
        self.assertEqual(best["artist"], "Ellie Goulding")


class TestRipoffMarkerCount(unittest.TestCase):
    def test_cover_detected(self):
        from utils.music_api import _ripoff_marker_count
        self.assertGreater(_ripoff_marker_count("Song (Cover)"), 0)

    def test_karaoke_detected(self):
        from utils.music_api import _ripoff_marker_count
        self.assertGreater(_ripoff_marker_count("Song (Karaoke Version)"), 0)

    def test_tribute_detected(self):
        from utils.music_api import _ripoff_marker_count
        self.assertGreater(_ripoff_marker_count("Tribute to Artist - Song"), 0)

    def test_remix_detected(self):
        from utils.music_api import _ripoff_marker_count
        self.assertGreater(_ripoff_marker_count("Song (Remix)"), 0)

    def test_clean_title_not_detected(self):
        from utils.music_api import _ripoff_marker_count
        self.assertEqual(_ripoff_marker_count("My Beautiful Song"), 0)

    def test_ignore_markers_in_query(self):
        from utils.music_api import _ripoff_marker_count
        self.assertEqual(
            _ripoff_marker_count("Song (Remix)", ignore_markers_in="Song Remix"),
            0,
        )

    def test_instrumental_detected(self):
        from utils.music_api import _ripoff_marker_count
        self.assertGreater(_ripoff_marker_count("Song (Instrumental)"), 0)


if __name__ == "__main__":
    unittest.main()
