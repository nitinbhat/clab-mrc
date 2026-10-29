import unittest

import lab


class TopologyTests(unittest.TestCase):
    def test_profiles_have_independent_source_links_and_spine_paths(self):
        for profile, (planes, host_links, _) in lab.PROFILES.items():
            with self.subTest(profile=profile):
                lab.select_profile(profile)
                self.assertEqual(len(lab.NODES), 2 + planes * 4)
                self.assertEqual(len(lab.LINKS), planes * (2 * host_links + 4))
                self.assertEqual(len(lab.PATHS), planes * host_links * 2)
                interfaces = {iface for iface, _ in lab.NODES["h1"]["interfaces"]}
                self.assertEqual(interfaces, {p["host_interface"] for p in lab.PATHS})
                for iface in interfaces:
                    paths = [p for p in lab.PATHS if p["host_interface"] == iface]
                    self.assertEqual(len(paths), 2)
                    self.assertEqual(len({p["spine"] for p in paths}), 2)
                    self.assertEqual(len({p["segments"][0] for p in paths}), 1)
                self.assertEqual(
                    len({p["segments"][0] for p in lab.PATHS}), len(interfaces))
                for path in lab.PATHS:
                    self.assertEqual(path["segments"][-1], "fc00:c8::d")
                    self.assertTrue(all(
                        str(path["plane"]) in n for n in path["nodes"][1:-1]))

    def test_transfer_requires_acknowledgment_on_every_ev(self):
        paths = [{"ev": 1}, {"ev": 2}]
        result = {"delivered": 10, "count": 10,
                  "paths": {"1": {"acked": 10}, "2": {"acked": 0}}}
        with self.assertRaisesRegex(RuntimeError, "Not every configured EV"):
            lab.verify_transfer(result, paths)
        result["paths"]["2"]["acked"] = 1
        lab.verify_transfer(result, paths)


if __name__ == "__main__":
    unittest.main()
