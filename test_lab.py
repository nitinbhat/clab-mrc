import ipaddress
import unittest

import lab
import mrc_paths


def walk(tables, node, destination):
    """Forward a packet through static uSID tables like the kernel; return nodes."""
    owners = {address: name for name, n in lab.NODES.items()
              for _, address in n["interfaces"]}
    visited = [node]
    for _ in range(16):
        table = tables[node]
        if destination in table["local"]:
            return visited
        prefix = str(ipaddress.IPv6Network(destination + "/48", strict=False))
        if prefix in table["end"]:
            destination = mrc_paths.usid_shift(destination)
            continue
        route = next((r for r in table["routes"] if r[0] == prefix), None)
        if route is None:
            raise AssertionError(f"{node}: no route for {destination}")
        node = owners[route[1]]
        visited.append(node)
    raise AssertionError("forwarding loop")


class AddressingTests(unittest.TestCase):
    def tearDown(self):
        lab.select_profile("multi-plane-multi-link")

    def test_profile_sizes(self):
        for profile, (planes, links, _) in lab.PROFILES.items():
            with self.subTest(profile=profile):
                lab.select_profile(profile)
                self.assertEqual(len(lab.NODES), 2 + planes * 4)
                self.assertEqual(len(lab.LINKS), planes * (2 * links + 4))
                self.assertEqual(len(lab.NODES["h1"]["port"]), planes * links)

    def test_readable_usids(self):
        self.assertEqual(mrc_paths.leaf_usid("a", 1, 1), 0xA101)
        self.assertEqual(mrc_paths.spine_usid(1, 1), 0x5110)
        self.assertEqual(mrc_paths.host_usid(2, 1, 1), 0xE211)
        ev = mrc_paths.make_ev(1, 1, 1, salt=1)
        self.assertEqual(ev, 0x10111)
        self.assertEqual(mrc_paths.ev_address(ev, 1, 2), "fcbb:0:a101:5110:b101:e211::")
        self.assertEqual(mrc_paths.ev_address(ev, 2, 1), "fcbb:0:b101:5110:a101:e111::")
        self.assertEqual(mrc_paths.usid_shift("fcbb:0:a101:5110:b101:e211::"),
                         "fcbb:0:5110:b101:e211::")

    def test_every_ev_follows_its_encoded_path_both_ways(self):
        for profile, (planes, links, _) in lab.PROFILES.items():
            with self.subTest(profile=profile):
                lab.select_profile(profile, "mrc")
                tables = lab.usid_tables()
                for row in lab.path_rows():
                    plane, spine = row["plane"], row["spine"]
                    nodes = ["h1", f"a{plane}", f"s{plane}{spine}", f"b{plane}", "h2"]
                    self.assertEqual(walk(tables, "h1", row["forward"]), nodes)
                    self.assertEqual(walk(tables, "h2", row["reverse"]), nodes[::-1])
                    # The first hop leaves on the NIC port the EV encodes.
                    first = next(r for r in tables["h1"]["routes"] if r[0] ==
                                 mrc_paths.usid_prefix(mrc_paths.active_usid(row["forward"])))
                    self.assertEqual(first[2], row["iface"])
                self.assertEqual(len({r["forward"] for r in lab.path_rows()}),
                                 planes * links * lab.SPINES)

    def test_clustermapper_probes_return_to_sender(self):
        lab.select_profile("multi-plane-multi-link", "mrc")
        tables = lab.usid_tables()
        address = mrc_paths.self_probe_address(1, 2, 2)
        self.assertEqual(walk(tables, "h1", address), ["h1", "a2", "h1"])
        address = mrc_paths.self_probe_address(1, 1, 2, spine=2)
        self.assertEqual(walk(tables, "h1", address), ["h1", "a1", "s12", "a1", "h1"])

    def test_separate_lab_per_fabric(self):
        lab.select_profile("single-plane-single-link", "mrc")
        self.assertEqual(lab.LAB_NAME, "mrc-single-plane-single-link-mrc")
        mrc_subnet = lab.SUBNET
        lab.select_profile("single-plane-single-link")
        self.assertNotEqual(lab.SUBNET, mrc_subnet)

    def test_endpoint_config_has_structure_only(self):
        lab.select_profile("multi-plane-single-link", "mrc")
        config = lab.fabric_config()
        self.assertEqual(config["hosts"]["h2"]["ports"], {"eth1": [1, 1], "eth2": [2, 1]})
        self.assertNotIn("paths", config)

    def test_baseline_leaves_originate_nic_prefixes(self):
        lab.select_profile("multi-plane-multi-link")
        config = "\n".join(lab.bgp_config(lab.NODES["b2"]))
        self.assertEqual(config.count("ipv6 route fc00:200::/48"), 2)
        self.assertIn("maximum-paths 8", config)


class AnalyzeTests(unittest.TestCase):
    def result(self, delivered):
        return {"algo": "mrc", "fabric": "mrc", "sent_new": sum(delivered),
                "delivered": sum(delivered), "undelivered": 0, "losses": 0,
                "retransmissions": 0, "duration": len(delivered) * 0.01,
                "t0": 100.0, "bin_seconds": 0.01, "finished": 100.0 + len(delivered) * 0.01,
                "events": [{"t": 101.004, "event": "ev_state", "ev": "10111",
                            "new": "ASSUMED_BAD"}],
                "timeline": [{"t": round(i * 0.01, 2), "delivered": n}
                             for i, n in enumerate(delivered)]}

    def test_outage_and_recovery(self):
        # 20 pkts/bin, a 300 ms outage starting at t=1 s, then full rate again.
        delivered = [20] * 100 + [0] * 30 + [20] * 100
        summary = lab.analyze(self.result(delivered), t_fault=101.0, t_restore=102.0)
        self.assertEqual(summary["longest_zero_delivery_ms"], 300)
        self.assertEqual(summary["recovery_ms"], 300)
        self.assertEqual(summary["first_ev_retired_ms"], 4)
        self.assertTrue(summary["recovered_before_repair"])

    def test_no_recovery_before_repair(self):
        delivered = [20] * 100 + [0] * 100 + [20] * 50
        summary = lab.analyze(self.result(delivered), t_fault=101.0, t_restore=102.0)
        self.assertFalse(summary["recovered_before_repair"])


class PreflightTests(unittest.TestCase):
    ROUTES = ("ROUTES_BEGIN\n"
              "unicast fc00:1::1  encap seg6local action End dev sid0 proto boot\n"
              "unicast fc00:2::/64  encap seg6 mode encap segs 1 [ fc00:1::1 ] dev sid0\n"
              "unicast fcbb:0:1::/48  encap seg6local action End {flavor}dev sid0\n"
              "ROUTES_END\n")

    def test_new_kernel_passes_every_check(self):
        output = self.ROUTES.format(
            flavor="flavors next-csid lblen 32 nflen 16 ") + "USID_FORWARD_OK\n"
        self.assertTrue(all(lab.parse_preflight(output).values()))

    def test_old_kernel_silently_drops_flavor(self):
        checks = lab.parse_preflight(self.ROUTES.format(flavor=""))
        self.assertTrue(checks["seg6local_end"] and checks["seg6_encap"])
        self.assertFalse(checks["next_csid_retained"] or checks["usid_forwarding"])

    def test_kernel_version_parsing(self):
        self.assertLess(lab.kernel_version("5.10.0-22-amd64"), (6, 1))
        self.assertGreaterEqual(lab.kernel_version("6.1.0-0.deb11.21-amd64"), (6, 1))


if __name__ == "__main__":
    unittest.main()
