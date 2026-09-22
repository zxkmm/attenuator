#!/usr/bin/env python3
"""
Verify (and, if needed, fix) the series<->shunt neck gap for every pi-attenuator
junction on channels 1 and 2 of 6g_att.kicad_pcb.

Why this exists: channel 1 (J1->J6) uses a 0.8mm pad-edge-to-pad-edge neck,
channel 2 (J2->J7) uses 1.2mm. This walks the *actual* netlist topology (same
tracer as place_all_pi.py), measures the *real* pad geometry at every junction
straight from the board file, and flags/fixes any junction whose measured
neck doesn't match its channel's expected value.

This only ever repositions the shunt resistor(s) at a bad junction (never the
touching series chain, never anything in a correct junction, never tracks).
It is fold/jog-agnostic: it measures each junction against its own immediate
series neighbor(s), so it doesn't care how the ladder is folded across the
board.

Usage:
    python3 fix_neck_gaps.py --pcb 6g_att.kicad_pcb            # report only
    python3 fix_neck_gaps.py --pcb 6g_att.kicad_pcb --apply    # fix + apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

PAD_X_HALF = 1.025 / 2   # along-resistor pad half-length (0805)
PAD_Y_HALF = 1.4 / 2     # transverse pad half-width (0805)
PAD_CENTRE = 0.9125      # footprint centre to pad centre (0805)

# Distance from a footprint's own centre to the outer edge of its near pad,
# measured along board X, depending on orientation:
#  - series (rot 0/180): the two pads straddle the centre ALONG X, so the
#    near-pad outer edge is PAD_CENTRE + PAD_X_HALF away (same on both sides).
#  - shunt  (rot +-90): the pads straddle the centre along Y instead, so in X
#    the footprint is just PAD_Y_HALF wide on either side of its centre.
SERIES_EDGE = PAD_CENTRE + PAD_X_HALF
SHUNT_EDGE = PAD_Y_HALF

CHANNELS = [
    {"name": "Channel 1 (J1->J6)", "start_net": "Net-(J1-In)", "expected_neck": 0.8},
    {"name": "Channel 2 (J2->J7)", "start_net": "Net-(J2-In)", "expected_neck": 1.2},
]

TOL = 0.01  # mm


def center_to_edge_x(rot: float) -> float:
    """Distance from a footprint's centre to its near pad's outer edge, along board X."""
    return SERIES_EDGE if round(rot) % 180 == 0 else SHUNT_EDGE


# ── netlist topology tracer (same logic as place_all_pi.py) ─────────────────
TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def parse_sexpr(text: str):
    toks = TOKEN.findall(text)
    pos = 0

    def walk():
        nonlocal pos
        out = []
        pos += 1
        while toks[pos] != ")":
            if toks[pos] == "(":
                out.append(walk())
            else:
                t = toks[pos]
                out.append(t[1:-1] if t.startswith('"') else t)
                pos += 1
        pos += 1
        return out

    return walk()


def children(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def load_topology(netlist_path: str):
    tree = parse_sexpr(open(netlist_path, encoding="utf-8").read())
    comps = {children(c, "ref")[0][1]: children(c, "value")[0][1]
             for c in children(children(tree, "components")[0], "comp")}

    pin_net = {}
    gnd_net = None
    for n in children(children(tree, "nets")[0], "net"):
        name = children(n, "name")[0][1]
        if name.strip("/").upper() == "GND":
            gnd_net = name
        for node in children(n, "node"):
            pin_net[(children(node, "ref")[0][1], children(node, "pin")[0][1])] = name

    resistors = [r for r in comps if re.match(r"^R\d+$", r)]
    r_nets = {r: (pin_net.get((r, "1")), pin_net.get((r, "2"))) for r in resistors}
    direct_gnd = [r for r, (n1, n2) in r_nets.items() if n1 == gnd_net or n2 == gnd_net]

    net_to_r = defaultdict(list)
    for r, (n1, n2) in r_nets.items():
        if n1:
            net_to_r[n1].append(r)
        if n2:
            net_to_r[n2].append(r)

    shunt_branches = []
    visited = set()
    for r in direct_gnd:
        if r in visited:
            continue
        branch = [r]
        visited.add(r)
        n1, n2 = r_nets[r]
        curr_net = n2 if n1 == gnd_net else n1
        while curr_net and curr_net != gnd_net:
            other_rs = [x for x in net_to_r[curr_net] if x not in visited]
            if len(net_to_r[curr_net]) == 2 and len(other_rs) == 1:
                next_r = other_rs[0]
                visited.add(next_r)
                branch.append(next_r)
                rn1, rn2 = r_nets[next_r]
                curr_net = rn2 if rn1 == curr_net else rn1
            else:
                break
        branch.reverse()
        shunt_branches.append((curr_net, branch))

    junction_shunts = defaultdict(list)
    for jnet, br in shunt_branches:
        junction_shunts[jnet].append(br)

    series_resistors = set(resistors) - set(r for _, br in shunt_branches for r in br)
    series_net_to_r = defaultdict(list)
    for r in series_resistors:
        n1, n2 = r_nets[r]
        series_net_to_r[n1].append(r)
        series_net_to_r[n2].append(r)

    def trace_channel(start_jnet):
        chain = []
        cur_jnet = start_jnet
        visited_jnets = set()
        while cur_jnet:
            visited_jnets.add(cur_jnet)
            shs = junction_shunts[cur_jnet]
            next_jnet = None
            series_path = []
            for r in series_net_to_r[cur_jnet]:
                path = [r]
                n1, n2 = r_nets[r]
                curr_n = n2 if n1 == cur_jnet else n1
                while curr_n not in junction_shunts:
                    cands = [x for x in series_net_to_r[curr_n] if x != path[-1] and x not in path]
                    if not cands:
                        break
                    path.append(cands[0])
                    pn1, pn2 = r_nets[cands[0]]
                    curr_n = pn2 if pn1 == curr_n else pn1
                if curr_n in junction_shunts and curr_n not in visited_jnets:
                    next_jnet = curr_n
                    series_path = path
                    break
            chain.append((cur_jnet, shs, series_path))
            cur_jnet = next_jnet
        return chain

    return trace_channel, r_nets


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcb", default="6g_att.kicad_pcb")
    ap.add_argument("--net", default=None, help="netlist path (default: <pcb-basename>.net next to --pcb)")
    ap.add_argument("--apply", action="store_true", help="write fixes via `kh place` (default: report only)")
    args = ap.parse_args()

    pcb_dir = os.path.dirname(os.path.abspath(args.pcb)) or "."
    net_path = args.net or os.path.join(pcb_dir, os.path.splitext(os.path.basename(args.pcb))[0] + ".net")

    import pcbnew  # noqa: E402  (offline SWIG bindings, no live editor needed)
    board = pcbnew.LoadBoard(args.pcb)
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}

    trace_channel, r_nets = load_topology(net_path)

    def pos(ref):
        fp = fps[ref]
        p = fp.GetPosition()
        return p.x / 1e6, p.y / 1e6, fp.GetOrientationDegrees()

    fixes = {}   # ref -> (x, y, rot)
    problems = []

    for chan in CHANNELS:
        chain = trace_channel(chan["start_net"])
        expected = chan["expected_neck"]
        print(f"\n{chan['name']}: {len(chain)} junctions, expected neck = {expected} mm")

        # NOTE: this ladder folds back on itself on the real board (row 2 of a
        # channel runs right-to-left in X while netlist/electrical order still
        # moves forward). So "gap" is computed as an unsigned edge-to-edge
        # distance -- never assume which side is physically left/right.
        def edge_gap(x1, rot1, x2, rot2):
            return abs(x1 - x2) - center_to_edge_x(rot1) - center_to_edge_x(rot2)

        prev_series = None  # series resistors of the previous stage (touching chain, in order)
        for jnet, shs, srs in chain:
            shunt_refs = [r for arm in shs for r in arm]
            if not shunt_refs:
                prev_series = srs
                continue

            jx, jy, jrot = pos(shunt_refs[0])

            right_gap = None
            if srs:
                sx, sy, srot = pos(srs[0])
                right_gap = edge_gap(jx, jrot, sx, srot)

            left_gap = None
            if prev_series:
                px, py, prot = pos(prev_series[-1])
                left_gap = edge_gap(jx, jrot, px, prot)

            # The "neck" that matters for a fix is the OUTGOING gap (shunt ->
            # this stage's series chain) -- that's what `jx` anchors. The left
            # gap (incoming series -> this shunt) is only a corroborating
            # signal: at a fold/turn junction it is legitimately not a normal
            # neck value, so it must never by itself veto or force a fix.
            if right_gap is None or abs(right_gap - expected) <= TOL:
                prev_series = srs
                continue

            label = "/".join(shunt_refs)
            if left_gap is not None and abs(left_gap - expected) <= TOL:
                # Left side is a valid, correct neck -> the shunt is properly
                # anchored there. Moving it to fix the right side would break
                # that correct left side instead of fixing anything -- the
                # OUTGOING series chain is what actually drifted. Flag it,
                # since shifting a touching series chain is a different
                # operation (and out of scope for a pure reposition here).
                problems.append(f"SERIES-DRIFT  junction {label}: right gap={right_gap:.4f} "
                                 f"(want {expected}), left gap OK -- series chain {srs} needs "
                                 f"manual shift, not a shunt fix")
                prev_series = srs
                continue

            # Reposition the shunt(s) along X to restore the outgoing (right) gap,
            # moving away from the series pad on whichever side the shunt is
            # currently on (handles both fold directions).
            sx, sy, srot = pos(srs[0])
            s_half = center_to_edge_x(srot)
            shunt_half = center_to_edge_x(jrot)
            sign = 1 if jx >= sx else -1
            new_jx = round(sx + sign * (expected + s_half + shunt_half), 4)
            delta = round(new_jx - jx, 4)

            for r in shunt_refs:
                rx, ry, rrot = pos(r)
                fixes[r] = (new_jx, ry, rrot)

            note = ""
            if left_gap is not None:
                note = f" (left gap was {left_gap:.4f})"
            print(f"  FIX  {label}: measured neck {right_gap:.4f} mm -> {expected} mm "
                  f"(shift {delta:+.4f} mm on X){note}")

            prev_series = srs

    if problems:
        print("\nNeeds manual attention (not auto-fixed):")
        for p in problems:
            print(f"  - {p}")

    if not fixes:
        print("\nNo neck-gap defects found that this script can auto-fix.")
    else:
        print(f"\n{len(fixes)} footprint(s) to reposition: {sorted(fixes, key=lambda r: int(r[1:]))}")

    if not args.apply:
        print("\n(dry run -- pass --apply to write these via `kh place`)")
        return

    if not fixes:
        return

    json_path = os.path.join(pcb_dir, "_neck_gap_fixes.json")
    with open(json_path, "w") as f:
        json.dump({k: list(v) for k, v in fixes.items()}, f, indent=2)

    cmd = ["kh", "place", "--pcb", pcb_dir, "--json", json_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    os.remove(json_path)
    print("Applied. In KiCad, File -> Revert to reload.")


if __name__ == "__main__":
    main()
