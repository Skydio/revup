from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class ErrorClass(Enum):
    """How a per-field GraphQL error should be handled on retry."""

    # Field wasn't computed because the request was too expensive. Re-transacting
    # just this field (alone or split smaller) can succeed.
    RETRYABLE = "retryable"
    # The intended effect already exists (a prior partial attempt applied it).
    # Treat as success; retrying would fail or duplicate.
    ALREADY_DONE = "already_done"
    # An informational notice (e.g. a deprecation warning) that accompanies an
    # otherwise-successful response. Ignore it entirely.
    INFORMATIONAL = "informational"
    # Won't succeed on retry (not found, no permission, malformed). Surface it.
    TERMINAL = "terminal"


# GraphQL error `type` values, mapped to how we treat the affected field.
# Anything unmapped is treated as TERMINAL (fail loudly rather than silently retry).
_ERROR_CLASS_BY_TYPE: Dict[str, ErrorClass] = {
    "RESOURCE_LIMITS_EXCEEDED": ErrorClass.RETRYABLE,
    # A mutation whose effect already exists (a PR/comment/etc from a partial retry).
    "UNPROCESSABLE": ErrorClass.ALREADY_DONE,
    # A warning that a field/id is deprecated; the response still succeeded.
    "DEPRECATION": ErrorClass.INFORMATIONAL,
}


@dataclass
class GraphqlError:
    """One entry from a GraphQL response `errors` array."""

    message: str
    type: str  # "" if GitHub gave no type (usually a request/validation error)
    path: List[Any]  # alias-first path, e.g. ["pr_out3", "pullRequest"]
    raw: Dict[str, Any]  # the original error object, for surfacing terminal errors

    @property
    def error_class(self) -> ErrorClass:
        return _ERROR_CLASS_BY_TYPE.get(self.type, ErrorClass.TERMINAL)

    @property
    def is_timeout(self) -> bool:
        # GitHub reports execution timeouts as a 200 body error ("We couldn't
        # respond to your request in time") with no distinct type, so match message.
        return "timeout" in self.message.lower() or "in time" in self.message.lower()

    @property
    def alias(self) -> Optional[str]:
        """The field alias this error is anchored to, if any.

        GitHub's path is alias-first for aliased fields (repo-scoped fields are not
        prefixed with `repository`), so the alias is the first `*_out*` path element.
        """
        for part in self.path:
            if isinstance(part, str) and "_out" in part:
                return part
        return None


@dataclass
class GraphqlResponse:
    """A parsed GraphQL response: partial data plus any per-field errors.

    Unlike an exception, this represents a response that may be partially usable —
    `data` holds whatever resolved, `errors` explains what didn't.
    """

    data: Any
    errors: List[GraphqlError] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: Any) -> GraphqlResponse:
        errors = [
            GraphqlError(
                message=e.get("message", ""),
                type=e.get("type", ""),
                path=e.get("path", []),
                raw=e,
            )
            for e in raw.get("errors", [])
        ]
        return cls(data=raw.get("data"), errors=errors)


@dataclass
class SingleQuery:
    """One aliased field in a GraphQL operation, owned and managed by GraphqlQuery."""

    prefix: str
    scope: str  # "repo" | "top" | "mutation"
    field_template: str
    var_types: List[str]
    values: List[Any]
    fragment: str
    index: int

    @property
    def alias(self) -> str:
        return f"{self.prefix}_out{self.index}"

    def var_name(self, var_idx: int) -> str:
        if len(self.var_types) == 1:
            return f"{self.prefix}{self.index}"
        return f"{self.prefix}{self.index}_{var_idx}"

    def render_field(self) -> str:
        var_names = [f"${self.var_name(j)}" for j in range(len(self.var_types))]
        return self.field_template.format(self.alias, *var_names)

    def render_declarations(self) -> List[str]:
        return [f"${self.var_name(j)}: {vtype}" for j, vtype in enumerate(self.var_types)]

    def render_variables(self) -> Dict[str, Any]:
        return {self.var_name(j): val for j, val in enumerate(self.values)}

    def extract(self, result: Any) -> Any:
        if self.scope == "repo":
            return result["data"]["repository"][self.alias]
        return result["data"][self.alias]


class GraphqlQuery:
    """Builds a GraphQL query/mutation from a flat list of aliased fields.

    Fields of different types (prefixes) coexist in one list, so split() can
    divide the list in half regardless of type. GraphqlQuery is the only owner
    of the field objects; callers add fields by value and read results back by
    prefix, never touching the field objects directly.
    """

    def __init__(self, operation: str = "query", name: str = ""):
        self.operation = operation
        self.name = name
        self.fixed_vars: List[Tuple[str, str, Any]] = []
        self.fixed_repo_fields: str = ""
        self.queries: List[SingleQuery] = []
        # Per-prefix counter so each type's aliases are 0, 1, 2, ... and unique.
        self._prefix_counts: Dict[str, int] = {}

    def add_fixed_var(self, name: str, gql_type: str, value: Any) -> None:
        self.fixed_vars.append((name, gql_type, value))

    def add(
        self,
        *,
        prefix: str,
        scope: str,
        field_template: str,
        var_types: List[str],
        values: List[Any],
        fragment: str = "",
    ) -> None:
        """Add a field. Its alias index is fixed here so results stay addressable
        by prefix even after the query is split and its results merged."""
        assert len(values) == len(var_types)
        idx = self._prefix_counts.get(prefix, 0)
        self._prefix_counts[prefix] = idx + 1
        self.queries.append(
            SingleQuery(
                prefix=prefix,
                scope=scope,
                field_template=field_template,
                var_types=list(var_types),
                values=list(values),
                fragment=fragment,
                index=idx,
            )
        )

    def extract(self, result: Any, prefix: str) -> List[Any]:
        """Return the result node for every field with the given prefix, in add order."""
        return [q.extract(result) for q in self.queries if q.prefix == prefix]

    def unfulfilled_aliases(self, result: Any) -> Set[str]:
        """Aliases whose result is null or absent in `result`.

        For a mutation this means the field did not apply (a completed mutation
        returns its payload); for a query it means the field did not resolve.
        """
        missing: Set[str] = set()
        for q in self.queries:
            try:
                node = q.extract(result)
            except (KeyError, TypeError):
                node = None
            if node is None:
                missing.add(q.alias)
        return missing

    def total_items(self) -> int:
        return len(self.queries)

    def build(self) -> Tuple[str, Dict[str, Any]]:
        all_decls: List[str] = []
        variables: Dict[str, Any] = {}

        for name, gql_type, value in self.fixed_vars:
            all_decls.append(f"${name}: {gql_type}")
            variables[name] = value

        for q in self.queries:
            all_decls.extend(q.render_declarations())
            variables.update(q.render_variables())

        decl_str = ", ".join(all_decls)
        name_str = f" {self.name}" if self.name else ""

        repo_fields = self.fixed_repo_fields
        top_fields = ""
        mutation_fields = ""
        for q in self.queries:
            rendered = q.render_field()
            if q.scope == "repo":
                repo_fields += rendered
            elif q.scope == "top":
                top_fields += rendered
            else:
                mutation_fields += rendered

        if self.operation == "query":
            body = ""
            if repo_fields:
                body += f"""
            repository(name: $name, owner: $owner) {{
                {repo_fields}
            }}"""
            body += top_fields
            query_str = f"""
        {self.operation}{name_str} ({decl_str}) {{{body}
        }}"""
        else:
            query_str = f"""
        {self.operation}{name_str} ({decl_str}) {{
            {mutation_fields}
        }}"""

        fragments = ""
        seen: set = set()
        for q in self.queries:
            if q.fragment and q.fragment not in seen:
                fragments += q.fragment
                seen.add(q.fragment)
        query_str += fragments

        return query_str, variables

    def _empty_clone(self) -> GraphqlQuery:
        # type(self) so a subclass survives a split as the same type.
        clone = type(self)(operation=self.operation, name=self.name)
        clone.fixed_vars = list(self.fixed_vars)
        clone.fixed_repo_fields = self.fixed_repo_fields
        return clone

    def _with_fields(self, fields: List[SingleQuery]) -> GraphqlQuery:
        clone = self._empty_clone()
        clone.queries = fields
        return clone

    def split(self) -> Tuple[GraphqlQuery, GraphqlQuery]:
        """Split the flat field list in half.

        Each half may be heterogeneous in field type. Aliases are baked into each
        field, so results from the two halves never collide when merged.
        """
        # Round up so an odd count keeps the extra item on the left (and a lone
        # item lands left with an empty right).
        mid = (len(self.queries) + 1) // 2
        return self._with_fields(self.queries[:mid]), self._with_fields(self.queries[mid:])

    def subset(self, aliases: Set[str]) -> GraphqlQuery:
        """A query containing only the fields whose alias is in `aliases`.

        Fields keep their original alias index, so results merge back without
        collision. Used to re-transact only the fields that failed retryably.
        """
        return self._with_fields([q for q in self.queries if q.alias in aliases])
