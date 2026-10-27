"""Diagnostics switch SHADPS4_BBSC_RX: the launcher passes it only when it is not "all", so a
normal build's game environment is unchanged."""
from paths import ROOT
import importlib.util
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(ROOT / "launcher"))
spec = importlib.util.spec_from_file_location("bbport_config_bbsc_rx",
                                              ROOT / "launcher/bbport_config.py")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


def settings(bbsc_rx):
    s = dict(config.DEFAULTS)
    s.update({
        "online": True,
        "online_npid": "npid",
        "online_password": "pw",
        "online_server": "play.seamlesshunt.com:31313",
        "online_webapi": "",
        "online_community": "",
        "bbsc_rx": bbsc_rx,
    })
    return s


class BbscRxEnvTests(unittest.TestCase):
    def environment(self, bbsc_rx):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHADPS4_BBSC_RX", None)
            return config.game_environment(settings(bbsc_rx))

    def test_all_leaves_the_game_environment_untouched(self):
        self.assertNotIn("SHADPS4_BBSC_RX", self.environment("all"))

    def test_missing_setting_is_plain_all(self):
        self.assertNotIn("SHADPS4_BBSC_RX", self.environment(""))

    def test_none_is_forwarded(self):
        self.assertEqual(self.environment("none")["SHADPS4_BBSC_RX"], "none")

    def test_a_type_list_is_forwarded_verbatim(self):
        self.assertEqual(self.environment("24,25,26,27")["SHADPS4_BBSC_RX"], "24,25,26,27")


if __name__ == "__main__":
    unittest.main()
