#!/usr/bin/env python3
"""Generate, configure, and validate four isolated FRR/SRv6 study topologies."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import platform
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT
LAB_NAME = "mrc-poc"
PROFILES = {
    "single-plane-single-link": (1, 1, 250),
    "single-plane-multi-link": (1, 2, 251),
    "multi-plane-single-link": (2, 1, 252),
    "multi-plane-multi-link": (2, 2, 253),
}
PLANES = 2
HOST_LINKS = 1
SUBNET = 253
IMAGE = "mrc-poc-frr:10.5.0"
NODES = {}
LINKS = []
PATHS = []


def select_profile(profile):
    global ROOT, LAB_NAME, PLANES, HOST_LINKS, SUBNET
    PLANES, HOST_LINKS, SUBNET = PROFILES[profile]
    LAB_NAME = "mrc-" + profile
    ROOT = PROJECT / "topologies" / profile
    NODES.clear()
    LINKS.clear()
    PATHS.clear()
    addressing()


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def execute(node, *args, capture=False):
    result = run("docker", "exec", f"clab-{LAB_NAME}-{node}", *args,
                 stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def addressing():
    for host, number in (("h1", 100), ("h2", 200)):
        NODES[host] = {"id": number, "interfaces": [], "peers": []}
    for plane in range(1, PLANES + 1):
        for name, number in ((f"a{plane}", plane * 10 + 1),
                             (f"b{plane}", plane * 10 + 2),
                             (f"s{plane}1", plane * 10 + 3),
                             (f"s{plane}2", plane * 10 + 4)):
            NODES[name] = {"id": number, "interfaces": [], "peers": []}
        pairs = [pair for _ in range(HOST_LINKS)
                 for pair in (("h1", f"a{plane}"), ("h2", f"b{plane}"))]
        pairs += [(f"a{plane}", f"s{plane}1"), (f"a{plane}", f"s{plane}2"),
                 (f"b{plane}", f"s{plane}1"), (f"b{plane}", f"s{plane}2")]
        for left, right in pairs:
            link_id = len(LINKS) + 1
            ends = []
            for node, peer, suffix in ((left, right, 1), (right, left, 2)):
                iface = f"eth{len(NODES[node]['interfaces']) + 1}"
                address = f"fd00:10:{link_id:x}::{suffix}"
                peer_address = f"fd00:10:{link_id:x}::{3-suffix}"
                NODES[node]["interfaces"].append((iface, address))
                NODES[node]["peers"].append((peer, peer_address, iface))
                ends.append(f"{node}:{iface}")
            LINKS.append({"endpoints": ends})
        for host_link in range(HOST_LINKS):
            for spine in (1, 2):
                ev = len(PATHS) + 1
                routers = [f"a{plane}", f"s{plane}{spine}", f"b{plane}"]
                first_sid = f"fc00:{NODES[routers[0]]['id']:x}::{0x10 + host_link:x}"
                PATHS.append({
                    "ev": ev, "plane": plane, "host_link": host_link + 1,
                    "host_interface": f"eth{(plane - 1) * HOST_LINKS + host_link + 1}",
                    "spine": f"s{plane}{spine}",
                    "nodes": ["h1", *routers, "h2"],
                    "destination": f"fc00:200:{ev}::2",
                    "segments": [first_sid]
                    + [f"fc00:{NODES[n]['id']:x}::1" for n in routers[1:]]
                    + ["fc00:c8::d"],
                })


def generate():
    config_dir = ROOT / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    topology = {
        "name": LAB_NAME,
        "mgmt": {"network": f"{LAB_NAME}-mgmt", "ipv4-subnet": f"172.31.{SUBNET}.0/24"},
        "topology": {"defaults": {
            "kind": "linux", "image": IMAGE,
            "sysctls": {"net.ipv6.conf.all.disable_ipv6": "0",
                        "net.ipv6.conf.default.disable_ipv6": "0",
                        "net.ipv6.conf.all.forwarding": "1",
                        "net.ipv6.conf.all.seg6_enabled": "1",
                        "net.ipv6.conf.default.seg6_enabled": "1"}},
            "nodes": {}, "links": LINKS},
    }
    daemons = (
        "bgpd=yes\nospfd=no\nospf6d=no\nisisd=no\n"
        "vtysh_enable=yes\nzebra_options=\"-A 127.0.0.1\"\n"
        "mgmtd_options=\"-A 127.0.0.1\"\nbgpd_options=\"-A 127.0.0.1\"\n"
        "staticd_options=\"-A 127.0.0.1\"\n"
    )
    (config_dir / "daemons").write_text(daemons)
    (config_dir / "vtysh.conf").write_text("service integrated-vtysh-config\n")
    for name, node in NODES.items():
        entry = {"binds": ["../../transport.py:/opt/transport.py",
                           "paths.json:/opt/paths.json",
                           "results:/results"]}
        if name.startswith("h"):
            entry["cmd"] = "sleep infinity"
        else:
            lines = ["frr version 10.5", "frr defaults datacenter",
                     f"hostname {name}", "log stdout", "service integrated-vtysh-config",
                     "interface lo", f" ipv6 address fc00:{node['id']:x}::2/64"]
            for iface, address in node["interfaces"]:
                lines += [f"interface {iface}", f" ipv6 address {address}/64"]
            lines += [f"router bgp {65000 + node['id']}",
                      f" bgp router-id 10.255.0.{node['id']}",
                      " bgp bestpath as-path multipath-relax",
                      " no bgp ebgp-requires-policy",
                      " no bgp default ipv4-unicast"]
            for peer, address, _ in node["peers"]:
                if not peer.startswith("h"):
                    lines += [f" neighbor {address} remote-as {65000 + NODES[peer]['id']}",
                              f" neighbor {address} timers 1 3"]
            lines += [" address-family ipv6 unicast", "  redistribute connected",
                      "  redistribute static", "  maximum-paths 8"]
            for peer, address, _ in node["peers"]:
                if not peer.startswith("h"):
                    lines += [f"  neighbor {address} activate"]
            lines += [" exit-address-family"]
            # Each plane advertises the shared endpoint loopbacks independently.
            for peer, address, _ in node["peers"]:
                if peer.startswith("h"):
                    prefix = "fc00:100::/48" if peer == "h1" else "fc00:200::/48"
                    lines += [f"ipv6 route {prefix} {address}"]
                    if peer == "h2":
                        lines += [f"ipv6 route fc00:c8::d/128 {address}"]
            (config_dir / f"{name}.conf").write_text("\n".join(lines) + "\n")
            entry["binds"] += [f"configs/{name}.conf:/etc/frr/frr.conf",
                               "configs/daemons:/etc/frr/daemons",
                               "configs/vtysh.conf:/etc/frr/vtysh.conf"]
        topology["topology"]["nodes"][name] = entry
    (ROOT / "mrc.clab.yml").write_text(json.dumps(topology, indent=2) + "\n")
    (ROOT / "paths.json").write_text(json.dumps(PATHS, indent=2) + "\n")
    (ROOT / "results").mkdir(exist_ok=True)
    profile = ROOT.name
    diagram = ["```mermaid", "flowchart LR"]
    for link in LINKS:
        left, right = link["endpoints"]
        node_a, port_a = left.split(":")
        node_b, port_b = right.split(":")
        diagram.append(f'  {node_a} ---|"{port_a} - {port_b}"| {node_b}')
    diagram.append("```")
    diagram = "\n".join(diagram)
    (ROOT / "README.md").write_text(
        f"# {profile}\n\n"
        f"{PLANES} plane(s), {HOST_LINKS} physical host link(s) per plane, "
        f"{len(NODES)} containers, {len(LINKS)} links, {len(PATHS)} source EVs.\n\n"
        "Each plane has source leaf `aN`, destination leaf `bN`, "
        "and two spines `sN1`/`sN2`. Every leaf connects to both spines. "
        "The two hosts are shared across planes; the routers are not.\n\n"
        f"{diagram}\n\n"
        "From the project root:\n\n```sh\n"
        f"python3 lab.py deploy --profile {profile}\n"
        f"python3 lab.py test --profile {profile}\n"
        f"python3 lab.py destroy --profile {profile}\n```\n\n"
        "`mrc.clab.yml` is JSON (valid YAML) with relative bind mounts. "
        "Deployment through `lab.py` also installs the Linux SRv6 datapath; "
        "a bare `containerlab deploy` must be followed by "
        f"`python3 lab.py configure --profile {profile}`.\n\n"
        "See the root README for protocol limitations and how these study "
        "variants differ from the article's drawings. Results and captures "
        "are written under `results/` and are excluded from version control.\n")


def probe_usid():
    command = ["docker", "exec", f"clab-{LAB_NAME}-h1", "ip", "-6", "route",
               "replace", "fcbb:0:ffff::/48", "encap", "seg6local",
               "action", "End", "flavors", "next-csid", "lblen", "32",
               "nflen", "16", "dev", "eth1"]
    result = subprocess.run(command, text=True, capture_output=True)
    route = ""
    if result.returncode == 0:
        try:
            route = execute("h1", "ip", "-6", "-details", "route", "show",
                            "fcbb:0:ffff::/48", capture=True)
        finally:
            execute("h1", "ip", "-6", "route", "del", "fcbb:0:ffff::/48")
    capabilities = {
        "usid_attributes_preserved": result.returncode == 0 and "next-csid" in route,
        "probe_exit_code": result.returncode,
        "probe_error": result.stderr, "probe_route": route,
        "note": "Attribute preservation is necessary, not a uSID forwarding test.",
    }
    (ROOT / "results/capabilities.json").write_text(
        json.dumps(capabilities, indent=2) + "\n")
    return capabilities


def configure():
    for name, node in NODES.items():
        for iface, address in node["interfaces"]:
            execute(name, "ip", "link", "set", iface, "up")
            if name.startswith("h"):
                execute(name, "ip", "-6", "addr", "replace",
                        address + "/64", "dev", iface, "nodad")
        execute(name, "sysctl", "-qw", "net.ipv6.conf.all.seg6_enabled=1",
                "net.ipv6.conf.default.seg6_enabled=1")
        if not name.startswith("h"):
            execute(name, "ip", "-6", "route", "replace",
                    f"fc00:{node['id']:x}::1/128", "encap", "seg6local",
                    "action", "End", "dev", "eth1")
            route = execute(name, "ip", "-6", "-details", "route", "show",
                            f"fc00:{node['id']:x}::1/128", capture=True)
            if "encap seg6local action End" not in route:
                raise RuntimeError(f"{name}: kernel did not install SRv6 End action")
    execute("h1", "ip", "-6", "addr", "replace", "fc00:100::1/128",
            "dev", "lo", "nodad")
    execute("h1", "ip", "sr", "tunsrc", "set", "fc00:100::1")
    for path in PATHS:
        execute("h2", "ip", "-6", "addr", "replace",
                path["destination"] + "/128", "dev", "lo", "nodad")
    execute("h2", "ip", "-6", "route", "replace", "fc00:c8::d/128",
            "encap", "seg6local", "action", "End.DT6", "table", "255",
            "dev", "eth1")
    route = execute("h2", "ip", "-6", "-details", "route", "show",
                    "fc00:c8::d/128", capture=True)
    if "End.DT6 table local" not in route and "End.DT6 table 255" not in route:
        raise RuntimeError("h2: kernel did not install local-table SRv6 decapsulation")
    for host, prefix in (("h1", "fc00::/16"), ("h2", "fc00:100::/48")):
        args = ["ip", "-6", "route", "replace", prefix, "metric", "100"]
        for _, address, iface in NODES[host]["peers"]:
            args += ["nexthop", "via", address, "dev", iface, "weight", "1"]
        execute(host, *args)
    for path in PATHS:
        _, address, iface = next(
            peer for peer in NODES["h1"]["peers"] if peer[2] == path["host_interface"])
        first_sid = path["segments"][0]
        # H.Encaps resolves the first SID separately from the inner /128 route.
        execute("h1", "ip", "-6", "route", "replace", first_sid + "/128",
                "via", address, "dev", iface)
        execute(f"a{path['plane']}", "ip", "-6", "route", "replace",
                first_sid + "/128", "encap", "seg6local",
                "action", "End", "dev", "eth1")
        execute("h1", "ip", "-6", "route", "replace",
                path["destination"] + "/128", "encap", "seg6", "mode", "encap",
                "segs", ",".join(path["segments"]), "via", address, "dev", iface)
    capabilities = probe_usid()
    print("uSID attributes preserved:", capabilities["usid_attributes_preserved"])
    deadline = time.monotonic() + 60
    while True:
        checks = [
            subprocess.run(["docker", "exec", f"clab-{LAB_NAME}-h1", "ping",
                            "-6", "-I", "fc00:100::1", "-c", "1", "-W", "1",
                            p["destination"]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            for p in PATHS
        ]
        if not any(checks):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("SRv6 paths did not converge; inspect FRR and kernel routes")
        time.sleep(1)
    print(f"All {len(PATHS)} explicit SRv6 paths pass end-to-end ping.")


def verify_transfer(result, paths):
    if result["delivered"] != result["count"]:
        raise RuntimeError("Sender did not complete the entire transfer")
    if any(result["paths"][str(p["ev"])]["acked"] == 0 for p in paths):
        raise RuntimeError("Not every configured EV received acknowledgments")


def control_plane():
    evidence = {"neighbors": {}, "ecmp_routes": {}}
    for node in NODES:
        if node.startswith("h"):
            continue
        summary = json.loads(execute(
            node, "vtysh", "-c", "show bgp ipv6 unicast summary json", capture=True))
        peers = summary["peers"]
        if len(peers) != 2 or any(p["state"] != "Established" for p in peers.values()):
            raise RuntimeError(f"{node}: not all expected BGP sessions are established")
        evidence["neighbors"][node] = summary
    for plane in range(1, PLANES + 1):
        prefix = f"fc00:{NODES[f'b{plane}']['id']:x}::/64"
        routes = json.loads(execute(
            f"a{plane}", "vtysh", "-c", f"show ipv6 route {prefix} json", capture=True))
        route = next(r for r in routes[prefix] if r["protocol"] == "bgp")
        installed = [n for n in route["nexthops"] if n.get("fib") and n["active"]]
        if len(installed) != 2:
            raise RuntimeError(f"Plane {plane}: expected two installed ECMP nexthops")
        evidence["ecmp_routes"][f"a{plane}"] = routes
    return evidence


def test():
    with (ROOT / "results/.test.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        test_locked()


def test_locked():
    (ROOT / "results/summary.json").unlink(missing_ok=True)
    evidence = control_plane()
    (ROOT / "results/control-plane.json").write_text(json.dumps(evidence, indent=2) + "\n")
    execute("h2", "python3", "/opt/transport.py", "receive", "--reset",
            capture=True)
    captures = {}
    receiver_process = subprocess.Popen([
        "docker", "exec", f"clab-{LAB_NAME}-h2",
        "python3", "/opt/transport.py", "receive"])
    try:
        deadline = time.monotonic() + 10
        while not (ROOT / "results/receiver-ready").exists():
            if receiver_process.poll() is not None:
                raise RuntimeError("Receiver exited before becoming ready")
            if time.monotonic() >= deadline:
                raise RuntimeError("Receiver did not become ready")
            time.sleep(0.1)
        targets = {n: (n, "eth1") for n in NODES if n.startswith("s")}
        targets.update({f"h1-{iface}": ("h1", iface)
                        for iface, _ in NODES["h1"]["interfaces"]})
        for label, (node, iface) in targets.items():
            captures[label] = subprocess.Popen([
                "docker", "exec", f"clab-{LAB_NAME}-{node}", "timeout", "15",
                "tcpdump", "-p", "-i", iface, "-U", "-c", "20", "-w",
                f"/results/{label}.pcap", "ip6 and ip6[6] == 43"])
        time.sleep(1)
        execute("h1", "python3", "/opt/transport.py", "send",
                "--output", "/results/baseline.json")
        baseline = json.loads((ROOT / "results/baseline.json").read_text())
        verify_transfer(baseline, PATHS)
        if baseline["retransmissions"]:
            raise RuntimeError("Baseline unexpectedly required retries")
        deltas = {}
        for label, process in captures.items():
            if process.wait(timeout=10):
                raise RuntimeError(f"SRH packet capture failed at {label}")
            text = execute(targets[label][0], "tcpdump", "-nn", "-r",
                           f"/results/{label}.pcap", capture=True)
            (ROOT / f"results/{label}-capture.txt").write_text(text)
            deltas[label] = len(text.splitlines())
            if deltas[label] != 20 or "RT6" not in text:
                raise RuntimeError(f"Missing real SRH packet evidence at {label}")
        (ROOT / "results/forwarding.json").write_text(json.dumps(deltas, indent=2) + "\n")
        # Fail one exact path after traffic has started; always restore the link.
        sender = subprocess.Popen([
            "docker", "exec", f"clab-{LAB_NAME}-h1", "python3", "/opt/transport.py",
            "send", "--count", "1000", "--interval", "0.003",
            "--output", "/results/failure.json"])
        try:
            time.sleep(1)
            execute("s11", "ip", "link", "set", "eth2", "down")
            if sender.wait(timeout=60):
                raise RuntimeError("Link-failure transfer failed")
        finally:
            execute("s11", "ip", "link", "set", "eth2", "up")
            if sender.poll() is None:
                sender.terminate()
                sender.wait(timeout=5)
        failure = json.loads((ROOT / "results/failure.json").read_text())
        verify_transfer(failure, PATHS)
        if not failure["paths"]["1"]["timeouts"] or not failure["retransmissions"]:
            raise RuntimeError("Failure did not exercise timeout and selective retry")
        # A new connection must be able to use the restored path again.
        time.sleep(5)
        execute("h1", "python3", "/opt/transport.py", "send",
                "--output", "/results/restored.json")
        restored = json.loads((ROOT / "results/restored.json").read_text())
        verify_transfer(restored, PATHS)
        if restored["retransmissions"]:
            raise RuntimeError("Restored paths unexpectedly required retries")
    finally:
        execute("h2", "python3", "/opt/transport.py", "stop")
        receiver_process.wait(timeout=5)
        # Container-side timeout bounds capture lifetime even if a test aborts.
        for process in captures.values():
            process.wait(timeout=20)
    receiver = json.loads((ROOT / "results/receiver.json").read_text())
    for result in (baseline, failure, restored):
        state = receiver[result["session"]]
        if state["received"] != result["count"] or state["next"] != result["count"]:
            raise RuntimeError("Receiver did not reassemble the complete sequence")
    report = {
        "profile": ROOT.name, "status": "PASS",
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "kernel": platform.release(),
        "bgp_sessions_established": sum(
            len(s["peers"]) for s in evidence["neighbors"].values()) // 2,
        "ecmp_nexthops_per_plane": 2,
        "baseline_delivered": baseline["delivered"],
        "forwarding_capture_packets": deltas,
        "failure_delivered": failure["delivered"],
        "failure_retransmissions": failure["retransmissions"],
        "failed_evs": {
            ev: {k: v for k, v in state.items() if k != "rtt_ms"}
            for ev, state in failure["paths"].items() if state["timeouts"]},
        "receiver_out_of_order": receiver[failure["session"]]["out_of_order"],
        "restored_delivered": restored["delivered"],
        "restored_evs_acked": {ev: s["acked"] for ev, s in restored["paths"].items()},
    }
    (ROOT / "results/summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["generate", "deploy", "configure",
                                          "test", "destroy", "usid-check"])
    parser.add_argument("--profile", choices=PROFILES,
                        default="multi-plane-multi-link")
    args = parser.parse_args()
    select_profile(args.profile)
    if args.action == "generate":
        generate()
    elif args.action == "deploy":
        generate()
        run("docker", "build", "-t", IMAGE, str(PROJECT))
        run("containerlab", "deploy", "-t", str(ROOT / "mrc.clab.yml"))
        configure()
    elif args.action == "configure":
        configure()
    elif args.action == "test":
        test()
    elif args.action == "destroy":
        run("containerlab", "destroy", "-t", str(ROOT / "mrc.clab.yml"))
    else:
        capabilities = probe_usid()
        if not capabilities["usid_attributes_preserved"]:
            raise SystemExit("Native uSID unavailable: kernel discarded next-csid attributes.")
        raise SystemExit("uSID attributes exist, but this PoC has no validated uSID datapath yet.")


if __name__ == "__main__":
    main()
