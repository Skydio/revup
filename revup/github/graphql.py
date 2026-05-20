from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class SingleQuery:
    """A single aliased field in a GraphQL operation.

    Each query owns its field_template, variable values and a per-prefix index
    that fixes its alias. Because the alias is stable, a GraphqlQuery can freely
    move a SingleQuery between split halves without result aliases colliding.
    """

    prefix: str
    scope: str  # "repo" | "top" | "mutation"
    field_template: str
    var_types: List[str]
    values: List[Any]
    fragment: str = ""
    index: int = 0

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
    """Builds a GraphQL query/mutation from a flat list of SingleQuery objects.

    Queries of different types (prefixes) coexist in one list, so split() can
    divide the list in half regardless of type, unlike a per-type batch which
    could only be split within a single type.
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
    ) -> SingleQuery:
        assert len(values) == len(var_types)
        idx = self._prefix_counts.get(prefix, 0)
        self._prefix_counts[prefix] = idx + 1
        sq = SingleQuery(
            prefix=prefix,
            scope=scope,
            field_template=field_template,
            var_types=list(var_types),
            values=list(values),
            fragment=fragment,
            index=idx,
        )
        self.queries.append(sq)
        return sq

    def total_items(self) -> int:
        return len(self.queries)

    def build(self) -> Tuple[str, Dict[str, Any]]:
        all_decls: List[str] = []
        variables: Dict[str, Any] = {}

        for name, gql_type, value in self.fixed_vars:
            all_decls.append(f"${name}: {gql_type}")
            variables[name] = value

        for sq in self.queries:
            all_decls.extend(sq.render_declarations())
            variables.update(sq.render_variables())

        decl_str = ", ".join(all_decls)
        name_str = f" {self.name}" if self.name else ""

        repo_fields = self.fixed_repo_fields
        top_fields = ""
        mutation_fields = ""
        for sq in self.queries:
            rendered = sq.render_field()
            if sq.scope == "repo":
                repo_fields += rendered
            elif sq.scope == "top":
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
        for sq in self.queries:
            if sq.fragment and sq.fragment not in seen:
                fragments += sq.fragment
                seen.add(sq.fragment)
        query_str += fragments

        return query_str, variables

    def _empty_clone(self) -> GraphqlQuery:
        clone = GraphqlQuery(operation=self.operation, name=self.name)
        clone.fixed_vars = list(self.fixed_vars)
        clone.fixed_repo_fields = self.fixed_repo_fields
        return clone

    def split(self) -> Tuple[GraphqlQuery, GraphqlQuery]:
        """Split the flat query list in half.

        Each half may be heterogeneous in query type. Aliases are baked into each
        SingleQuery, so results from the two halves never collide when merged.
        """
        # Round up so an odd count keeps the extra item on the left (and a lone
        # item lands left with an empty right).
        mid = (len(self.queries) + 1) // 2
        left = self._empty_clone()
        right = self._empty_clone()
        left.queries = self.queries[:mid]
        right.queries = self.queries[mid:]
        return left, right
