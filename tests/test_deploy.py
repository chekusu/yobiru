"""Safety contracts for the versioned, manually installed gateway services."""
import configparser
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class LitestreamServiceTests(unittest.TestCase):
    def test_gateway_stop_waits_for_litestream_before_database_replacement(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read_string((REPO / "deploy/yobiru-litestream.service").read_text())
        gateway = "yobiru-gateway.service"

        # Deployment stops the gateway before backing up or replacing its database.
        # PartOf propagates that stop, but only After makes the gateway's stop job
        # wait for Litestream to release its WAL reader (shutdown reverses startup).
        self.assertIn(gateway, unit["Unit"].get("PartOf", "").split())
        self.assertIn(gateway, unit["Unit"].get("After", "").split())
        self.assertNotIn(gateway, unit["Unit"].get("Before", "").split())
        self.assertIn(gateway, unit["Install"].get("WantedBy", "").split())


if __name__ == "__main__":
    unittest.main()
