##
# Copyright (c) 2008-present MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##

from collections import defaultdict, OrderedDict

from edgedb.lang.common.ordered import OrderedSet


class UnresolvedReferenceError(Exception):
    pass


class CycleError(Exception):
    pass


def sort(graph, return_record=False, root_only=False):
    adj = defaultdict(OrderedSet)
    radj = defaultdict(OrderedSet)

    for item_name, item in graph.items():
        if "merge" in item:
            for merge in item["merge"]:
                if merge in graph:
                    adj[item_name].add(merge)
                    radj[merge].add(item_name)
                else:
                    raise UnresolvedReferenceError(
                        'reference to an undefined item {} in {}'.format(
                            merge, item_name))

        if "deps" in item:
            for dep in item["deps"]:
                if dep in graph:
                    adj[item_name].add(dep)
                    radj[dep].add(item_name)
                else:
                    raise UnresolvedReferenceError(
                        'reference to an undefined item {} in {}'.format(
                            dep, item_name))

    visiting = set()
    visited = set()
    sorted = []

    def visit(item):
        if item in visiting:
            raise CycleError("detected cycle on vertex {!r}".format(item))
        if item not in visited:
            visiting.add(item)
            for n in adj[item]:
                visit(n)
            sorted.append(item)
            visiting.remove(item)
            visited.add(item)

    if root_only:
        items = set(graph) - set(radj)
    else:
        items = graph

    for item in items:
        visit(item)

    if return_record:
        return ((item, graph[item]) for item in sorted)
    else:
        return (graph[item]["item"] for item in sorted)


def normalize(graph, merger, **merger_kwargs):
    merged = OrderedDict()

    for name, item in sort(graph, return_record=True):
        merge = item.get("merge")
        if merge:
            for m in merge:
                merger(item["item"], merged[m], **merger_kwargs)

        merged.setdefault(name, item["item"])

    return merged.values()