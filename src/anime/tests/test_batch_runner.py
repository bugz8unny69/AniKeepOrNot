"""Offline tests of the local NextGen batch runner itself."""
import unittest

from src.anime.tests.run_batches import batch_modules, find_test_modules, main


class BatchRunnerTests(unittest.TestCase):
    def test_discovers_only_nextgen_tests(self):
        modules = find_test_modules()
        self.assertIn("src.anime.tests.test_anime_series_schema", modules)
        self.assertTrue(all(module.startswith("src.anime.tests.") for module in modules))
        self.assertEqual(modules, sorted(modules))

    def test_filters_by_filename(self):
        self.assertIn("src.anime.tests.test_anime_series_schema",
                      find_test_modules("schema"))
        self.assertNotIn("src.anime.tests.test_anime_series_schema",
                         find_test_modules("batch_runner"))

    def test_batches_keep_all_files_without_skipping(self):
        modules = ["test_a", "test_b", "test_c", "test_d", "test_e"]
        groups = list(batch_modules(modules, 2))
        self.assertEqual(groups, [
            ["test_a", "test_b"], ["test_c", "test_d"], ["test_e"]
        ])
        self.assertEqual([m for batch in groups for m in batch], modules)

    def test_unknown_filter_fails_instead_of_false_pass(self):
        self.assertEqual(main(["--match", "definitely_no_nextgen_tests_exist"]), 2)


if __name__ == "__main__":
    unittest.main()
