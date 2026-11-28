"""Keep imports a file with its release's quality and group (owner, 2026-11-28).

AKON's renamed files carry neither: Sonarr guessed HDTV-1080p, so any WEB 1080p release
was an upgrade and Sonarr downloaded the episode again.
"""
import unittest

from src.anime.release_quality import group, quality


def name_of(release, fallback=None):
    found = quality(release, fallback)
    return found and found["quality"]["name"]


class ReleaseQualityTests(unittest.TestCase):
    def test_the_source_and_resolution_come_from_the_release_name(self):
        cases = {
            "The World Is Dancing S01E01 1080p AMZN WEB-DL DUAL DDP2.0 H.264-VARYG": "WEBDL-1080p",
            "[Trix] The Laid-Off Cheat-Granting Mage S01E05 [WEBRip 1080p AV1 Opus] (Dual Audio, Multi Subs)": "WEBRip-1080p",
            "[AnoZu] The Laid-Off Cheat-Granting Mage S01E01 1080p CR WEB-DL Dual-Audio DDP 2.0 H.264": "WEBDL-1080p",
            "[Kaizoku] Show - 03 [BD 720p x265]": "Bluray-720p",
            "Show.S01E02.1080p.BluRay.x265-KAF": "Bluray-1080p",
            "Show S01E02 720p HDTV x264-GRP": "HDTV-720p",
            # No source: what fansub and encode groups encode from; ranked with WEB-DL.
            "[Judas] Tsuihou sareta Cheat Fuyo (The Laid-Off Mage) - S01E04 [1080p][HEVC x265 10bit][Dual-Audio]": "WEBRip-1080p",
        }
        for release, expected in cases.items():
            self.assertEqual(name_of(release), expected, release)

    def test_without_a_resolution_sonarrs_own_is_used_and_otherwise_nothing_is_guessed(self):
        self.assertEqual(name_of("Chii Fuyo - 03", 1080), "WEBRip-1080p")
        self.assertIsNone(quality("Chii Fuyo - 03"))                  # Sonarr's preview stays.

    def test_the_release_group(self):
        self.assertEqual(group("[Judas] Tsuihou sareta - S01E04 [1080p]"), "Judas")
        self.assertEqual(group("The World Is Dancing S01E01 1080p AMZN WEB-DL H.264-VARYG"), "VARYG")
        self.assertEqual(group("Show.S01E02.1080p.BluRay.x265-KAF.mkv"), "KAF")
        self.assertIsNone(group("Chii Fuyo - 03"))
        self.assertIsNone(group("Show S01E01 1080p Dual-Audio"))      # Not a group.
        self.assertIsNone(group(None))


if __name__ == "__main__":
    unittest.main()
