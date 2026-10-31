"""Algorithmic mapping between entropy values (EVs) and SRv6 uSID paths.

Mirrors section 2.3 of the OpenAI/Microsoft MRC paper at lab scale: switch
uSIDs are allocated from the network structure, so an EV is a compact encoding
of the choices that differ between paths (plane, NIC port, spine) and the
sender rebuilds the destination address from it without per-path state.

uSIDs are 16-bit and readable in packet captures (one hex digit per field):

    source leaf a<plane>, port <link>   0xa<plane>0<link>   a1 port 1 -> a101
    dest leaf   b<plane>, port <link>   0xb<plane>0<link>   b2 port 1 -> b201
    spine s<plane><n>                   0x5<plane><n>0      s12      -> 5120
    host h<i> port (plane, link)        0xe<i><plane><link> h2 p1 l1 -> e211

EV layout (32 bits): [31:16] QP salt, [11:8] plane, [7:4] port link, [3:0] spine.
Like MRC, the EV is striped across the inner UDP source port (low 16 bits)
and IPv6 flow label (high 16 bits).
"""
import ipaddress

BLOCK = "fcbb:0"
BLOCK_BYTES = 4
LEAF_SIDE = {1: "a", 2: "b"}  # host index -> attached leaf letter


def leaf_usid(side, plane, link):
    return {"a": 0xA000, "b": 0xB000}[side] | plane << 8 | link


def spine_usid(plane, spine):
    return 0x5000 | plane << 8 | spine << 4


def host_usid(host, plane, link):
    return 0xE000 | host << 8 | plane << 4 | link


def usid_address(usids):
    if not 1 <= len(usids) <= 6:
        raise ValueError("A /32 block carries at most six 16-bit uSIDs")
    text = BLOCK + ":" + ":".join(f"{u:x}" for u in usids)
    return str(ipaddress.IPv6Address(text + ("::" if len(usids) < 6 else "")))


def usid_prefix(usid):
    return str(ipaddress.IPv6Network(f"{BLOCK}:{usid:x}::/48"))


def usid_shift(address):
    """uN with NEXT-C-SID: drop the active uSID and pad with zeros."""
    packed = ipaddress.IPv6Address(address).packed
    return str(ipaddress.IPv6Address(
        packed[:BLOCK_BYTES] + packed[BLOCK_BYTES + 2:] + b"\0\0"))


def active_usid(address):
    packed = ipaddress.IPv6Address(address).packed
    return int.from_bytes(packed[BLOCK_BYTES:BLOCK_BYTES + 2], "big")


def make_ev(plane, link, spine, salt=0):
    for name, value in (("plane", plane), ("link", link), ("spine", spine)):
        if not 1 <= value <= 15:
            raise ValueError(f"{name} must be 1..15")
    return (salt & 0xFFFF) << 16 | plane << 8 | link << 4 | spine


def ev_fields(ev):
    """Return (plane, link, spine) encoded in an SRv6-mode EV."""
    return (ev >> 8) & 0xF, (ev >> 4) & 0xF, ev & 0xF


def ev_address(ev, src_host, dst_host):
    """Destination address for an EV: src leaf -> spine -> dst leaf -> dst port."""
    plane, link, spine = ev_fields(ev)
    return usid_address([
        leaf_usid(LEAF_SIDE[src_host], plane, link),
        spine_usid(plane, spine),
        leaf_usid(LEAF_SIDE[dst_host], plane, link),
        host_usid(dst_host, plane, link),
    ])


def self_probe_address(host, plane, link, spine=None):
    """Clustermapper-style probe: to the T0 (and optionally a T1) and back."""
    leaf = leaf_usid(LEAF_SIDE[host], plane, link)
    hops = [leaf] if spine is None else [leaf, spine_usid(plane, spine), leaf]
    return usid_address(hops + [host_usid(host, plane, link)])


def port_index(plane, link, links_per_plane):
    return (plane - 1) * links_per_plane + (link - 1)
