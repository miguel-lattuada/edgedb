##
# Copyright (c) 2016 MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##


from edgedb.lang import edgeql
from edgedb.lang.common import ast
from edgedb.lang.edgeql import ast as qlast
from edgedb.lang.graphql import ast as gqlast
from edgedb.lang.graphql import parser as gqlparser


GQL_OPS_MAP = {
    '__eq': ast.ops.EQ, '__ne': ast.ops.NE,
    '__in': ast.ops.IN, '__ni': ast.ops.NOT_IN,
}


class GraphQLTranslator:
    def __init__(self, schema):
        self.schema = schema

    def translate(self, gqltree, variables):
        self._fragments = {
            f.name: f for f in gqltree.definitions
            if isinstance(f, gqlast.FragmentDefinition)
        }

        # create a dict of variables that will be marked as critical or not
        #
        variables = {name: [val, False] for name, val in variables.items()}

        for definition in gqltree.definitions:
            if isinstance(definition, gqlast.OperationDefinition):
                query = self._process_definition(definition, variables)

        # produce the list of variables critical to the shape of the query
        #
        critvars = [(name, val) for name, (val, crit) in variables.items()
                    if crit]
        critvars.sort(key=lambda x: x[0])

        return query, critvars

    def _should_include(self, directives, variables):
        for directive in directives:
            if directive.name in ('include', 'skip'):
                cond = [a.value for a in directive.arguments
                        if a.name == 'if'][0]
                if isinstance(cond, gqlast.Variable):
                    var = variables[cond.value]
                    cond = var[0]
                    var[1] = True  # mark the variable as critical
                else:
                    cond = cond.value

                if directive.name == 'include' and cond is False:
                    return False
                elif directive.name == 'skip' and cond is True:
                    return False
        return True

    def _process_definition(self, definition, variables):
        query = None

        if definition.type is None or definition.type == 'query':
            module = None
            for directive in definition.directives:
                args = {a.name: a.value.value for a in directive.arguments}
                if directive.name == 'edgedb':
                    module = args['module']

            for selset in definition.selection_set.selections:
                selquery = qlast.SelectQueryNode(
                    namespaces=[
                        qlast.NamespaceAliasDeclNode(
                            namespace=module
                        )
                    ],
                    targets=[
                        self._process_selset(selset, variables)
                    ],
                    where=self._process_select_where(selset)
                )

                if query is None:
                    query = selquery
                else:
                    query = qlast.SelectQueryNode(
                        op=qlast.UNION,
                        op_larg=query,
                        op_rarg=selquery
                    )

        else:
            raise ValueError('unsupported definition type: {!r}'.format(
                definition.type))

        return query

    def _process_selset(self, selset, variables):
        concept = selset.name

        expr = qlast.SelectExprNode(
            expr=qlast.PathNode(
                steps=[qlast.PathStepNode(expr=concept)],
                pathspec=self._process_pathspec(
                    [selset.name],
                    selset.selection_set.selections,
                    variables)
            )
        )

        return expr

    def _process_pathspec(self, base, selections, variables):
        pathspec = []

        for sel in selections:
            if not self._should_include(sel.directives, variables):
                continue

            if isinstance(sel, gqlast.Field):
                pathspec.append(self._process_field(base, sel, variables))
            elif isinstance(sel, gqlast.InlineFragment):
                pathspec.extend(self._process_inline_fragment(
                    base, sel, variables))
            elif isinstance(sel, gqlast.FragmentSpread):
                pathspec.extend(self._process_spread(base, sel, variables))

        return pathspec

    def _process_field(self, base, field, variables):
        base = base + [field.name]
        spec = qlast.SelectPathSpecNode(
            expr=qlast.LinkExprNode(
                expr=qlast.LinkNode(
                    name=field.name
                )
            ),
            where=self._process_path_where(base, field.arguments)
        )

        if field.selection_set is not None:
            spec.pathspec = self._process_pathspec(
                base,
                field.selection_set.selections,
                variables)

        return spec

    def _process_inline_fragment(self, base, inline_frag, variables):
        return self._process_pathspec(base,
                                      inline_frag.selection_set.selections,
                                      variables)

    def _process_spread(self, base, spread, variables):
        return self._process_pathspec(
            base,
            self._fragments[spread.name].selection_set.selections,
            variables)

    def _process_select_where(self, selset):
        if not selset.arguments:
            return None

        def get_path_prefix():
            return [qlast.PathStepNode(expr=selset.name)]

        args = [
            qlast.BinOpNode(left=left, op=op, right=right)
            for left, op, right in self._process_arguments(get_path_prefix,
                                                           selset.arguments)]

        return self._join_expressions(args)

    def _process_path_where(self, base, arguments):
        if not arguments:
            return None

        def get_path_prefix():
            prefix = [qlast.PathStepNode(expr=base[0])]
            prefix.extend([qlast.LinkExprNode(expr=qlast.LinkNode(name=name))
                           for name in base[1:]])
            return prefix

        args = [
            qlast.BinOpNode(left=left, op=op, right=right)
            for left, op, right in self._process_arguments(
                get_path_prefix, arguments)]

        return self._join_expressions(args)

    def _process_arguments(self, get_path_prefix, args):
        result = []
        for arg in args:
            if arg.name[-4:] in GQL_OPS_MAP:
                op = GQL_OPS_MAP[arg.name[-4:]]
                name_parts = arg.name[:-4]
            else:
                op = ast.ops.EQ
                name_parts = arg.name

            name = get_path_prefix()
            name.extend([
                qlast.LinkExprNode(expr=qlast.LinkNode(name=part))
                for part in name_parts.split('__')])
            name = qlast.PathNode(steps=name)

            value = self._process_literal(arg.value)

            result.append((name, op, value))

        return result

    def _process_literal(self, literal):
        if isinstance(literal, gqlast.ListLiteral):
            return qlast.SequenceNode(elements=[
                self._process_literal(el) for el in literal.value
            ])
        elif isinstance(literal, gqlast.ObjectLiteral):
            raise Exception(
                "don't know how to translate an Object literal to EdgeQL")
        elif isinstance(literal, gqlast.Variable):
            return qlast.ConstantNode(index=literal.value[1:])
        else:
            return qlast.ConstantNode(value=literal.value)

    def _join_expressions(self, exprs, op=ast.ops.AND):
        if len(exprs) == 1:
            return exprs[0]

        result = qlast.BinOpNode(
            left=exprs[0],
            op=op,
            right=exprs[1]
        )
        for expr in exprs[2:]:
            result = qlast.BinOpNode(
                left=result,
                op=op,
                right=expr
            )

        return result


def translate(schema, graphql, variables=None):
    if variables is None:
        variables = {}
    parser = gqlparser.GraphQLParser()
    gqltree = parser.parse(graphql)
    edgeql_tree, critvars = GraphQLTranslator(schema).translate(gqltree,
                                                                variables)
    code = edgeql.generate_source(edgeql_tree)
    if critvars:
        crit = ['{}={!r}'.format(name, val) for name, val in critvars]
        code = '# critical variables: {}\n{}'.format(', '.join(crit), code)

    return code