##
# Copyright (c) 2008-present MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##
"""EdgeQL set compilation functions."""


import typing

from edgedb.lang.common import parsing

from edgedb.lang.ir import ast as irast
from edgedb.lang.ir import utils as irutils

from edgedb.lang.schema import concepts as s_concepts
from edgedb.lang.schema import nodes as s_nodes
from edgedb.lang.schema import objects as s_obj
from edgedb.lang.schema import pointers as s_pointers
from edgedb.lang.schema import sources as s_sources
from edgedb.lang.schema import views as s_views

from edgedb.lang.edgeql import ast as qlast
from edgedb.lang.edgeql import errors

from . import context
from . import dispatch
from . import pathctx
from . import schemactx
from . import stmtctx


PtrDir = s_pointers.PointerDirection


def compile_path(expr: qlast.Path, *, ctx: context.ContextLevel) -> irast.Set:
    pathvars = ctx.pathvars
    anchors = ctx.anchors

    path_tip = None

    if expr.partial:
        if ctx.result_path_steps:
            expr.steps = ctx.result_path_steps + expr.steps
        else:
            raise errors.EdgeQLError('could not resolve partial path ',
                                     context=expr.context)

    for i, step in enumerate(expr.steps):
        if isinstance(step, qlast.ClassRef):
            if i > 0:
                raise RuntimeError(
                    'unexpected ClassRef as a non-first path item')

            refnode = None

            if not step.module:
                # Check if the starting path label is a known anchor
                refnode = anchors.get(step.name)

            if refnode is None:
                # Check if the starting path label is a known
                # path variable (defined in a WITH clause).
                refnode = pathvars.get(step.name)

            if refnode is None:
                # Finally, check if the starting path label is
                # a query defined as a view.
                if path_tip is not None:
                    src_path_id = path_tip.path_id
                else:
                    src_path_id = None

                if not step.module:
                    refnode = ctx.substmts.get((step.name, src_path_id))

                if refnode is None:
                    schema_name = schemactx.resolve_schema_name(
                        step.name, step.module, ctx=ctx)
                    refnode = ctx.substmts.get((schema_name, src_path_id))

            if refnode is not None:
                path_tip = refnode
                continue

        if isinstance(step, qlast.ClassRef):
            # Starting path label.  Must be a valid reference to an
            # existing Concept class, as aliases and path variables
            # have been checked above.
            scls = schemactx.get_schema_object(step, ctx=ctx)
            if isinstance(scls, s_views.View):
                path_tip = stmtctx.declare_view_from_schema(scls, ctx=ctx)
            else:
                path_id = irast.PathId([scls])

                try:
                    # We maintain a registry of Set nodes for each unique
                    # Path to achieve path prefix matching.
                    path_tip = ctx.sets[path_id]
                except KeyError:
                    path_tip = class_set(scls, ctx=ctx)
                    ctx.sets[path_id] = path_tip

        elif isinstance(step, qlast.Ptr):
            # Pointer traversal step
            ptr_expr = step
            ptr_target = None

            direction = (ptr_expr.direction or
                         s_pointers.PointerDirection.Outbound)
            if ptr_expr.target:
                # ... link [IS Target]
                ptr_target = schemactx.get_schema_object(
                    ptr_expr.target, ctx=ctx)
                if not isinstance(ptr_target, s_concepts.Concept):
                    raise errors.EdgeQLError(
                        f'invalid type filter operand: {ptr_target.name} '
                        f'is not a concept',
                        context=ptr_expr.target.context)

            ptr_name = (ptr_expr.ptr.module, ptr_expr.ptr.name)

            if ptr_expr.type == 'property':
                # Link property reference; the source is the
                # link immediately preceding this step in the path.
                source = path_tip.rptr.ptrcls
            else:
                source = path_tip.scls

            path_tip, _ = path_step(
                path_tip, source, ptr_name, direction, ptr_target,
                source_context=step.context, ctx=ctx)

        else:
            # Arbitrary expression
            if i > 0:
                raise RuntimeError(
                    'unexpected expression as a non-first path item')

            expr = dispatch.compile(step, ctx=ctx)
            if isinstance(expr, irast.Set):
                path_tip = expr
            else:
                path_tip = generated_set(expr, ctx=ctx)

    if isinstance(path_tip, irast.Set):
        pathctx.register_path_scope(path_tip.path_id, ctx=ctx)

    return path_tip


def path_step(
        path_tip: irast.Set, source: s_sources.Source,
        ptr_name: typing.Tuple[str, str],
        direction: PtrDir,
        ptr_target: s_nodes.Node,
        source_context: parsing.ParserContext, *,
        ctx: context.ContextLevel) \
        -> typing.Tuple[irast.Set, s_pointers.Pointer]:

    if isinstance(source, s_obj.Tuple):
        if ptr_name[0] is not None:
            el_name = '::'.join(ptr_name)
        else:
            el_name = ptr_name[1]

        if el_name in source.element_types:
            try:
                expr = ctx.sets[path_tip, el_name]
            except KeyError:
                path_id = irutils.tuple_indirection_path_id(
                    path_tip.path_id, el_name,
                    source.element_types[el_name])
                expr = irast.TupleIndirection(
                    expr=path_tip, name=el_name, path_id=path_id,
                    context=source_context)
            else:
                return expr, None
        else:
            raise errors.EdgeQLReferenceError(
                f'{el_name} is not a member of a struct')

        tuple_ind = generated_set(expr, ctx=ctx)
        ctx.sets[path_tip, el_name] = tuple_ind

        return tuple_ind, None

    else:
        # Check if the tip of the path has an associated shape.
        # This would be the case for paths on views.
        ptrcls = None
        shape_el = None
        view_source = None
        view_set = None

        if irutils.is_view_set(path_tip):
            view_set = irutils.get_subquery_shape(path_tip)

        if view_set is None:
            view_set = path_tip

        # Search for the pointer in the shape associated with
        # the tip of the path, i.e. a view.
        for shape_el in view_set.shape:
            shape_ptrcls = shape_el.rptr.ptrcls
            shape_pn = shape_ptrcls.shortname

            if ((ptr_name[0] and ptr_name == shape_pn.as_tuple()) or
                    ptr_name[1] == shape_pn.name):
                # Found a match!
                ptrcls = shape_ptrcls
                if shape_el.expr is not None:
                    view_source = shape_el
                break

        if ptrcls is None:
            # Try to resolve a pointer using the schema.
            ptrcls = resolve_ptr(
                source, ptr_name, direction, target=ptr_target, ctx=ctx)

        target = ptrcls.get_far_endpoint(direction)
        target_path_id = path_tip.path_id.extend(
            ptrcls, direction, target)

        if (view_source is None or shape_el.path_id != target_path_id or
                path_tip.expr is not None):
            path_tip = irutils.get_canonical_set(path_tip)
            path_tip = extend_path(
                path_tip, ptrcls, direction, target, ctx=ctx)

            path_tip.view_source = view_source
        else:
            path_tip = shape_el
            pathctx.register_path_scope(path_tip.path_id, ctx=ctx)

        if (isinstance(target, s_concepts.Concept) and
                target.is_virtual and
                ptr_target is not None):
            try:
                path_tip = ctx.sets[path_tip.path_id, ptr_target.name]
            except KeyError:
                pf = irast.TypeFilter(
                    path_id=path_tip.path_id,
                    expr=path_tip,
                    type=irast.TypeRef(maintype=ptr_target.name)
                )

                new_path_tip = generated_set(pf, ctx=ctx)
                new_path_tip.rptr = path_tip.rptr
                path_tip = new_path_tip
                ctx.sets[path_tip.path_id, ptr_target.name] = path_tip

        return path_tip, ptrcls


def resolve_ptr(
        near_endpoint: irast.Set,
        ptr_name: typing.Tuple[str, str],
        direction: s_pointers.PointerDirection,
        target: typing.Optional[s_nodes.Node]=None, *,
        ctx: context.ContextLevel) -> s_pointers.Pointer:
    ptr_module, ptr_nqname = ptr_name

    if ptr_module:
        pointer = schemactx.get_schema_object(
            name=ptr_nqname, module=ptr_module, ctx=ctx)
        pointer_name = pointer.name
    else:
        pointer_name = ptr_nqname

    ptr = None

    if isinstance(near_endpoint, s_sources.Source):
        ptr = near_endpoint.resolve_pointer(
            ctx.schema,
            pointer_name,
            direction=direction,
            look_in_children=False,
            include_inherited=True,
            far_endpoint=target)
    else:
        if direction == s_pointers.PointerDirection.Outbound:
            bptr = schemactx.get_schema_object(pointer_name, ctx=ctx)
            schema_cls = ctx.schema.get('schema::Atom')
            if bptr.shortname == 'std::__class__':
                ptr = bptr.derive(ctx.schema, near_endpoint, schema_cls)

    if not ptr:
        msg = ('({near_endpoint}).{direction}({ptr_name}{far_endpoint}) '
               'does not resolve to any known path')
        far_endpoint_str = ' TO {}'.format(target.name) if target else ''
        msg = msg.format(
            near_endpoint=near_endpoint.name,
            direction=direction,
            ptr_name=pointer_name,
            far_endpoint=far_endpoint_str)
        raise errors.EdgeQLReferenceError(msg)

    return ptr


def extend_path(
        source_set: irast.Set,
        ptrcls: s_pointers.Pointer,
        direction: PtrDir=PtrDir.Outbound,
        target: typing.Optional[s_nodes.Node]=None, *,
        ctx: context.ContextLevel) -> irast.Set:
    """Return a Set node representing the new path tip."""
    if target is None:
        target = ptrcls.get_far_endpoint(direction)

    path_id = source_set.path_id.extend(ptrcls, direction, target)

    if not source_set.expr or irutils.is_strictly_view_set(source_set):
        target_set = ctx.sets.get(path_id)
    else:
        target_set = None

    if target_set is None:
        target_set = ctx.sets[path_id] = irast.Set()
        target_set.scls = target
        target_set.path_id = path_id

        ptr = irast.Pointer(
            source=source_set,
            target=target_set,
            ptrcls=ptrcls,
            direction=direction
        )

        target_set.rptr = ptr

        pathctx.register_path_scope(target_set.path_id, ctx=ctx)

    return target_set


def class_set(
        scls: s_nodes.Node, *, ctx: context.ContextLevel) -> irast.Set:
    path_id = irast.PathId([scls])
    ir_set = irast.Set(path_id=path_id, scls=scls)
    pathctx.register_path_scope(ir_set.path_id, ctx=ctx)
    return ir_set


def generated_set(
        expr: irast.Base, path_id: typing.Optional[irast.PathId]=None, *,
        ctx: context.ContextLevel) -> irast.Set:
    alias = ctx.aliases.get('expr')
    return irutils.new_expression_set(expr, ctx.schema, path_id, alias=alias)


def ensure_set(expr: irast.Base, *, ctx: context.ContextLevel) -> irast.Set:
    if not isinstance(expr, irast.Set):
        expr = generated_set(expr, ctx=ctx)
    return expr


def ensure_stmt(expr: irast.Base, *, ctx: context.ContextLevel) -> irast.Stmt:
    if not isinstance(expr, irast.Stmt):
        expr = irast.SelectStmt(
            result=ensure_set(expr, ctx=ctx),
            path_scope=ctx.path_scope
        )
        expr.specific_path_scope = {
            ctx.sets[p] for p in ctx.stmt_path_scope
            if p in ctx.sets
        }
    return expr