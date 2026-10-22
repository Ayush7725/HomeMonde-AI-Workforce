"""Safe expression evaluator for metric formulas, rule conditions and the AI's calculate tool.

Only numbers, names, arithmetic, comparisons and boolean logic are allowed. No attribute access,
calls, subscripts or comprehensions, so tenant config and model input can never execute code."""
import ast
import math
import operator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.FloorDiv: operator.floordiv}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCS = {"abs": abs, "min": min, "max": max, "round": round}
MAX_LEN = 500


class ExprError(ValueError):
    pass


def parse(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise ExprError("empty expression")
    if len(expr) > MAX_LEN:
        raise ExprError("expression too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"invalid expression: {e.msg}") from None
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Name, ast.Load,
                               ast.Constant, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, *_BIN, *_CMP))
        if isinstance(node, ast.Call):
            ok = isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            ok = False
        if not ok:
            raise ExprError(f"unsupported syntax: {type(node).__name__}")
    return tree


def names(expr: str) -> set[str]:
    return {n.id for n in ast.walk(parse(expr)) if isinstance(n, ast.Name) and n.id not in _FUNCS}


def evaluate(expr: str, values: dict[str, float | None]):
    """Evaluate. A missing or None input yields None (unknown), never a guessed value."""
    tree = parse(expr)

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            v = values.get(n.id)
            if v is None:
                raise _Unknown
            return v
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else (+v if isinstance(n.op, ast.UAdd) else not v)
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)) and b == 0:
                raise _Unknown
            if isinstance(n.op, ast.Pow) and abs(b) > 10:
                raise ExprError("exponent too large")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.BoolOp):
            # Three-valued: "unknown and false" is false and "unknown or true" is true, as the known part decides it;
            # otherwise an unknown part leaves the whole unknown.
            is_and, unknown = isinstance(n.op, ast.And), False
            for part in n.values:
                try:
                    v = bool(ev(part))
                except _Unknown:
                    unknown = True
                    continue
                if v is not is_and:
                    return v
            if unknown:
                raise _Unknown
            return is_and
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, comp in zip(n.ops, n.comparators):
                right = ev(comp)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.Call):
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ExprError("unsupported")

    try:
        out = ev(tree)
    except _Unknown:
        return None
    if isinstance(out, float) and (math.isnan(out) or math.isinf(out)):
        return None
    return out


class _Unknown(Exception):
    pass
