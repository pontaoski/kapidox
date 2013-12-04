#!/usr/bin/env python
# encoding: utf-8
import argparse
from contextlib import contextmanager
import itertools
import os
import re
import shutil
import sys
import tempfile

import yapgvb

DESCRIPTION = """\
"""


TIER_ATTRS = dict(style="dashed")
OTHER_ATTRS = TIER_ATTRS
FW_ATTRS = dict(style="filled", color="lightgrey")


def preprocess(fname):
    lst = []
    gfx = yapgvb.Graph().read(fname)
    txt = open(fname).read()
    targets = []
    for node in gfx.nodes:
        label = node.label.replace("KF5::", "")
        if node.shape == Framework.TARGET_SHAPE:
            targets.append(label)
        txt = txt.replace('"' + node.name + '"', '"' + label + '"')

    # Sometimes cmake will generate an entry for the target alias, something
    # like this:
    #
    # "node9" [ label="KParts" shape="polygon"];
    # ...
    # "node15" [ label="KF5::KParts" shape="ellipse"];
    # ...
    #
    # After our node renaming, this ends up with a second "KParts" node
    # definition, which we need to get rid of.
    for target in targets:
        rx = r' *"' + target + '".*label="KF5::' + target + '".*shape="ellipse".*;'
        txt = re.sub(rx, '', txt)
    return txt


def to_temp_file(dirname, fname, content):
    tier = fname.split("/")[-3]
    path = os.path.join(dirname, tier + "-" + os.path.basename(fname))
    if os.path.exists(path):
        raise Exception("{} already exists".format(path))
    open(path, "w").write(content)
    return path


class Framework(object):
    TARGET_SHAPE = "polygon"
    DEPS_SHAPE = "ellipse"

    def __init__(self, fname, options):
        self.options = options

        lst = os.path.basename(fname).split("-")
        self.tier = lst[0]
        self.name = lst[1].replace(".dot", "")

        # Target names
        self.targets = set([])

        # lists of (tail, head) tuples
        self.edges = []

        src = yapgvb.Graph().read(fname)

        for node in src.nodes:
            if node.shape == self.TARGET_SHAPE:
                self.targets.add(node.name)
        for edge in src.edges:
            if self.want(edge.head) and self.want(edge.tail):
                self.edges.append((edge.tail.name, edge.head.name))

    def want(self, node):
        if node.shape not in (Framework.TARGET_SHAPE, Framework.DEPS_SHAPE):
            return False
        name = node.name
        if not name[0].isupper():
            return False
        if "test" in name:
            return False
        if not self.options.qt and name.startswith("Qt"):
            return False
        if name.endswith("Test") or name.endswith("Tests"):
            return False
        if name.startswith("Phonon"):
            return False
        if name.startswith("LibAttica"):
            return False
        return True


def find_not_target_nodes(frameworks):
    nodes = set([])
    targets = set([])
    for fw in frameworks:
        targets.update(fw.targets)
        nodes.update([x[1] for x in fw.edges])
    return nodes.difference(targets)


class Block(object):
    INDENT_SIZE = 4
    def __init__(self, out, depth = 0):
        self.out = out
        self.depth = depth

    def writeln(self, text):
        self.out.write(self.depth * DotWriter.INDENT_SIZE * " " + text + "\n")

    def write_attrs(self, **attrs):
        for key, value in attrs.items():
            self.writeln("{} = {};".format(key, value))

    def write_nodes(self, nodes):
        for node in sorted(nodes):
            self.writeln('"{}" [ label="{}" ];'.format(node, node))

    @contextmanager
    def block(self, opener, closer, **attrs):
        self.writeln(opener)
        block = Block(self.out, depth=self.depth + 1)
        block.write_attrs(**attrs)
        yield block
        self.writeln(closer)

    def square_block(self, prefix, **attrs):
        return self.block(prefix + " [", "]", **attrs)

    def curly_block(self, prefix, **attrs):
        return self.block(prefix + " {", "}", **attrs)

    def cluster_block(self, title, **attrs):
        return self.curly_block("subgraph cluster_" + title, label=title, **attrs)


class DotWriter(Block):
    def __init__(self, frameworks, out):
        Block.__init__(self, out)
        self.frameworks = frameworks

    def write(self):
        with self.curly_block("digraph Root") as root:
            with root.square_block("node") as b:
                b.writeln("fontsize = 12")
                b.writeln("shape = box")

            other_nodes = find_not_target_nodes(self.frameworks)
            qt_nodes = set([x for x in other_nodes if x.startswith("Qt")])
            other_nodes.difference_update(qt_nodes)

            if qt_nodes:
                with root.cluster_block("Qt", **OTHER_ATTRS) as b:
                    b.write_nodes(qt_nodes)

            if other_nodes:
                with root.cluster_block("Others", **OTHER_ATTRS) as b:
                    b.write_nodes(other_nodes)

            for tier, frameworks in itertools.groupby(self.frameworks, lambda x: x.tier):
                with root.cluster_block(tier, **TIER_ATTRS) as tier_block:
                    for fw in frameworks:
                        self.write_framework(tier_block, fw)

    def write_framework(self, tier_block, fw):
        with tier_block.cluster_block(fw.name, **FW_ATTRS) as fw_block:
            fw_block.write_nodes(fw.targets)
            for edge in sorted(fw.edges, key=lambda x:x[1]):
                fw_block.writeln('"{}" -> "{}";'.format(edge[0], edge[1]))


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)

    parser.add_argument("-o", "--output", dest="output", default="-",
        help="Output to FILE", metavar="FILE")

    parser.add_argument("--qt", dest="qt", action="store_true",
        help="Show Qt libraries")

    parser.add_argument("dot_files", nargs="+")

    args = parser.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="kf5dot")
    try:
        input_files = [to_temp_file(tmpdir, x, preprocess(x)) for x in args.dot_files]
        frameworks = [Framework(x, args) for x in input_files]
    finally:
        shutil.rmtree(tmpdir)

    if args.output == "-":
        out = sys.stdout
    else:
        out = open(args.output, "w")
    writer = DotWriter(frameworks, out)
    writer.write()

    return 0


if __name__ == "__main__":
    sys.exit(main())
# vi: ts=4 sw=4 et
