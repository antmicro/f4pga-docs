#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2019-2022 F4PGA Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""
This script provides a temporary solution for the problem of inout top level
port representation of the BLIF format.

The script loads the design from a JSON file generated by Yosys. Then it splits
all inout ports along with their nets and connections to cell ports into two.
Suffixes are automatically added to distinguish between the input and the
output part.

The JSON design format used by Yosys is documented there:
- http://www.clifford.at/yosys/cmd_write_json.html
- http://www.clifford.at/yosys/cmd_read_json.html

For example in the given design (in verilog):

 module top(
  input  A,
  output B,
  inout  C
 );

  IOBUF iobuf (
   .I(A),
   .O(B),
   .IO_$inp(C),
   .IO_$out(C)
  );

 endmodule

the resulting design would be:

 module top(
  input  A,
  output B,
  input  C_$inp,
  output C_$out
 );

  IOBUF iobuf (
   .I(A),
   .O(B),
   .IO_$inp(C_$inp),
   .IO_$out(C_$out)
  );

 endmodule

"""


from pathlib import Path
from os.path import splitext
import simplejson as json
from argparse import ArgumentParser, RawDescriptionHelpFormatter


def find_top_module(design):
    """
    Looks for the top-level module in the design. Returns its name. Throws
    an exception if none was found.
    """
    for name, module in design["modules"].items():
        attrs = module["attributes"]
        if "top" in attrs and int(attrs["top"]) == 1:
            return name
    raise RuntimeError("No top-level module found in the design!")


def get_nets(bits):
    """
    Returns a set of numbers corresponding to net indices effectively skipping
    connections to consts ("0", "1", "x").

    >>> get_nets([0, 1, 2, "0", "1", "x", 3, 4, 5])
    {0, 1, 2, 3, 4, 5}
    """
    return set([n for n in bits if isinstance(n, int)])


def get_free_net(nets):
    """
    Given a set of used net indices, returns a new, free index.

    >>> get_free_net({0, 1, 2,    4, 5, 6})
    3
    >>> get_free_net({0, 1, 2, 3, 4, 5, 6})
    7
    """
    sorted_nets = sorted(list(nets))

    # Find a gap in the sequence
    for i in range(len(nets) - 1):
        n0 = sorted_nets[i]
        n1 = sorted_nets[i + 1]
        if n1 != (n0 + 1):
            return n0 + 1

    # No gap was found, return max + 1.
    return sorted_nets[-1] + 1


def main(input: str, output: str = None):
    if output is None:
        output = splitext(input)[0] + "_out.json"

    with Path(input).open("r") as fp:
        design = json.load(fp)

    module_name = find_top_module(design)

    # Take a module from the design and split all of its inout ports into pairs of inputs and outputs.
    # Newly created ports are given suffixed.
    # For example an inout port named "A" is going to be replaced by a pair consisting of "A_$inp" and "A_$out" ports.

    # The function also looks for "netnames" that correspond to inout ports being split.
    # These ones are removed and replaced with new ones related to newly added input and output ports.

    # If any other "netname" mentions a net index connected to a former inout port, then the index is removed from the
    # "netname" (replaced by "x").
    # If there are only "x" left in the "netname", then it is removed.

    # The function returns port name map and net index map.
    # The port map is a list of pairs (input name, input/output name).
    # There are two entries per an inout.
    # The net map is a dict indexed by indices of nets associated with inout ports.
    # Each item contains a dict like {"i": int, "o": int} with indices of the inout net split products.

    # Get the module
    module = design["modules"][module_name]

    # Find indices of all used nets
    nets = set()
    for port in module["ports"].values():
        nets |= get_nets(port["bits"])

    for netname in module["netnames"].values():
        nets |= get_nets(netname["bits"])

    for cell in module["cells"].values():
        for connection in cell["connections"].values():
            nets |= get_nets(connection)

    # Get all inout ports
    inouts = {k: v for k, v in module["ports"].items() if v["direction"] == "inout"}

    # Split ports
    new_ports = {}
    net_map = {}
    port_map = []
    for name, port in inouts.items():
        # Remove the inout port from the module
        del module["ports"][name]
        nets -= get_nets(port["bits"])

        # Make an input and output port
        for dir in ["input", "output"]:
            new_name = name + "_$" + dir[:3]
            new_port = {"direction": dir, "bits": []}

            print("Mapping port '{}' to '{}'".format(name, new_name))

            for n in port["bits"]:
                if isinstance(n, int):
                    mapped_n = get_free_net(nets)
                    print("Mapping net {} to {} ({})".format(n, mapped_n, dir))

                    if n not in net_map:
                        net_map[n] = {}
                    net_map[n][dir[0]] = mapped_n
                    nets.add(mapped_n)

                    new_port["bits"].append(mapped_n)
                else:
                    new_port["bits"].append(n)

            port_map.append(
                (
                    name,
                    new_name,
                )
            )
            new_ports[new_name] = new_port

    # Add inputs and outputs
    module["ports"].update(new_ports)

    netnames = module["netnames"]

    # Remove netnames related to inout ports
    for name, net in list(netnames.items()):
        if name in inouts:
            print(f"Removing netname '{name}'")
            del netnames[name]

    # Remove remapped nets
    for name, net in list(netnames.items()):
        # Remove "bits" used by the net that were re-mapped.
        if len(set(net["bits"]) & set(net_map.keys())):
            # Remove
            net["bits"] = ["x" if b in net_map else b for b in net["bits"]]

            # If there is nothing left, remove the whole net.
            if all([b == "x" for b in net["bits"]]):
                print(f"Removing netname '{name}'")
                del netnames[name]

    # Add netnames related to new input and output ports
    for name, port in new_ports.items():
        netnames[name] = {"hide_name": 0, "bits": port["bits"], "attributes": {}}

    # Remap cell connections that mention inout ports being split.
    # Loop over all cells and their ports.
    # If a port contains a connection to an inout net, then the connection is remapped according to the given
    # net_map.
    # Only ports which names ends on "_$inp" and "_$out" are affected.

    module = design["modules"][module_name]
    cells = module["cells"]

    # Process cells
    for name, cell in cells.items():
        if "port_directions" not in cell:
            continue

        port_directions = cell["port_directions"]
        connections = cell["connections"]

        # Process cell connections
        for port_name, port_nets in list(connections.items()):
            # Skip if no net of this connection were remapped
            if len(set(net_map.keys()) & set(port_nets)) == 0:
                continue

            # Remove connections to the output net from input port and vice
            # versa.
            for dir in ["input", "output"]:
                if port_directions[port_name] == dir and port_name.endswith("$" + dir[:3]):
                    for i, n in enumerate(port_nets):
                        if n in net_map:
                            mapped_n = net_map[n][dir[0]]
                            port_nets[i] = mapped_n
                            print("Mapping connection {}.{}[{}] from {} to {}".format(name, port_name, i, n, mapped_n))

    with Path(output).open("w") as fp:
        json.dump(design, fp, sort_keys=True, indent=2)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__, formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument("-i", required=True, type=str, help="Input JSON")
    parser.add_argument("-o", default=None, type=str, help="Output JSON")
    args = parser.parse_args()
    main(args.i, args.o)
