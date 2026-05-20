from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class QueryGroup:
    """A batch of homogeneous aliased fields in a GraphQL operation.

    All items share the same field_template and variable types.
    Supports slicing to split items across multiple queries while
    preserving original alias indices (so merged results don't collide).
    """

    prefix: str
    scope: str  # "repo" | "top" | "mutation"
    field_template: str
    var_types: List[str]
    fragment: str = ""
    values: List[List[Any]] = field(default_factory=list)
    _offset: int = 0

    def add(self, *values: Any) -> str:
        assert len(values) == len(self.var_types)
        idx = len(self.values)
        self.values.append(list(values))
        return self.alias(idx)

    def alias(self, idx: int) -> str:
        return f"{self.prefix}_out{self._offset + idx}"

    @property
    def aliases(self) -> List[str]:
        return [self.alias(i) for i in range(len(self.values))]

    def var_name(self, item_idx: int, var_idx: int) -> str:
        actual = self._offset + item_idx
        if len(self.var_types) == 1:
            return f"{self.prefix}{actual}"
        return f"{self.prefix}{actual}_{var_idx}"

    def __len__(self) -> int:
        return len(self.values)

    def render_fields(self) -> str:
        parts: List[str] = []
        for i in range(len(self.values)):
            var_names = [f"${self.var_name(i, j)}" for j in range(len(self.var_types))]
            parts.append(self.field_template.format(self.alias(i), *var_names))
        return "".join(parts)

    def render_declarations(self) -> List[str]:
        decls: List[str] = []
        for i in range(len(self.values)):
            for j, vtype in enumerate(self.var_types):
                decls.append(f"${self.var_name(i, j)}: {vtype}")
        return decls

    def render_variables(self) -> Dict[str, Any]:
        variables: Dict[str, Any] = {}
        for i, vals in enumerate(self.values):
            for j, val in enumerate(vals):
                variables[self.var_name(i, j)] = val
        return variables

    def extract(self, result: Any) -> List[Any]:
        if self.scope == "repo":
            repo = result["data"]["repository"]
            return [repo[self.alias(i)] for i in range(len(self.values))]
        else:
            data = result["data"]
            return [data[self.alias(i)] for i in range(len(self.values))]

    def slice(self, start: int, end: int) -> QueryGroup:
        g = QueryGroup(
            prefix=self.prefix,
            scope=self.scope,
            field_template=self.field_template,
            var_types=list(self.var_types),
            fragment=self.fragment,
            _offset=self._offset + start,
        )
        g.values = self.values[start:end]
        return g


class GraphqlQuery:
    """Builds a GraphQL query/mutation from composable, sliceable groups."""

    def __init__(self, operation: str = "query", name: str = ""):
        self.operation = operation
        self.name = name
        self.fixed_vars: List[Tuple[str, str, Any]] = []
        self.fixed_repo_fields: str = ""
        self.groups: List[QueryGroup] = []

    def add_fixed_var(self, name: str, gql_type: str, value: Any) -> None:
        self.fixed_vars.append((name, gql_type, value))

    def add_group(self, group: QueryGroup) -> None:
        self.groups.append(group)

    def total_items(self) -> int:
        return sum(len(g) for g in self.groups)

    def build(self) -> Tuple[str, Dict[str, Any]]:
        all_decls: List[str] = []
        variables: Dict[str, Any] = {}

        for name, gql_type, value in self.fixed_vars:
            all_decls.append(f"${name}: {gql_type}")
            variables[name] = value

        for group in self.groups:
            all_decls.extend(group.render_declarations())
            variables.update(group.render_variables())

        decl_str = ", ".join(all_decls)
        name_str = f" {self.name}" if self.name else ""

        repo_fields = self.fixed_repo_fields
        top_fields = ""
        mutation_fields = ""
        for group in self.groups:
            rendered = group.render_fields()
            if group.scope == "repo":
                repo_fields += rendered
            elif group.scope == "top":
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
        for group in self.groups:
            if group.fragment and group.fragment not in seen and len(group) > 0:
                fragments += group.fragment
                seen.add(group.fragment)
        query_str += fragments

        return query_str, variables

    def split(self) -> Tuple[GraphqlQuery, GraphqlQuery]:
        """Split into two queries by halving each group's items.

        Alias indices are preserved so merged results don't collide.
        """
        left = GraphqlQuery(operation=self.operation, name=self.name)
        right = GraphqlQuery(operation=self.operation, name=self.name)

        left.fixed_vars = list(self.fixed_vars)
        right.fixed_vars = list(self.fixed_vars)
        left.fixed_repo_fields = self.fixed_repo_fields
        right.fixed_repo_fields = self.fixed_repo_fields

        for group in self.groups:
            mid = len(group) // 2
            if mid == 0:
                left.add_group(group.slice(0, len(group)))
                right.add_group(group.slice(0, 0))
            else:
                left.add_group(group.slice(0, mid))
                right.add_group(group.slice(mid, len(group)))

        return left, right
