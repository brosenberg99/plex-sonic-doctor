import contextlib
import io
import os
import shutil
import tempfile
import unittest

from plex_sonic_doctor import cli, config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sonicdoctor_home_")
        os.environ["SONICDOCTOR_HOME"] = self.home

    def tearDown(self):
        os.environ.pop("SONICDOCTOR_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_settings_round_trip_and_redaction(self):
        cfg = config.load()
        self.assertFalse(cfg.configured)
        cfg.plex_url, cfg.plex_token = "http://server:32400", "secret-token"
        cfg.acoustid_key = "secret-acoustid-key"
        cfg.path_maps = [config.PathMap("E:\\", "H:\\")]
        cfg.rules.identical_max_length_diff = 3.0
        config.save(cfg)
        again = config.load()
        self.assertEqual(again.path_maps, cfg.path_maps)
        self.assertEqual(again.rules.identical_max_length_diff, 3.0)
        self.assertNotIn("secret-token", str(config.redacted(again)))
        self.assertNotIn("secret-acoustid-key", str(config.redacted(again)))
        self.assertEqual(again.acoustid_key, "secret-acoustid-key")
        self.assertTrue(config.config_path().startswith(self.home))

    def test_cli_runs_without_setup(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["batches"])
        self.assertIn("No batches", out.getvalue())
        with self.assertRaises(SystemExit):
            cli.main(["scan"])

    def test_tags_needs_a_key_and_a_scan(self):
        with self.assertRaises(SystemExit) as e:
            cli.main(["tags"])
        self.assertIn("acoustid.org", str(e.exception))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(RuntimeError) as e:
            cli.main(["tags", "--key", "abc123"])            # saved, then: no track list yet
        self.assertIn("Run a scan first", str(e.exception))
        self.assertEqual(config.load().acoustid_key, "abc123")
        with contextlib.redirect_stdout(out):
            cli.main(["tags", "--key", ""])
        self.assertEqual(config.load().acoustid_key, "")


if __name__ == "__main__":
    unittest.main()
