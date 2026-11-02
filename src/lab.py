#!/usr/bin/env python3
"""Generate, configure, and run MRC learning-lab topologies with containerlab.

Two fabric modes share the same topologies:

- baseline: conventional design. FRR eBGP + ECMP (L4 hash), plain IPv6/UDP.
  The EV is the UDP source port; switches hash it onto a path.
- mrc: the OpenAI/Microsoft MRC design at lab scale. No routing protocol;
  each switch has one uN uSID (End + NEXT-C-SID) and static routes to its
  neighbours, installed once. Endpoints source-route every packet with
  IPv6-in-IPv6 (outer destination = uSID path, inner = NIC address).
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

# Add src/ to path for sibling imports when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mrc_paths

ROOT = Path(__file__).resolve().parent.parent  # project root, not src/
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
SPINES = 2
SUBNET = 253
IMAGE = "mrc-poc-frr:10.5.0"
FABRICS = ("baseline", "mrc")
FABRIC = "baseline"
USID_FLAVOR = ["flavors", "next-csid", "lblen", "32", "nflen", "16"]
USID_DEVICE = "usid0"
NIC = {"h1": "fc00:100::1", "h2": "fc00:200::1"}
HOST_INDEX = {"h1": 1, "h2": 2}
NODES = {}
LINKS = []


def select_profile(profile, fabric="baseline"):
    global ROOT, LAB_NAME, PLANES, HOST_LINKS, SUBNET, FABRIC
    PLANES, HOST_LINKS, SUBNET = PROFILES[profile]
    FABRIC = fabric
    directory = profile if fabric == "baseline" else f"{profile}-{fabric}"
    if fabric != "baseline":
        SUBNET -= 10
    LAB_NAME = "mrc-" + directory
    ROOT = PROJECT / "topologies" / directory
    NODES.clear()
    LINKS.clear()
    addressing()


def profile_name():
    return next(n for n, v in PROFILES.items() if v[:2] == (PLANES, HOST_LINKS))


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def container(node):
    return f"clab-{LAB_NAME}-{node}"


def execute(node, *args, capture=False, quiet=False, check=True):
    result = subprocess.run(["docker", "exec", container(node), *args], text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.DEVNULL if quiet else None)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.stdout if capture else result.returncode


def addressing():
    for host in ("h1", "h2"):
        NODES[host] = {"id": 100 * HOST_INDEX[host], "interfaces": [], "peers": [],
                       "port": {}, "usids": []}
    for plane in range(1, PLANES + 1):
        for name, number in ((f"a{plane}", plane * 10 + 1), (f"b{plane}", plane * 10 + 2),
                             (f"s{plane}1", plane * 10 + 3), (f"s{plane}2", plane * 10 + 4)):
            NODES[name] = {"id": number, "interfaces": [], "peers": [], "port": {},
                           "usids": [], "plane": plane}
        NODES[f"s{plane}1"]["usids"] = [mrc_paths.spine_usid(plane, 1)]
        NODES[f"s{plane}2"]["usids"] = [mrc_paths.spine_usid(plane, 2)]
        pairs = [(host, f"{leaf}{plane}", link) for link in range(1, HOST_LINKS + 1)
                 for host, leaf in (("h1", "a"), ("h2", "b"))]
        pairs += [(f"{leaf}{plane}", f"s{plane}{spine}", 0)
                  for leaf in "ab" for spine in (1, 2)]
        for left, right, link in pairs:
            link_id = len(LINKS) + 1
            ends = []
            for node, peer, suffix in ((left, right, 1), (right, left, 2)):
                iface = f"eth{len(NODES[node]['interfaces']) + 1}"
                NODES[node]["interfaces"].append((iface, f"fd00:10:{link_id:x}::{suffix}"))
                NODES[node]["peers"].append((peer, f"fd00:10:{link_id:x}::{3 - suffix}", iface))
                ends.append(f"{node}:{iface}")
                if link:
                    # (plane, link) of the NIC port this host link belongs to.
                    NODES[node]["port"][iface] = (plane, link)
            LINKS.append({"endpoints": ends})
            if link:
                host, leaf = left, right
                NODES[host]["usids"].append(mrc_paths.host_usid(HOST_INDEX[host], plane, link))
                NODES[leaf]["usids"].append(mrc_paths.leaf_usid(leaf[0], plane, link))


def iface_to(node, peer, nth=0):
    return [i for p, _, i in NODES[node]["peers"] if p == peer][nth]


def plane_ports(leaf, host):
    return [i for p, _, i in NODES[leaf]["peers"] if p == host]


def nic_link_iface(leaf, host, link):
    plane = NODES[leaf]["plane"]
    return next(i for p, _, i in NODES[leaf]["peers"]
                if p == host and NODES[leaf]["port"][i] == (plane, link))


def peer_address(node, iface):
    return next(a for _, a, i in NODES[node]["peers"] if i == iface)


def host_port_iface(host, plane, link):
    return next(i for i, port in NODES[host]["port"].items() if port == (plane, link))


def fabric_config():
    """Everything the endpoint needs: no per-path state, only structure."""
    return {
        "fabric": FABRIC, "planes": PLANES, "host_links": HOST_LINKS, "spines": SPINES,
        "hosts": {h: {"index": HOST_INDEX[h], "nic": NIC[h],
                      "ports": {i: list(p) for i, p in NODES[h]["port"].items()}}
                  for h in ("h1", "h2")},
    }


def usid_tables():
    """Static per-node uSID state: owned uN uSIDs, local addresses, routes."""
    tables = {}
    for name, node in NODES.items():
        table = {"end": [], "local": [], "routes": []}
        if name.startswith("h"):
            table["local"] = [mrc_paths.usid_address([u]) for u in node["usids"]]
        else:
            table["end"] = [mrc_paths.usid_prefix(u) for u in node["usids"]]
        for peer, address, iface in node["peers"]:
            if peer.startswith("h"):
                plane, link = node["port"][iface]
                reachable = [mrc_paths.host_usid(HOST_INDEX[peer], plane, link)]
            elif name.startswith("h"):
                plane, link = node["port"][iface]
                reachable = [mrc_paths.leaf_usid(peer[0], plane, link)]
            else:
                reachable = NODES[peer]["usids"]
            table["routes"] += [(mrc_paths.usid_prefix(u), address, iface) for u in reachable]
        tables[name] = table
    return tables


def bgp_config(node):
    lines = [f"router bgp {65000 + node['id']}",
             f" bgp router-id 10.255.0.{node['id']}",
             " bgp bestpath as-path multipath-relax",
             " no bgp ebgp-requires-policy",
             " no bgp default ipv4-unicast"]
    routers = [(p, a) for p, a, _ in node["peers"] if not p.startswith("h")]
    for peer, address in routers:
        lines += [f" neighbor {address} remote-as {65000 + NODES[peer]['id']}",
                  f" neighbor {address} timers 1 3"]
    lines += [" address-family ipv6 unicast", "  redistribute connected",
              "  redistribute static", "  maximum-paths 8"]
    lines += [f"  neighbor {address} activate" for _, address in routers]
    lines += [" exit-address-family"]
    # Leaves originate the attached host's NIC prefix; each plane does so independently.
    for peer, address, _ in node["peers"]:
        if peer.startswith("h"):
            lines += [f"ipv6 route {NIC[peer].rsplit(':', 2)[0]}::/48 {address}"]
    return lines


def generate():
    config_dir = ROOT / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    sysctls = {"net.ipv6.conf.all.disable_ipv6": "0",
               "net.ipv6.conf.default.disable_ipv6": "0",
               "net.ipv6.conf.all.forwarding": "1",
               # ECMP hashes the 5-tuple, so the UDP source port (EV) picks the path.
               "net.ipv6.fib_multipath_hash_policy": "1",
               "net.ipv6.conf.all.seg6_enabled": "1",
               "net.ipv6.conf.default.seg6_enabled": "1",
               # Linux holds a re-enabled interface's addresses "tentative" for
               # ~1.5 s (DAD) and cannot resolve next hops meanwhile. Switch
               # ASICs do not, so DAD would unfairly delay link recovery.
               "net.ipv6.conf.all.accept_dad": "0",
               "net.ipv6.conf.default.accept_dad": "0",
               # A host must not keep hashing flows onto a NIC port without carrier.
               "net.ipv6.conf.all.ignore_routes_with_linkdown": "1",
               "net.ipv6.conf.default.ignore_routes_with_linkdown": "1"}
    topology = {
        "name": LAB_NAME,
        "mgmt": {"network": f"{LAB_NAME}-mgmt", "ipv4-subnet": f"172.31.{SUBNET}.0/24"},
        "topology": {"defaults": {"kind": "linux", "image": IMAGE, "sysctls": sysctls},
                     "nodes": {}, "links": LINKS},
    }
    (config_dir / "daemons").write_text(
        f"bgpd={'yes' if FABRIC == 'baseline' else 'no'}\n"
        f"bfdd={'yes' if FABRIC == 'baseline' else 'no'}\nospfd=no\nospf6d=no\nisisd=no\n"
        "vtysh_enable=yes\nzebra_options=\"-A 127.0.0.1\"\n"
        "mgmtd_options=\"-A 127.0.0.1\"\nbgpd_options=\"-A 127.0.0.1\"\n"
        "staticd_options=\"-A 127.0.0.1\"\n")
    (config_dir / "vtysh.conf").write_text("service integrated-vtysh-config\n")
    tables = usid_tables()
    for name, node in NODES.items():
        # Mount the directory, not single files: editors replace files (new
        # inode), which a single-file bind mount would never see.
        entry = {"binds": ["../..:/opt/mrc:ro", "fabric.json:/opt/fabric.json",
                           "results:/results"]}
        if name[0] in "ab":
            # Linux < 6.11 shares one multipath hash seed across namespaces, so
            # identical hashes on consecutive tiers polarize ECMP (a1 would map
            # every flow from a given h1 port to the same spine). Different
            # hash field sets per tier stand in for per-switch hash seeds.
            entry["sysctls"] = {**sysctls, "net.ipv6.fib_multipath_hash_policy": "3",
                                "net.ipv6.fib_multipath_hash_fields":
                                    "0x0013" if name[0] == "a" else "0x0033"}
        if name.startswith("h"):
            entry["cmd"] = "sleep infinity"
        else:
            lines = ["frr version 10.5", "frr defaults datacenter", f"hostname {name}",
                     "log stdout", "service integrated-vtysh-config"]
            if FABRIC == "baseline":
                lines += ["interface lo", f" ipv6 address fc00:{node['id']:x}::2/64"]
            for iface, address in node["interfaces"]:
                lines += [f"interface {iface}", f" ipv6 address {address}/64"]
            if FABRIC == "baseline":
                lines += bgp_config(node)
            else:
                # Installed once; staticd reinstalls them after a link flap.
                lines += [f"ipv6 route {prefix} {address} {iface}"
                          for prefix, address, iface in tables[name]["routes"]]
            (config_dir / f"{name}.conf").write_text("\n".join(lines) + "\n")
            entry["binds"] += [f"configs/{name}.conf:/etc/frr/frr.conf",
                               "configs/daemons:/etc/frr/daemons",
                               "configs/vtysh.conf:/etc/frr/vtysh.conf"]
        topology["topology"]["nodes"][name] = entry
    (ROOT / "mrc.clab.yml").write_text(json.dumps(topology, indent=2) + "\n")
    (ROOT / "fabric.json").write_text(json.dumps(fabric_config(), indent=2) + "\n")
    (ROOT / "results").mkdir(exist_ok=True)
    stale = ROOT / "paths.json"
    if stale.exists():
        stale.unlink()
    write_topology_readme(tables)


def path_rows():
    """Every h1 -> h2 SRv6 path, derived from the EV like the endpoint does."""
    rows = []
    for plane in range(1, PLANES + 1):
        for link in range(1, HOST_LINKS + 1):
            for spine in range(1, SPINES + 1):
                ev = mrc_paths.make_ev(plane, link, spine)
                rows.append({"ev": ev, "plane": plane, "link": link, "spine": spine,
                             "iface": host_port_iface("h1", plane, link),
                             "forward": mrc_paths.ev_address(ev, 1, 2),
                             "reverse": mrc_paths.ev_address(ev, 2, 1)})
    return rows


def write_topology_readme(tables):
    flags = f"--profile {profile_name()}" + (f" --fabric {FABRIC}" if FABRIC != "baseline" else "")
    diagram = ["```mermaid", "flowchart LR"]
    for link in LINKS:
        (node_a, port_a), (node_b, port_b) = (e.split(":") for e in link["endpoints"])
        diagram.append(f'  {node_a} ---|"{port_a} - {port_b}"| {node_b}')
    diagram.append("```")
    if FABRIC == "mrc":
        uids = "\n".join(f"| {n} | " + ", ".join(f"`{u:04x}`" for u in NODES[n]["usids"]) + " |"
                         for n in NODES)
        paths = "".join(
            f"| `{r['ev']:04x}` | {r['plane']} | {r['iface']} | s{r['plane']}{r['spine']} | "
            f"`{r['forward']}` | `{r['reverse']}` |\n" for r in path_rows())
        fabric_text = (
            "Fabric `mrc`: no routing protocol. Every switch holds one uN uSID per "
            "role (`End` + `next-csid`, block `fcbb:0::/32`, 16-bit uSIDs) and static "
            "`/48` routes to its neighbours' uSIDs. Endpoints send IPv6-in-IPv6: the "
            "outer destination is the path and every switch shifts it; the inner "
            "destination is the NIC address.\n\n"
            f"| Node | uSIDs |\n|---|---|\n{uids}\n\n"
            "| EV (low bits) | Plane | h1 port | Spine | h1 -> h2 outer destination | "
            "h2 -> h1 (same plane/port/spine) |\n|---|---:|---|---|---|---|\n" + paths + "\n")
    else:
        fabric_text = (
            "Fabric `baseline`: FRR eBGP (timers 1/3) with ECMP over a 5-tuple hash. "
            "Hosts own `fc00:100::1` / `fc00:200::1` and use a static ECMP route "
            "across their NIC ports. Packets are plain IPv6/UDP; the EV is the UDP "
            "source port, and the path is whatever the hashes pick.\n\n")
    (ROOT / "README.md").write_text(
        f"# {ROOT.name}\n\n"
        f"{PLANES} plane(s), {HOST_LINKS} NIC port(s) per plane, {len(NODES)} containers, "
        f"{len(LINKS)} links, {PLANES * HOST_LINKS * SPINES} distinct h1 -> h2 paths.\n\n"
        "Each plane has leaf `aN` (h1 side), leaf `bN` (h2 side), and spines "
        "`sN1`/`sN2`. Hosts are shared across planes; switches are not.\n\n"
        + "\n".join(diagram) + "\n\n" + fabric_text +
        "From the project root:\n\n```sh\n"
        f"python3 src/lab.py deploy {flags}\n"
        f"python3 src/lab.py test {flags}\n"
        f"python3 src/lab.py experiment link-down {flags}\n"
        f"python3 src/lab.py destroy {flags}\n```\n\n"
        "`mrc.clab.yml` is JSON (valid YAML). A bare `containerlab deploy` must be "
        f"followed by `python3 src/lab.py configure {flags}`. Results are written under "
        "`results/` (not version controlled).\n")


PREFLIGHT_SCRIPT = """
ip netns add a; ip netns add r; ip netns add b
ip link add va1 type veth peer name vr1; ip link set va1 netns a; ip link set vr1 netns r
ip link add vr2 type veth peer name vb2; ip link set vr2 netns r; ip link set vb2 netns b
for n in a r b; do ip -n $n link set lo up; done
ip -n a link set va1 up; ip -n r link set vr1 up; ip -n r link set vr2 up
ip -n b link set vb2 up
ip netns exec r sysctl -qw net.ipv6.conf.all.forwarding=1
ip -n a addr add fd01::1/64 dev va1 nodad; ip -n r addr add fd01::2/64 dev vr1 nodad
ip -n r addr add fd02::1/64 dev vr2 nodad; ip -n b addr add fd02::2/64 dev vb2 nodad
ip -n r link add sid0 type dummy; ip -n r link set sid0 up
ip -n r -6 route add fc00:1::1/128 encap seg6local action End dev sid0
ip -n r -6 route add fc00:2::/64 encap seg6 mode encap segs fc00:1::1 dev sid0
ip -n r -6 route add fcbb:0:1::/48 encap seg6local action End \
    flavors next-csid lblen 32 nflen 16 dev sid0
ip -n r -6 route add fcbb:0:2::/48 via fd02::2
ip -n a -6 route add fcbb:0:1::/48 via fd01::2
ip -n b addr add fcbb:0:2::/128 dev lo
echo ROUTES_BEGIN; ip -n r -6 -d route show; echo ROUTES_END
ip netns exec b timeout 6 tcpdump -nn -i vb2 -c 1 'ip6 dst fcbb:0:2::' >/dev/null 2>&1 &
sleep 1
ip netns exec a ping -6 -c 3 -W 1 fcbb:0:1:2:: >/dev/null 2>&1
wait $! && echo USID_FORWARD_OK
"""
REQUIREMENTS = {
    "baseline": (),  # plain IPv6 + BGP; SRv6 checks are informational
    "mrc": ("seg6local_end", "next_csid_retained", "usid_forwarding"),
}


def parse_preflight(output):
    routes = output.partition("ROUTES_BEGIN")[2].partition("ROUTES_END")[0]
    lines = {line.split()[1]: line for line in routes.splitlines()
             if line.startswith("unicast")}
    return {
        "seg6local_end": "seg6local action End" in lines.get("fc00:1::1", ""),
        "seg6_encap": "encap seg6 mode encap" in lines.get("fc00:2::/64", ""),
        "next_csid_retained": "next-csid" in lines.get("fcbb:0:1::/48", ""),
        "usid_forwarding": "USID_FORWARD_OK" in output,
    }


def kernel_version(release):
    match = re.match(r"(\d+)\.(\d+)", release)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def preflight(fabric):
    report = {"fabric": fabric, "kernel": platform.release(),
              "tested_at_utc": datetime.now(timezone.utc).isoformat()}
    missing = [tool for tool in ("docker", "containerlab") if not shutil.which(tool)]
    if missing:
        raise SystemExit(f"Preflight: missing tools on PATH: {', '.join(missing)}")
    result = subprocess.run(
        ["docker", "run", "--rm", "--privileged", IMAGE, "sh", "-c", PREFLIGHT_SCRIPT],
        text=True, capture_output=True)
    report["checks"] = parse_preflight(result.stdout)
    report["output"] = result.stdout + result.stderr
    (ROOT / "results").mkdir(parents=True, exist_ok=True)
    (ROOT / "results/capabilities.json").write_text(json.dumps(report, indent=2) + "\n")
    for name, passed in report["checks"].items():
        print(f"preflight {name}: {'OK' if passed else 'FAIL'}")
    failed = [name for name in REQUIREMENTS[fabric] if not report["checks"][name]]
    if failed:
        hint = ""
        if kernel_version(report["kernel"]) < (6, 1) and fabric == "mrc":
            hint = (" Native uSID (End with the next-csid flavor) needs Linux >= 6.1;"
                    " on Debian 11 install linux-image-amd64 from bullseye-backports,"
                    " or use Debian 12 / Ubuntu 24.04. Use --fabric baseline otherwise.")
        raise SystemExit(f"Preflight failed for fabric '{fabric}' on kernel "
                         f"{report['kernel']}: {', '.join(failed)}.{hint} "
                         f"Details: {ROOT / 'results/capabilities.json'}")
    return report


def configure():
    if FABRIC == "mrc":
        configure_mrc()
    else:
        configure_baseline()


def host_interfaces(name, node):
    for iface, address in node["interfaces"]:
        execute(name, "ip", "link", "set", iface, "up")
        if name.startswith("h"):
            execute(name, "ip", "-6", "addr", "replace", address + "/64", "dev", iface, "nodad")


def configure_baseline():
    for name, node in NODES.items():
        host_interfaces(name, node)
    for host, other in (("h1", "h2"), ("h2", "h1")):
        execute(host, "ip", "-6", "addr", "replace", NIC[host] + "/128", "dev", "lo", "nodad")
        # A host spreads flows over its NIC ports (all planes) by 5-tuple hash.
        args = ["ip", "-6", "route", "replace", NIC[other].rsplit(":", 2)[0] + "::/48"]
        for _, address, iface in NODES[host]["peers"]:
            args += ["nexthop", "via", address, "dev", iface, "weight", "1"]
        execute(host, *args)
    deadline = time.monotonic() + 90
    while subprocess.run(["docker", "exec", container("h1"), "ping", "-6", "-c", "1", "-W",
                          "1", "-I", NIC["h1"], NIC["h2"]], capture_output=True).returncode:
        if time.monotonic() >= deadline:
            raise RuntimeError("BGP fabric did not converge; inspect `vtysh -c 'show bgp summary'`")
        time.sleep(1)
    deadline = time.monotonic() + 30
    while True:
        try:
            control_plane_baseline()
            break
        except (RuntimeError, KeyError, StopIteration):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    print("Baseline fabric converged: eBGP up, ECMP installed, h1 reaches h2.")


def configure_mrc():
    tables = usid_tables()
    for name, node in NODES.items():
        host_interfaces(name, node)
        table = tables[name]
        if name.startswith("h"):
            for address in table["local"]:
                execute(name, "ip", "-6", "addr", "replace", address + "/128", "dev", "lo",
                        "nodad")
            for prefix, address, iface in table["routes"]:
                execute(name, "ip", "-6", "route", "replace", prefix, "via", address,
                        "dev", iface)
            continue
        # A dummy device keeps the End routes independent of data-link state.
        if execute(name, "ip", "link", "show", USID_DEVICE, quiet=True, check=False,
                   capture=True) == "":
            execute(name, "ip", "link", "add", USID_DEVICE, "type", "dummy")
        execute(name, "ip", "link", "set", USID_DEVICE, "up")
        for prefix in table["end"]:
            execute(name, "ip", "-6", "route", "replace", prefix, "encap", "seg6local",
                    "action", "End", *USID_FLAVOR, "dev", USID_DEVICE)
    wait_for_static_tables()
    directory = experiment_dir("reachability")
    with responder():
        execute("h1", "python3", "/opt/mrc/src/transport.py", "send", "--host", "h1",
                "--duration", "0.5", "--rate", "400",
                "--output", f"/results/experiments/{directory.name}/sender.json", quiet=True)
    result = json.loads((directory / "sender.json").read_text())
    unused = [ev for ev, s in result["evs"].items() if not s["acked"]]
    if result["delivered"] != result["sent_new"] or result["losses"] or unused:
        raise RuntimeError(f"Static uSID paths failed reachability: {result['delivered']}/"
                           f"{result['sent_new']} delivered, EVs without ACKs {unused}")
    print(f"All {len(result['evs'])} static uSID paths deliver and acknowledge traffic.")


def static_table_state():
    """Return (evidence, problems) for every switch's installed uSID table."""
    evidence, problems = {}, []
    for name, table in usid_tables().items():
        if name.startswith("h"):
            continue
        # Newer iproute2 warns about route attributes unrelated to SRv6.
        routes = execute(name, "ip", "-6", "-details", "route", "show", capture=True,
                         quiet=True)
        installed = {line.split()[1]: line for line in routes.splitlines()
                     if line.startswith("unicast fcbb:")}
        evidence[name] = sorted(installed.values())
        for prefix in table["end"]:
            if "next-csid" not in installed.get(prefix, ""):
                problems.append(f"{name}: missing End next-csid for {prefix}")
        for prefix, address, iface in table["routes"]:
            line = installed.get(prefix, "")
            if f"via {address} dev {iface}" not in line or "linkdown" in line:
                problems.append(f"{name}: missing static route {prefix} via {iface}")
    return evidence, problems


def wait_for_static_tables(timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        evidence, problems = static_table_state()
        if not problems:
            return evidence
        if time.monotonic() >= deadline:
            raise RuntimeError("Static uSID tables incomplete: " + "; ".join(problems))
        time.sleep(1)


def control_plane_baseline():
    evidence = {"neighbors": {}, "ecmp_routes": {}}
    for node in NODES:
        if node.startswith("h"):
            continue
        summary = json.loads(execute(node, "vtysh", "-c", "show bgp ipv6 unicast summary json",
                                     capture=True))
        peers = summary["peers"]
        if len(peers) != 2 or any(p["state"] != "Established" for p in peers.values()):
            raise RuntimeError(f"{node}: not all expected BGP sessions are established")
        evidence["neighbors"][node] = {a: p["state"] for a, p in peers.items()}
    for plane in range(1, PLANES + 1):
        prefix = "fc00:200::/48"
        routes = json.loads(execute(f"a{plane}", "vtysh", "-c", f"show ipv6 route {prefix} json",
                                    capture=True))
        route = next(r for r in routes[prefix] if r["protocol"] == "bgp")
        installed = [n for n in route["nexthops"] if n.get("fib") and n["active"]]
        if len(installed) != 2:
            raise RuntimeError(f"Plane {plane}: expected two installed ECMP nexthops")
        evidence["ecmp_routes"][f"a{plane}"] = [n.get("ip") for n in installed]
    return evidence


def control_plane_mrc():
    evidence = {"bgpd_running": {}}
    for node in NODES:
        if node.startswith("h"):
            continue
        if execute(node, "pgrep", "-x", "bgpd", check=False, capture=True).strip():
            raise RuntimeError(f"{node}: bgpd is running in the static mrc fabric")
        evidence["bgpd_running"][node] = False
    evidence["usid_tables"] = wait_for_static_tables(timeout=10)
    return evidence


# --- experiments -------------------------------------------------------------

def experiment_dir(name):
    directory = ROOT / "results" / "experiments" / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


@contextlib.contextmanager
def responder(host="h2"):
    execute(host, "rm", "-f", f"/results/stop-{host}", f"/results/ready-{host}")
    process = subprocess.Popen(["docker", "exec", container(host), "python3",
                                "/opt/mrc/src/transport.py", "serve", "--host", host])
    try:
        deadline = time.monotonic() + 10
        while not (ROOT / "results" / f"ready-{host}").exists():
            if process.poll() is not None:
                raise RuntimeError("Responder exited before becoming ready")
            if time.monotonic() >= deadline:
                raise RuntimeError("Responder did not become ready")
            time.sleep(0.05)
        yield
    finally:
        execute(host, "python3", "/opt/mrc/src/transport.py", "stop", "--host", host, check=False)
        process.wait(timeout=10)


def start_sender(directory, output, *args, host="h1"):
    return subprocess.Popen([
        "docker", "exec", container(host), "python3", "/opt/mrc/src/transport.py", "send",
        "--host", host, "--output", f"/results/experiments/{directory.name}/{output}",
        *[str(a) for a in args]], stdout=subprocess.DEVNULL)


def capture(directory, label, node, iface, expression, count=40, seconds=20):
    return subprocess.Popen([
        "docker", "exec", container(node), "timeout", str(seconds), "tcpdump", "-p", "-i", iface,
        "-U", "-s", "200", "-c", str(count), "-w",
        f"/results/experiments/{directory.name}/{label}.pcap", expression],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def decode_capture(directory, label, node, verbose=False):
    text = execute(node, "tcpdump", "-nn", *(["-v"] if verbose else []), "-r",
                   f"/results/experiments/{directory.name}/{label}.pcap",
                   capture=True, quiet=True, check=False)
    (directory / f"{label}.txt").write_text(text)
    return text


# Fault injection. Each helper runs its commands in one shell inside the
# container and returns the container's clock right after the commands
# all took effect. Containers share the host kernel clock, so this is directly
# comparable with the endpoint's timestamps (docker exec latency excluded).
def execute_timed(node, commands):
    # busybox `date` has no sub-second resolution. Python starts first, then
    # runs the commands and reads the clock immediately afterwards.
    script = ("import subprocess, sys, time\n"
              "subprocess.run(sys.argv[1], shell=True, check=True)\n"
              "print(time.time())")
    return float(execute(node, "python3", "-c", script, " && ".join(commands),
                         capture=True).split()[-1])


def parallel(*calls):
    """Run fault commands on several nodes at once, so a fault is one event."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(lambda call: call(), calls))


def set_link(node, iface, up):
    return execute_timed(node, [f"ip link set {iface} {'up' if up else 'down'}"])


def set_blackhole(node, iface, on):
    """Silent link failure: carrier stays up, every packet (BGP included) is dropped."""
    if on:
        return execute_timed(node, [f"tc qdisc add dev {iface} clsact",
                                    f"tc filter add dev {iface} egress matchall action drop"])
    return execute_timed(node, [f"tc qdisc del dev {iface} clsact 2>/dev/null; true"])


def set_gray(node, on):
    """Gray switch failure: control plane up, data (fc00::/8, fcbb::/16) dropped."""
    commands = []
    for iface, _ in NODES[node]["interfaces"]:
        if on:
            commands += [f"tc qdisc add dev {iface} clsact",
                         f"tc filter add dev {iface} ingress protocol ipv6 flower "
                         "dst_ip fc00::/8 action drop"]
        else:
            commands += [f"tc qdisc del dev {iface} clsact 2>/dev/null; true"]
    return execute_timed(node, commands)


def set_shaping(node, iface, mbit):
    """Rate-limit an uplink and ECN-mark (RED) instead of dropping."""
    execute(node, "tc", "qdisc", "del", "dev", iface, "root", check=False, quiet=True)
    if mbit:
        execute(node, "tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "htb",
                "default", "1")
        execute(node, "tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:1",
                "htb", "rate", f"{mbit}mbit", "burst", "15k")
        execute(node, "tc", "qdisc", "add", "dev", iface, "parent", "1:1", "handle", "10:",
                "red", "limit", "400000", "min", "8000", "max", "24000", "avpkt", "1100",
                "burst", "12", "probability", "0.5", "ecn", "bandwidth", f"{mbit}mbit")


def set_loss(node, iface, percent):
    """Random packet loss on an egress interface (netem); 0 removes it."""
    if percent:
        return execute_timed(node, [f"tc qdisc replace dev {iface} root netem loss {percent}%"])
    return execute_timed(node, [f"tc qdisc del dev {iface} root 2>/dev/null; true"])


def set_taildrop(node, iface, mbit):
    """Rate-limit an interface with a short drop-tail queue (no ECN); 0 removes it."""
    execute(node, "sh", "-c", f"tc qdisc del dev {iface} root 2>/dev/null; true", quiet=True)
    if mbit:
        execute(node, "tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "htb",
                "default", "1")
        execute(node, "tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:1",
                "htb", "rate", f"{mbit}mbit", "burst", "3k")
        execute(node, "tc", "qdisc", "add", "dev", iface, "parent", "1:1", "handle", "10:",
                "pfifo", "limit", "20")


def switches():
    return [name for name in NODES if not name.startswith("h")]


def uplinks(leaf_prefix):
    """(leaf, interface) for every leaf-to-spine link of the leaves starting with the prefix."""
    return [(name, iface) for name in NODES if name.startswith(leaf_prefix)
            for peer, _, iface in NODES[name]["peers"] if peer.startswith("s")]


BFD_PROFILE = ("fast", 3, 100)  # name, detect multiplier, interval in ms


def bfd_sessions_up(node):
    out = execute(node, "sh", "-c", "vtysh -c 'show bfd peers brief' | grep -c ' up' || true",
                  capture=True, quiet=True)
    return int(out.strip() or 0)


def set_bfd(on):
    """Baseline only: run BFD (3 x 100 ms) on every eBGP session, or remove it."""
    name, multiplier, interval = BFD_PROFILE
    for node in switches():
        if on and not execute(node, "pgrep", "-x", "bfdd", capture=True, check=False, quiet=True):
            # Deployments made before bfdd was enabled in the daemons file.
            execute(node, "/usr/lib/frr/bfdd", "-d", "-F", "traditional", "-A", "127.0.0.1",
                    quiet=True, capture=True, check=False)
    time.sleep(1 if on else 0)
    for node in switches():
        routers = [address for peer, address, _ in NODES[node]["peers"]
                   if not peer.startswith("h")]
        commands = ["configure"]
        if on:
            commands += ["bfd", f"profile {name}", f"detect-multiplier {multiplier}",
                         f"transmit-interval {interval}", f"receive-interval {interval}",
                         "exit", "exit"]
        commands += [f"router bgp {65000 + NODES[node]['id']}"]
        for address in routers:
            commands += ([f"neighbor {address} bfd", f"neighbor {address} bfd profile {name}"]
                         if on else [f"no neighbor {address} bfd"])
        if not on:
            commands += ["exit", "bfd", f"no profile {name}"]
        execute(node, "vtysh", *[arg for c in commands for arg in ("-c", c)], quiet=True)
    if on:
        sessions = sum(1 for node in switches() for peer, *_ in NODES[node]["peers"]
                       if not peer.startswith("h"))
        deadline = time.monotonic() + 30
        while True:
            up = sum(bfd_sessions_up(node) for node in switches())
            if up >= sessions:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"BFD sessions did not come up ({up}/{sessions})")
            time.sleep(0.5)


def pick_ecmp_ev(h1_iface, checks):
    """Find a UDP source port whose 5-tuple hashes onto the wanted path.

    `checks` is a list of (node, iif, wanted egress interface) evaluated with
    `ip route get`, i.e. with the switches' real ECMP hash.
    """
    ports = range(0xC000, 0xC000 + 1500)
    query = ("for p in " + " ".join(map(str, ports)) + "; do echo $p $(ip -6 route get "
             f"{NIC['h2']} from {NIC['h1']} ipproto udp sport $p dport {transport_port()} "
             "{iif}); done")
    candidates = set()
    for line in execute("h1", "sh", "-c", query.format(iif=""), capture=True).splitlines():
        port, *rest = line.split()
        if f"dev {h1_iface} " in " ".join(rest) + " ":
            candidates.add(int(port))
    for node, iif, egress in checks:
        text = execute(node, "sh", "-c", query.format(iif=f"iif {iif}"), capture=True)
        candidates &= {int(line.split()[0]) for line in text.splitlines()
                       if f" dev {egress} " in line}
    if not candidates:
        raise RuntimeError("No source port hashes onto the requested ECMP path")
    return sorted(candidates)


def transport_port():
    return 4791


def analyze(result, t_fault=None, t_restore=None):
    """Turn a sender's 10 ms timeline into failure-response metrics."""
    t0, width = result["t0"], result["bin_seconds"]
    bins = {round(b["t"] / width): b for b in result["timeline"]}

    def rate(start, end):
        first, last = int((start - t0) / width), int((end - t0) / width)
        return sum(bins.get(i, {}).get("delivered", 0) for i in range(first, last)) / (end - start)

    summary = {k: result[k] for k in ("algo", "fabric", "sent_new", "delivered", "undelivered",
                                       "losses", "retransmissions") if k in result}
    summary["goodput_pps"] = round(result["delivered"] / max(result["duration"], 1e-9))
    if t_fault is None:
        return summary
    before = rate(t_fault - 0.5, t_fault)
    end = result["finished"]
    # Recovered: back to >= 90% of the pre-fault goodput, sustained for 250 ms.
    recovered = None
    t = t_fault
    while t + 0.25 <= end:
        if rate(t, t + 0.05) >= 0.9 * before and rate(t + 0.05, t + 0.25) >= 0.9 * before:
            recovered = t
            break
        t += width
    gap, longest = 0, 0
    for i in range(int((t_fault - t0) / width), int(((t_restore or end) - t0) / width)):
        gap = gap + 1 if not bins.get(i, {}).get("delivered") else 0
        longest = max(longest, gap)
    lost_after = sum(b.get("lost", 0) for b in result["timeline"] if t0 + b["t"] >= t_fault)
    summary.update({
        "goodput_before_pps": round(before),
        "goodput_during_fault_pps": round(rate(t_fault, t_restore or end)),
        "recovery_ms": None if recovered is None else round((recovered - t_fault) * 1000),
        "recovered_before_repair": recovered is not None and (
            t_restore is None or recovered < t_restore - 0.05),
        "longest_zero_delivery_ms": round(longest * width * 1000),
        "losses_after_fault": lost_after,
    })
    events = result.get("events", [])
    retire = [e for e in events if e["event"] == "ev_state" and e["new"] in (
        "ASSUMED_BAD", "DENIED") and e["t"] >= t_fault]
    if retire:
        summary["first_ev_retired_ms"] = round((retire[0]["t"] - t_fault) * 1000)
        summary["evs_retired"] = sorted({e["ev"] for e in retire
                                         if t_restore is None or e["t"] < t_restore})
        swaps = [e for e in events if e["event"] == "ev_activated" and e["t"] >= t_fault
                 and (t_restore is None or e["t"] < t_restore)]
        summary["backup_evs_activated"] = sorted({e["ev"] for e in swaps})
    if t_restore is not None:
        summary["ev_state_changes_during_fault"] = sum(
            1 for e in events if e["event"] == "ev_state" and t_fault <= e["t"] < t_restore)
        back = [e for e in events if e["t"] >= t_restore and (
            e["event"] == "ev_resurrected" or (e["event"] == "ev_state" and e["new"] == "BACKUP"))]
        if back:
            summary["first_ev_restored_ms"] = round((back[0]["t"] - t_restore) * 1000)
    if result["algo"] == "roce":
        summary["go_back_n_events"] = sum(1 for e in events if e["event"] == "go_back_n")
    return summary


def mrc_sender_args():
    per_plane = HOST_LINKS * SPINES
    # With 4 paths per plane, keep 2 active and 2 as same-plane backups (MRC CX8 style).
    return ["--active-per-plane", per_plane // 2] if per_plane >= 4 else []


FAULTS = {
    # name: (description, inject(on), fault time, repair time, duration)
    "link-down": ("T0-T1 link a1-s11 administratively down, then up",
                  lambda on: set_link("a1", iface_to("a1", "s11"), not on), 1.0, 3.0, 6.0),
    "silent-link": ("T0-T1 link a1-s11 drops everything (carrier stays up)",
                    # The fault time is when both directions are cut.
                    lambda on: max(parallel(
                        lambda: set_blackhole("a1", iface_to("a1", "s11"), on),
                        lambda: set_blackhole("s11", iface_to("s11", "a1"), on))),
                    1.0, 5.5, 7.5),
    "partial-loss": ("T0-T1 link a1-s11 drops 20% of packets in both directions",
                     lambda on: max(parallel(
                         lambda: set_loss("a1", iface_to("a1", "s11"), 20 if on else 0),
                         lambda: set_loss("s11", iface_to("s11", "a1"), 20 if on else 0))),
                     1.0, 5.5, 7.5),
    "gray-switch": ("Spine s11 stops forwarding data; its control plane stays up",
                    lambda on: set_gray("s11", on), 1.0, 4.0, 5.5),
    # The leaf side goes down, so h2's NIC sees loss of carrier (as with a
    # cable or transceiver fault) and keeps its own configuration. All of
    # h2's plane-1 ports fail, so plane 1 can no longer reach h2.
    "nic-plane": ("h2 loses its plane-1 NIC port(s) (links to b1 down), then they recover",
                  lambda on: execute_timed("b1", [
                      f"ip link set {i} {'down' if on else 'up'}"
                      for i in plane_ports("b1", "h2")]),
                  1.0, 3.0, 5.0),
}


def rx_packets(node, iface):
    return int(execute(node, "cat", f"/sys/class/net/{iface}/statistics/rx_packets",
                       capture=True))


def fault_sender_args(name, directory):
    """Sender arguments; call with the responder running."""
    if FABRIC == "mrc":
        return ["--algo", "mrc", *mrc_sender_args()]
    # Baseline: a single-path RoCE-style QP whose ECMP hash crosses the fault.
    # `ip route get` predicts the hash; a short burst counted on s11 confirms it.
    checks = [("a1", iface_to("a1", "h1"), iface_to("a1", "s11"))]
    s11_port = iface_to("s11", "a1")
    for sport in pick_ecmp_ev(iface_to("h1", "a1"), checks)[:10]:
        before = rx_packets("s11", s11_port)
        execute("h1", "python3", "/opt/mrc/src/transport.py", "send", "--host", "h1", "--algo",
                "roce", "--evs", f"{sport:x}", "--qp", "9", "--rate", "1000", "--duration",
                "0.2", "--output", f"/results/experiments/{directory.name}/pin-check.json",
                quiet=True)
        if rx_packets("s11", s11_port) - before >= 200:
            return ["--algo", "roce", "--evs", f"{sport:x}"]
    raise RuntimeError("Could not pin a single-path flow onto the a1-s11 link")


class Monitor:
    """Poll a command in a container and record each change of its output."""

    def __init__(self, label, node, script, interval=0.2):
        import threading
        self.label, self.node, self.script, self.interval = label, node, script, interval
        self.changes, self.stop = [], threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        last = None
        while not self.stop.is_set():
            # The container clock is read right after the command, inside the container.
            out = execute(self.node, "sh", "-c", f"{self.script}; echo; "
                          "python3 -c 'import time; print(time.time())'",
                          capture=True, quiet=True, check=False).strip().splitlines()
            if out:
                value, stamp = " ".join(x.strip() for x in out[:-1]).strip(), float(out[-1])
                if value != last:
                    self.changes.append({"t": stamp, "value": value})
                    last = value
            self.stop.wait(self.interval)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)

    def relative(self, origin):
        return [{"t_ms": round((c["t"] - origin) * 1000), "value": c["value"]}
                for c in self.changes]


def fault_monitors(name, bfd=False):
    """What the control plane believes during the fault (the point of comparison)."""
    s11 = peer_address("a1", iface_to("a1", "s11"))
    if FABRIC == "baseline":
        bgp = (f"vtysh -c 'show bgp neighbors {s11} json' | "
               "grep -o '\"bgpState\":\"[A-Za-z]*\"' | head -1")
        monitors = [Monitor("a1 BGP session to s11", "a1", bgp),
                    Monitor("a1 ECMP next hops to fc00:200::/48", "a1",
                            "ip -6 route show fc00:200::/48 | grep -cE 'nexthop|via' || true")]
        if bfd:
            monitors.insert(1, Monitor(
                "a1 BFD session to s11", "a1",
                f"vtysh -c 'show bfd peers brief' | grep -F {s11} | awk '{{print $NF}}'"))
        if name == "nic-plane":
            monitors = [Monitor("a1 route to h2 (fc00:200::/48) in plane 1", "a1",
                                "ip -6 route show fc00:200::/48 | head -1 | cut -c1-40"),
                        Monitor("h1 ECMP next hops to fc00:200::/48 (all planes)", "h1",
                                "ip -6 route show fc00:200::/48 | grep -cE 'nexthop|via' || true")]
        return monitors
    return [Monitor("a1 uSID routes installed", "a1", "ip -6 route show | grep -c fcbb"),
            Monitor("bgpd processes on a1", "a1", "pgrep -x bgpd | wc -l")]


def run_fault(name, bfd=False):
    """Inject one fault; with bfd, the baseline's eBGP sessions run BFD first."""
    description, inject, fault_at, repair_at, duration = FAULTS[name]
    if name == "nic-plane" and PLANES < 2:
        raise SystemExit("nic-plane needs a multi-plane profile")
    label = f"{name}-bfd" if bfd else name
    directory = experiment_dir(label)
    if bfd:
        set_bfd(True)
    try:
        summary = run_fault_once(name, label, directory, bfd)
    finally:
        if bfd:
            set_bfd(False)
    return summary


def run_fault_once(name, label, directory, bfd):
    description, inject, fault_at, repair_at, duration = FAULTS[name]
    monitors = fault_monitors(name, bfd)
    with responder(), contextlib.ExitStack() as stack:
        sender_args = fault_sender_args(name, directory)
        for monitor in monitors:
            stack.enter_context(monitor)
        # Fault times are relative to the sender's real start, not to docker exec.
        start = time.time() + 1.5
        sender = start_sender(directory, "sender.json", *sender_args, "--rate", 2000,
                              "--duration", duration, "--drain", 8, "--start-at", start)
        repaired = False
        try:
            around = name == "silent-link" and FABRIC == "mrc" and not bfd
            time.sleep(max(0.0, start + fault_at - (0.5 if around else 0) - time.time()))
            if around:
                # Packets on one NIC port around the fault: EVs going quiet, resends.
                around_cap = capture(directory, "h1-port1-around-fault", "h1",
                                 host_port_iface("h1", 1, 1), "ip6 and ip6[6] == 41",
                                 count=4000, seconds=3)
                time.sleep(max(0.0, start + fault_at - time.time()))
            t_fault = inject(True)
            if name == "gray-switch":
                # What leaves the "broken" spine towards b1 during the fault.
                cap = capture(directory, "s11-to-b1-during-fault", "s11",
                              iface_to("s11", "b1"), "ip6 and not icmp6", count=30,
                              seconds=int(repair_at - fault_at) - 0.5)
            time.sleep(max(0.0, start + repair_at - time.time()))
            t_repair = inject(False)
            repaired = True
        finally:
            if not repaired:
                inject(False)
        sender.wait(timeout=duration + 30)
    if around:
        around_cap.wait(timeout=30)
    if name == "gray-switch":
        cap.wait(timeout=30)
        decode_capture(directory, "s11-to-b1-during-fault", "s11")
    result = json.loads((directory / "sender.json").read_text())
    shutil.copy(ROOT / "results" / "receiver-h2.json", directory / "receiver.json")
    summary = {"experiment": label, "description": description + (
                   f" (eBGP sessions run BFD, {BFD_PROFILE[1]} x {BFD_PROFILE[2]} ms)"
                   if bfd else ""),
               "profile": profile_name(), "fabric": FABRIC, "sender_args": sender_args, "fault_at": t_fault,
               "repair_at": t_repair, **analyze(result, t_fault, t_repair),
               "control_plane_ms_after_fault": {m.label: m.relative(t_fault) for m in monitors}}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def run_spray():
    """MRC fabric: one QP sprays over every EV; captures show the uSID shifting."""
    if FABRIC != "mrc":
        raise SystemExit("spray evidence is for --fabric mrc")
    directory = experiment_dir("spray")
    rows = path_rows()
    first = rows[0]
    points = {
        "1-h1-port1": ("h1", first["iface"]),
        "2-s11-ingress": ("s11", iface_to("s11", "a1")),
        "3-b1-to-h2": ("b1", iface_to("b1", "h2")),
        "4-h2-ingress": ("h2", host_port_iface("h2", 1, 1)),
    }
    expression = "ip6 and ip6[6] == 41 and ip6 dst net fcbb::/16"
    with responder():
        captures = {label: capture(directory, label, node, iface, expression)
                    for label, (node, iface) in points.items()}
        time.sleep(1)
        execute("h1", "python3", "/opt/mrc/src/transport.py", "send", "--host", "h1", "--rate", "1000",
                "--duration", "2", "--output", f"/results/experiments/{directory.name}/sender.json",
                quiet=True)
        for process in captures.values():
            process.wait(timeout=30)
    result = json.loads((directory / "sender.json").read_text())
    receiver = json.loads((ROOT / "results" / "receiver-h2.json").read_text())
    shutil.copy(ROOT / "results" / "receiver-h2.json", directory / "receiver.json")
    hops = {label: decode_capture(directory, label, node, verbose=True)
            for label, (node, _) in points.items()}
    expected = {
        "1-h1-port1": first["forward"],
        "2-s11-ingress": mrc_paths.usid_shift(first["forward"]),
        "3-b1-to-h2": mrc_paths.usid_shift(mrc_paths.usid_shift(mrc_paths.usid_shift(
            first["forward"]))),
        "4-h2-ingress": mrc_paths.usid_address([mrc_paths.host_usid(2, 1, 1)]),
    }
    seen = {label: f"> {address}:" in text for label, text in hops.items()
            for address in [expected[label]]}
    per_ev = {ev: s["sent"] for ev, s in result["evs"].items()}
    per_plane = {}
    for ev, count in per_ev.items():
        plane = str(mrc_paths.ev_fields(int(ev, 16))[0])
        per_plane[plane] = per_plane.get(plane, 0) + count
    qp = next(iter(receiver["qps"].values()))
    summary = {"experiment": "spray", "profile": profile_name(), "fabric": FABRIC,
               **analyze(result), "psn_range": result["psn_range"],
               "packets_per_ev": per_ev, "packets_per_plane": per_plane,
               "receiver_packets_per_ev": qp["per_ev"],
               "receiver_reverse_ev_per_plane": qp["reverse"],
               "expected_outer_destination": expected, "seen_in_capture": seen}
    if not all(seen.values()) or result["delivered"] != result["sent_new"]:
        raise RuntimeError(f"Spray evidence incomplete: {seen}")
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def qdisc_stats(node, iface):
    """RED counters on a shaped uplink: packets ECN-marked vs really dropped."""
    text = execute(node, "tc", "-s", "qdisc", "show", "dev", iface, capture=True)
    red = text[text.index("qdisc red"):] if "qdisc red" in text else ""
    number = lambda pattern: int((re.search(pattern, red) or [0, 0])[1])
    return {"sent_packets": number(r"Sent \d+ bytes (\d+) pkt"),
            "dropped": number(r"dropped (\d+)"), "ecn_marked": number(r"marked (\d+)"),
            "early_drops": number(r"early (\d+)"), "tail_drops": number(r"pdrop (\d+)")}


def run_ecn():
    """ECN as a load-balancing signal: a competing flow congests one path."""
    if FABRIC != "mrc":
        raise SystemExit("ecn experiment is for --fabric mrc")
    results = {}
    uplinks = [iface_to("a1", "s11"), iface_to("a1", "s12")]
    try:
        for variant, extra in (("ecn-avoid", []), ("no-ecn-avoid", ["--no-ecn-avoid"])):
            for iface in uplinks:  # fresh queues and counters per variant
                set_shaping("a1", iface, 20)
            directory = experiment_dir(f"ecn-{variant}")
            ev_a = mrc_paths.make_ev(1, 1, 1)
            ev_b = mrc_paths.make_ev(1, 1, 2)
            with responder():
                cap = capture(directory, "s11-ingress", "s11", iface_to("s11", "a1"),
                              "ip6 and ip6[6] == 41 and ip6[1] & 0x30 == 0x30", count=20)
                # Both flows start at scheduled wall-clock times (not docker exec time).
                start = time.time() + 1.5
                t_competitor = start + 2.0
                flow_a = start_sender(directory, "flow-a.json", "--qp", 1, "--evs",
                                      f"{1 << 16 | ev_a:x},{1 << 16 | ev_b:x}", "--rate", 2400,
                                      "--size", 1000, "--duration", 6, "--inflight", 64,
                                      "--start-at", start, *extra)
                flow_b = start_sender(directory, "flow-b.json", "--qp", 2, "--evs",
                                      f"{2 << 16 | ev_a:x}", "--rate", 1400, "--size", 1000,
                                      "--duration", 2.5, "--inflight", 64, "--no-ecn-avoid",
                                      "--start-at", t_competitor)
                flow_a.wait(timeout=30)
                flow_b.wait(timeout=30)
                cap.wait(timeout=30)
            decode_capture(directory, "s11-ingress", "s11", verbose=True)
            a = json.loads((directory / "flow-a.json").read_text())
            b = json.loads((directory / "flow-b.json").read_text())
            shutil.copy(ROOT / "results" / "receiver-h2.json", directory / "receiver.json")

            def share(lo, hi):
                counts = {}
                for row in a["timeline"]:
                    if lo <= a["t0"] + row["t"] - t_competitor < hi:
                        for ev, n in row.get("sent_by_ev", {}).items():
                            counts[ev] = counts.get(ev, 0) + n
                total = sum(counts.values()) or 1
                return {("via s11" if int(ev, 16) & 0xF == 1 else "via s12"): round(n / total, 2)
                        for ev, n in sorted(counts.items())}

            summary = {"experiment": f"ecn-{variant}", "profile": profile_name(),
                       "fabric": FABRIC, "uplink_mbit": 20,
                       "flow_a": analyze(a), "flow_b": analyze(b),
                       "flow_a_share_before_competitor": share(-1.5, 0),
                       "flow_a_share_with_competitor": share(0.5, 2.5),
                       "flow_a_share_after_competitor": share(3.0, 4.0),
                       "flow_a_ce_echoes": sum(e["ce"] for e in a["evs"].values()),
                       "uplink_queues": {f"a1->{peer}": qdisc_stats("a1", iface_to("a1", peer))
                                         for peer in ("s11", "s12")},
                       # Packets the sender declared lost but that were acknowledged
                       # anyway: queueing delay exceeded the fixed RTO.
                       "spurious_loss_declarations": {
                           flow: sum(min(e["lost"], e["acked"] - (e["sent"] - e["lost"]))
                                     if e["acked"] > e["sent"] - e["lost"] else 0
                                     for e in r["evs"].values())
                           for flow, r in (("flow_a", a), ("flow_b", b))},
                       "flow_a_evs_retired": [ev for ev, e in a["evs"].items() if e["lost"]],
                       "flow_a_events": [e for e in a["events"] if e["event"] == "ev_state"]}
            (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            results[variant] = summary
    finally:
        for iface in uplinks:
            set_shaping("a1", iface, 0)
    return results


def run_clustermap():
    if FABRIC != "mrc":
        raise SystemExit("clustermap uses SRv6 self-probes: --fabric mrc")
    directory = experiment_dir("clustermap")
    reports = {}
    iface = iface_to("a1", "s11")
    for state in ("healthy", "a1-s11-silent-failure"):
        if state != "healthy":
            set_blackhole("a1", iface, True)
        try:
            output = f"/results/experiments/{directory.name}/{state}.json"
            execute("h1", "python3", "/opt/mrc/src/transport.py", "clustermap", "--host", "h1",
                    "--count", "20", "--output", output, quiet=True)
        finally:
            set_blackhole("a1", iface, False)
        reports[state] = json.loads((directory / f"{state}.json").read_text())
    summary = {"experiment": "clustermap", "profile": profile_name(), "fabric": FABRIC,
               **{state: {"probes": {t["target"]: f"{t['received']}/{t['sent']}"
                                     for t in r["targets"]},
                          "probe_address": {t["target"]: t["address"] for t in r["targets"]},
                          "diagnosis": r["diagnosis"]} for state, r in reports.items()}}
    if summary["healthy"]["diagnosis"] or not summary["a1-s11-silent-failure"]["diagnosis"]:
        raise RuntimeError("Clustermapper did not localize the injected failure")
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def window_goodput(result, seconds):
    """Packets delivered per second inside the send window (retransmission drain excluded)."""
    return sum(b.get("delivered", 0) for b in result["timeline"] if b["t"] < seconds) / seconds


def single_path_sender_args(sport):
    return ["--algo", "roce", "--evs", f"{sport:x}"]


def spray_sender_args():
    return ["--algo", "mrc", *mrc_sender_args()]


def tx_packets(node, iface):
    return int(execute(node, "cat", f"/sys/class/net/{iface}/statistics/tx_packets",
                       capture=True))


COLLISION = {"flows": 4, "rate": 700, "size": 1000, "mbit": 10, "duration": 6, "seeds": (1, 2, 3)}


def run_ecmp_collision():
    """Paper section 1: hash-based ECMP collides flows; spraying does not.

    Every h1 NIC port and every leaf uplink is limited to `mbit` with a short
    drop-tail queue. Several flows that each use well under one link lose
    nothing if spread perfectly; any collision overloads a link.
    """
    import random
    cfg = COLLISION
    directory = experiment_dir("ecmp-collision")
    nic_links = [("h1", iface) for iface, _ in NODES["h1"]["interfaces"]]
    leaf_links = uplinks("a")
    links = nic_links + leaf_links
    trials = []
    try:
        for seed in cfg["seeds"]:
            for node, iface in links:
                set_taildrop(node, iface, cfg["mbit"])
            sports = random.Random(seed).sample(range(0xC000, 0xFFFF), cfg["flows"])
            before = {f"{n}:{i}": tx_packets(n, i) for n, i in links}
            with responder():
                start = time.time() + 1.5
                senders = []
                for qp in range(1, cfg["flows"] + 1):
                    args = (single_path_sender_args(sports[qp - 1]) if FABRIC == "baseline"
                            else spray_sender_args())
                    senders.append(start_sender(
                        directory, f"trial{seed}-flow{qp}.json", *args, "--qp", qp, "--rate",
                        cfg["rate"], "--size", cfg["size"], "--duration", cfg["duration"],
                        "--drain", 3, "--start-at", start))
                for sender in senders:
                    sender.wait(timeout=60)
            used = {k: tx_packets(*k.split(":")) - v for k, v in before.items()}
            flows = [json.loads((directory / f"trial{seed}-flow{qp}.json").read_text())
                     for qp in range(1, cfg["flows"] + 1)]
            goodput = [round(window_goodput(f, cfg["duration"])) for f in flows]
            busy = lambda k: used[k] > 0.02 * max(used.values())
            uplink_keys = [f"{n}:{i}" for n, i in leaf_links]
            trials.append({
                "seed": seed, "per_flow_goodput_pps": goodput,
                "aggregate_goodput_pps": sum(goodput),
                "worst_flow_goodput_pps": min(goodput),
                "retransmissions": sum(f["retransmissions"] for f in flows),
                "link_tx_packets": used,
                "uplinks_carrying_traffic": f"{sum(busy(k) for k in uplink_keys)}"
                                            f"/{len(uplink_keys)}",
                "busiest_uplink_to_average": round(
                    max(used[k] for k in uplink_keys) /
                    (sum(used[k] for k in uplink_keys) / len(uplink_keys)), 2)})
    finally:
        for node, iface in links:
            set_taildrop(node, iface, 0)
    summary = {"experiment": "ecmp-collision", "profile": profile_name(), "fabric": FABRIC,
               "description": f"{cfg['flows']} flows of {cfg['rate']} pkt/s over links limited "
                              f"to {cfg['mbit']} Mbit/s",
               "link_mbit": cfg["mbit"], "offered_aggregate_pps": cfg["flows"] * cfg["rate"],
               "mean_aggregate_goodput_pps": round(
                   sum(t["aggregate_goodput_pps"] for t in trials) / len(trials)),
               "mean_worst_flow_goodput_pps": round(
                   sum(t["worst_flow_goodput_pps"] for t in trials) / len(trials)),
               "trials": trials}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


RANDOM_LOSS = {"percents": (0, 0.1, 1.0), "rate": 4000, "duration": 6}


def run_random_loss():
    """Paper section 5.2.7: uniform random loss on every plane, RoCE-style vs MRC."""
    import random
    cfg = RANDOM_LOSS
    directory = experiment_dir("random-loss")
    links = uplinks("a") + uplinks("b")
    cases = {}
    try:
        for percent in cfg["percents"]:
            for node, iface in links:
                set_loss(node, iface, percent)
            name = f"loss-{percent}"
            with responder():
                args = (single_path_sender_args(random.Random(7).randrange(0xC000, 0xFFFF))
                        if FABRIC == "baseline" else spray_sender_args())
                sender = start_sender(directory, f"{name}.json", *args, "--rate", cfg["rate"],
                                      "--duration", cfg["duration"], "--drain", 10)
                sender.wait(timeout=90)
            result = json.loads((directory / f"{name}.json").read_text())
            cases[str(percent)] = {
                "goodput_pps": round(window_goodput(result, cfg["duration"])),
                "offered_pps": cfg["rate"],
                "delivered": result["delivered"], "sent_new": result["sent_new"],
                "undelivered": result["undelivered"],
                "retransmissions": result["retransmissions"],
                "retransmitted_pct_of_new": round(
                    100 * result["retransmissions"] / max(result["sent_new"], 1), 1),
                "completed_after_s": round(result["finished"] - result["started"], 2)}
    finally:
        for node, iface in links:
            set_loss(node, iface, 0)
    summary = {"experiment": "random-loss", "profile": profile_name(), "fabric": FABRIC,
               "description": "Uniform random loss on every leaf-to-spine link, both directions",
               "cases_by_loss_percent": cases}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


EXPERIMENTS = {
    "spray": run_spray,
    "link-down": lambda: run_fault("link-down"),
    "silent-link": lambda: run_fault("silent-link"),
    "gray-switch": lambda: run_fault("gray-switch"),
    "nic-plane": lambda: run_fault("nic-plane"),
    "partial-loss": lambda: run_fault("partial-loss"),
    "random-loss": run_random_loss,
    "ecmp-collision": run_ecmp_collision,
    "silent-link-bfd": lambda: run_fault("silent-link", bfd=True),
    "gray-switch-bfd": lambda: run_fault("gray-switch", bfd=True),
    "ecn": run_ecn,
    "clustermap": run_clustermap,
}
MRC_ONLY = {"spray", "ecn", "clustermap"}
BASELINE_ONLY = {"silent-link-bfd", "gray-switch-bfd"}


def applicable_experiments():
    names = [n for n in EXPERIMENTS if (FABRIC == "mrc" or n not in MRC_ONLY)
             and (FABRIC == "baseline" or n not in BASELINE_ONLY)]
    return [n for n in names if n != "nic-plane" or PLANES >= 2]


def run_experiments(names):
    with (ROOT / "results" / ".experiment.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        summaries = {}
        for name in names:
            print(f"== {name} ({FABRIC})", flush=True)
            summaries[name] = EXPERIMENTS[name]()
            print(json.dumps(summaries[name], indent=2), flush=True)
        return summaries


def test():
    """Smoke test: control plane, spraying (mrc), and one failure + repair."""
    evidence = control_plane_baseline() if FABRIC == "baseline" else control_plane_mrc()
    (ROOT / "results" / "control-plane.json").write_text(json.dumps(evidence, indent=2) + "\n")
    names = (["spray"] if FABRIC == "mrc" else []) + ["link-down"]
    summaries = run_experiments(names)
    link = summaries["link-down"]
    if link["delivered"] != link["sent_new"]:
        raise RuntimeError("Not every packet was eventually delivered")
    if FABRIC == "mrc" and not (link.get("first_ev_retired_ms") is not None
                                and link.get("first_ev_restored_ms") is not None):
        raise RuntimeError("MRC did not retire and later restore the failed paths")
    report = {"profile": profile_name(), "fabric": FABRIC, "status": "PASS",
              "tested_at_utc": datetime.now(timezone.utc).isoformat(),
              "kernel": platform.release(), "experiments": summaries}
    (ROOT / "results" / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS {profile_name()} ({FABRIC})")


EVIDENCE_FILES = ("summary.json", "*.txt", "*.pcap")


def collect_evidence(names):
    """Copy experiment results (small pcaps included) into docs/evidence."""
    target = PROJECT / "docs" / "evidence" / profile_name() / FABRIC
    for name in names:
        source_names = [f"ecn-{v}" for v in ("ecn-avoid", "no-ecn-avoid")] if name == "ecn" \
            else [name]
        for source_name in source_names:
            source = ROOT / "results" / "experiments" / source_name
            destination = target / source_name
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir(parents=True)
            for pattern in EVIDENCE_FILES + ("sender.json", "receiver.json", "flow-*.json",
                                             "healthy.json", "a1-s11-*.json"):
                for path in source.glob(pattern):
                    shutil.copy(path, destination / path.name)
    meta = {"profile": profile_name(), "fabric": FABRIC, "kernel": platform.release(),
            "collected_at_utc": datetime.now(timezone.utc).isoformat(),
            "control_plane": json.loads((ROOT / "results" / "control-plane.json").read_text())
            if (ROOT / "results" / "control-plane.json").exists() else None}
    (target / "environment.json").write_text(json.dumps(meta, indent=2) + "\n")
    give_back(PROJECT / "docs" / "evidence")
    print(f"Evidence written to {target}")


def give_back(path):
    """Files created under sudo belong to the invoking user, not root."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        for item in [path, *path.rglob("*")]:
            os.chown(item, int(uid), int(gid))


def summarize(root):
    """Recompute fault summaries from stored raw sender data (no lab needed)."""
    for path in sorted(Path(root).rglob("summary.json")):
        summary = json.loads(path.read_text())
        sender = path.parent / "sender.json"
        if "fault_at" not in summary or not sender.exists():
            continue
        result = json.loads(sender.read_text())
        summary.update(analyze(result, summary["fault_at"], summary["repair_at"]))
        path.write_text(json.dumps(summary, indent=2) + "\n")


def goodput_series(directory, summary, before=0.5, after=None, step=0.05):
    """Delivered packets/s in 50 ms buckets, relative to the fault time."""
    result = json.loads((directory / "sender.json").read_text())
    after = after or (summary["repair_at"] - summary["fault_at"]) + 0.75
    counts = {}
    for row in result["timeline"]:
        offset = result["t0"] + row["t"] - summary["fault_at"]
        if -before <= offset < after:
            key = round(int((offset + before) / step) * step - before, 2)
            counts[key] = counts.get(key, 0) + row.get("delivered", 0)
    keys = [round(-before + i * step, 2) for i in range(int((before + after) / step))]
    return keys, [round(counts.get(k, 0) / step) for k in keys]


def _load(path):
    return json.loads(path.read_text()) if path.exists() else None


def report_bfd(base):
    rows = []
    for name in ("silent-link", "gray-switch"):
        plain = _load(base / "baseline" / name / "summary.json")
        fast = _load(base / "baseline" / f"{name}-bfd" / "summary.json")
        if plain and fast:
            rows.append(f"| `{name}` | {plain['longest_zero_delivery_ms']} ms | "
                        f"{fast['longest_zero_delivery_ms']} ms"
                        f"{'' if fast['recovered_before_repair'] else ' (until repair)'} |")
    if not rows:
        return []
    return ["## Does BFD fix the baseline? (`silent-link-bfd`, `gray-switch-bfd`)\n\n"
            "BGP timers here are already 1 s keepalive / 3 s hold (real defaults are 60/180 s). "
            "The BFD runs use 100 ms x 3 on every eBGP session.\n\n"
            "| Fault | Longest outage, BGP timers only | Longest outage, BGP + BFD |\n|---|---|---|\n"
            + "\n".join(rows) + "\n\nBFD only helps where a link-level hello is lost. A spine that "
            "keeps answering BFD while dropping transit traffic is invisible to it.\n"]


def report_collision(base):
    data = {f: _load(base / f / "ecmp-collision" / "summary.json") for f in FABRICS}
    if not all(data.values()):
        return []
    rows = []
    for fabric, summary in data.items():
        for t in summary["trials"]:
            rows.append(f"| {fabric} | {t['seed']} | {t['uplinks_carrying_traffic']} | "
                        f"{t['aggregate_goodput_pps']} | {t['worst_flow_goodput_pps']} | "
                        f"{t['retransmissions']} |")
    b, m = data["baseline"], data["mrc"]
    return ["## ECMP flow collisions (`ecmp-collision`)\n\n"
            f"4 flows of 700 pkt/s (offered {b['offered_aggregate_pps']}) cross links limited to "
            f"{b['link_mbit']} Mbit/s. Baseline flows get random UDP source ports (ECMP hash); "
            "mrc flows spray over all paths. Three random seeds each.\n\n"
            "| Fabric | Seed | Uplinks used | Aggregate pkt/s | Worst flow pkt/s | Retransmissions |\n"
            "|---|---|---|---|---|---|\n" + "\n".join(rows)
            + f"\n\nMean aggregate: baseline {b['mean_aggregate_goodput_pps']}, "
            f"mrc {m['mean_aggregate_goodput_pps']} pkt/s. Mean worst flow: baseline "
            f"{b['mean_worst_flow_goodput_pps']}, mrc {m['mean_worst_flow_goodput_pps']} pkt/s.\n"]


def report_random_loss(base):
    data = {f: _load(base / f / "random-loss" / "summary.json") for f in FABRICS}
    if not all(data.values()):
        return []
    rows = []
    for pct in data["mrc"]["cases_by_loss_percent"]:
        cells = []
        for fabric in FABRICS:
            c = data[fabric]["cases_by_loss_percent"][pct]
            cells.append(f"{c['goodput_pps']} pkt/s, {c['retransmitted_pct_of_new']}% retransmitted")
        rows.append(f"| {pct}% | " + " | ".join(cells) + " |")
    return ["## Random loss on every link (`random-loss`)\n\n"
            "Uniform netem loss on all leaf-to-spine links, 4,000 pkt/s offered. At this "
            "scale RoCE-style recovery does not collapse; the visible cost is retransmission "
            "amplification (go-back-N resends the window after each loss).\n\n"
            "| Loss | Baseline | mrc |\n|---|---|---|\n" + "\n".join(rows) + "\n"]


def report(profile):
    """Write docs/evidence/<profile>/README.md comparing both fabrics."""
    base = PROJECT / "docs" / "evidence" / profile
    rows, charts = [], []
    for name in FAULTS:
        pair = {}
        for fabric in FABRICS:
            path = base / fabric / name / "summary.json"
            if path.exists():
                pair[fabric] = (path.parent, json.loads(path.read_text()))
        if len(pair) != 2:
            continue
        description = pair["mrc"][1]["description"]

        def cell(summary):
            outage = summary["longest_zero_delivery_ms"]
            text = f"**{summary['goodput_during_fault_pps']} pkt/s** during the fault; "
            if outage < 1000 and summary["goodput_during_fault_pps"] < 0.9 * summary["goodput_before_pps"]:
                text += f"**degraded**, never fully stopped (longest gap {outage} ms)"
            elif not outage:
                text += "no interruption"
            elif summary["recovered_before_repair"]:
                text += f"**nothing delivered for {outage} ms**, then recovered"
            else:
                text += f"**nothing delivered for {outage} ms**, until the repair"
            return text + f"; {summary['retransmissions']} packets retransmitted"

        mrc = pair["mrc"][1]
        extra = []
        if mrc.get("first_ev_retired_ms") is not None:
            extra.append(f"failed EVs removed {mrc['first_ev_retired_ms']} ms after the fault")
        if mrc.get("first_ev_restored_ms") is not None:
            extra.append(f"back in use {mrc['first_ev_restored_ms']} ms after the repair")
        rows.append(f"| `{name}` | {description} | {cell(pair['baseline'][1])} | "
                    f"{cell(mrc)}{'; ' + ', '.join(extra) if extra else ''} |")
        keys, base_series = goodput_series(*pair["baseline"])
        _, mrc_series = goodput_series(pair["mrc"][0], pair["mrc"][1],
                                       after=keys[-1] + 0.05)
        length = min(len(keys), len(base_series), len(mrc_series))
        keys, base_series, mrc_series = keys[:length], base_series[:length], mrc_series[:length]
        labels = ", ".join(f'"{k:+.2f}"' if i % 10 == 0 else '" "'
                           for i, k in enumerate(keys))
        charts.append(
            f"### `{name}`\n\n{description}. Fault at 0 s, repair at "
            f"{pair['mrc'][1]['repair_at'] - pair['mrc'][1]['fault_at']:.1f} s. "
            "Delivered packets per second in 50 ms buckets "
            "(first line: baseline, second line: mrc).\n\n"
            "```mermaid\nxychart-beta\n"
            f'  title "{name}: goodput (pkt/s)"\n'
            f"  x-axis [{labels}]\n"
            '  y-axis "pkt/s" 0 --> 2500\n'
            f"  line [{', '.join(map(str, base_series))}]\n"
            f"  line [{', '.join(map(str, mrc_series[:len(keys)]))}]\n```\n")
    extras = []
    spray_path = base / "mrc" / "spray" / "summary.json"
    if spray_path.exists():
        spray = json.loads(spray_path.read_text())
        hops = "\n".join(f"| {label[2:]} | `{address}` | {'yes' if spray['seen_in_capture'][label] else 'NO'} |"
                         for label, address in spray["expected_outer_destination"].items())
        extras.append(
            "## Spraying and uSID forwarding (`spray`, mrc)\n\n"
            f"One QP sent {spray['sent_new']} packets: per EV "
            + ", ".join(f"`{ev}` {n}" for ev, n in sorted(spray["packets_per_ev"].items()))
            + "; per plane " + ", ".join(f"plane {k}: {v}" for k, v in
                                         sorted(spray["packets_per_plane"].items()))
            + ". Outer destination of the EV "
            "`10111` packets at each capture point (`mrc/spray/*.txt`):\n\n"
            "| Capture point | Outer IPv6 destination | Seen |\n|---|---|---|\n" + hops + "\n")
    rows_ecn = []
    for variant in ("ecn-avoid", "no-ecn-avoid"):
        path = base / "mrc" / f"ecn-{variant}" / "summary.json"
        if path.exists():
            e = json.loads(path.read_text())
            q = e["uplink_queues"]["a1->s11"]
            rows_ecn.append(
                f"| {variant} | {e['flow_a_share_before_competitor']['via s11']:.0%} / "
                f"{e['flow_a_share_with_competitor']['via s11']:.0%} / "
                f"{e['flow_a_share_after_competitor']['via s11']:.0%} | "
                f"{q['ecn_marked']} marked, {q['dropped']} dropped | "
                f"{e['flow_b']['goodput_pps']} pkt/s | "
                f"{e['spurious_loss_declarations']['flow_a']} / "
                f"{e['spurious_loss_declarations']['flow_b']} |")
    if rows_ecn:
        extras.append(
            "## ECN as a load-balancing signal (`ecn`, mrc)\n\n"
            "Both a1 uplinks are shaped to 20 Mbit/s with RED ECN marking. Flow A "
            "(2,400 pkt/s, EVs via s11 and s12) runs for 6 s; flow B (1,400 pkt/s, "
            "pinned to the s11 path) joins after 2 s for 2.5 s.\n\n"
            "| Variant | Flow A share via s11: before / with B / after | a1->s11 queue | "
            "Flow B goodput (offered 1,400) | False loss declarations A / B |\n"
            "|---|---|---|---|---|\n" + "\n".join(rows_ecn) + "\n")
    cm_path = base / "mrc" / "clustermap" / "summary.json"
    if cm_path.exists():
        cm = json.loads(cm_path.read_text())
        rows_cm = "\n".join(
            f"| {t} | `{cm['healthy']['probe_address'][t]}` | {cm['healthy']['probes'][t]} | "
            f"{cm['a1-s11-silent-failure']['probes'][t]} |" for t in cm["healthy"]["probes"])
        extras.append(
            "## Clustermapper self-probes (`clustermap`, mrc)\n\n"
            "| Target | Probe address (to the switch and back to h1) | Healthy | a1-s11 silently failed |\n"
            "|---|---|---|---|\n" + rows_cm + "\n\nDiagnosis: "
            + "; ".join(cm["a1-s11-silent-failure"]["diagnosis"]) + "\n")
    extras.extend(report_bfd(base) + report_collision(base) + report_random_loss(base))
    environment = json.loads((base / "mrc" / "environment.json").read_text())
    text = (
        f"# Evidence: {profile}\n\n"
        "Generated by `python3 src/lab.py report` from the files in this directory "
        "(see [the walkthrough](../../walkthrough.md) for how to read them). "
        f"Kernel `{environment['kernel']}`, collected {environment['collected_at_utc'][:10]}.\n\n"
        "Every fault run sends 2,000 packets/s for several seconds; the fault is "
        "injected 1 s in. Baseline traffic is one RoCE-style single-path QP "
        "(go-back-N) whose ECMP hash was chosen to cross the fault; mrc traffic is "
        "one MRC-style QP spraying over its EV set.\n\n"
        "| Experiment | Fault | Baseline (eBGP + ECMP, single path) | mrc (static SRv6 uSID, MRC endpoint) |\n"
        "|---|---|---|---|\n" + "\n".join(rows) + "\n\n" + "\n".join(extras)
        + "\n## Goodput around each fault\n\n" + "\n".join(charts))
    (base / "README.md").write_text(text)
    print(f"Wrote {base / 'README.md'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["generate", "deploy", "configure", "test",
                                           "experiment", "evidence", "summarize", "report",
                                           "destroy", "preflight"])
    parser.add_argument("name", nargs="?", help="experiment name, or 'all'")
    parser.add_argument("--profile", choices=PROFILES, default="multi-plane-multi-link")
    parser.add_argument("--fabric", choices=FABRICS, default="baseline",
                        help="baseline: eBGP + ECMP; mrc: static SRv6 uSID, no routing protocol")
    args = parser.parse_args()
    select_profile(args.profile, args.fabric)
    if args.action == "generate":
        generate()
    elif args.action in ("deploy", "preflight"):
        if args.action == "deploy":
            generate()
        run("docker", "build", "-q", "-t", IMAGE, str(PROJECT), stdout=subprocess.DEVNULL)
        preflight(args.fabric)
        if args.action == "deploy":
            run("containerlab", "deploy", "-t", str(ROOT / "mrc.clab.yml"))
            configure()
    elif args.action == "configure":
        configure()
    elif args.action == "test":
        test()
    elif args.action == "summarize":
        summarize(PROJECT / "docs" / "evidence" / args.profile)
    elif args.action == "report":
        report(args.profile)
    elif args.action in ("experiment", "evidence"):
        available = applicable_experiments()
        if args.name in (None, "all"):
            names = available
        elif args.name in available:
            names = [args.name]
        else:
            raise SystemExit(f"Experiments for this profile/fabric: {', '.join(available)}")
        if args.action == "evidence":
            evidence = (control_plane_baseline() if FABRIC == "baseline"
                        else control_plane_mrc())
            (ROOT / "results" / "control-plane.json").write_text(
                json.dumps(evidence, indent=2) + "\n")
        run_experiments(names)
        if args.action == "evidence":
            collect_evidence(names)
    else:
        run("containerlab", "destroy", "-t", str(ROOT / "mrc.clab.yml"))


if __name__ == "__main__":
    main()
