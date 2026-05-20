import re
from typing import Any, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel

from revup.github.endpoint import GitHubEndpoint

_VAR_DECL_RE = re.compile(r"\(\s*(\$\w+:\s*\S+(?:,\s*\$\w+:\s*\S+)*)\s*\)")
_BODY_RE = re.compile(r"\{\s*(.+)\s*\}", re.DOTALL)


def _serialize_variables(variables: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if variables is None:
        return None
    return {k: _serialize_value(v) for k, v in variables.items()}


def _serialize_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return value


def _parse_operation(query: str) -> Tuple[str, str]:
    """Extract variable declarations and body from a GraphQL operation string."""
    decl_match = _VAR_DECL_RE.search(query)
    decls = decl_match.group(1).strip() if decl_match else ""
    start = query.index("{") + 1
    depth = 1
    i = start
    while depth > 0:
        if query[i] == "{":
            depth += 1
        elif query[i] == "}":
            depth -= 1
        i += 1
    body = query[start : i - 1].strip()
    return decls, body


class BatchOperation:
    """A queued operation waiting to be executed as part of a batch."""

    def __init__(
        self,
        query: str,
        variables: Dict[str, Any],
        model_class: Type[BaseModel],
    ):
        self.query = query
        self.variables = variables
        self.model_class = model_class
        self.result: Optional[BaseModel] = None


class GitHubBatch:
    """
    Collects multiple GraphQL operations and executes them in a single HTTP request.
    Each typed client method becomes a non-async call that queues the operation and
    returns its index. After flush(), results are available by index.
    """

    def __init__(self, endpoint: GitHubEndpoint) -> None:
        self.endpoint = endpoint
        self._ops: List[BatchOperation] = []

    def add(
        self,
        query: str,
        variables: Dict[str, Any],
        model_class: Type[BaseModel],
    ) -> int:
        op = BatchOperation(query, variables, model_class)
        self._ops.append(op)
        return len(self._ops) - 1

    @property
    def pending(self) -> bool:
        return len(self._ops) > 0

    def get(self, index: int) -> BaseModel:
        op = self._ops[index]
        assert op.result is not None
        return op.result

    async def flush(self) -> None:
        if not self._ops:
            return

        all_var_decls: List[str] = []
        all_body_parts: List[str] = []
        all_variables: Dict[str, Any] = {}

        for i, op in enumerate(self._ops):
            prefix = f"_b{i}_"
            decls, body = _parse_operation(op.query)

            # Prefix all variable names to avoid collisions
            renamed_decls = []
            rename_map: Dict[str, str] = {}
            if decls:
                for part in decls.split(","):
                    part = part.strip()
                    # $varName: Type!
                    var_name = part.split(":")[0].strip().lstrip("$")
                    var_type = part.split(":", 1)[1].strip()
                    new_name = f"{prefix}{var_name}"
                    rename_map[var_name] = new_name
                    renamed_decls.append(f"${new_name}: {var_type}")

            # Rename variables in the body
            renamed_body = body
            for old_name, new_name in rename_map.items():
                renamed_body = re.sub(
                    r"\$" + re.escape(old_name) + r"\b",
                    f"${new_name}",
                    renamed_body,
                )

            # Alias the top-level field
            alias = f"_b{i}"
            all_body_parts.append(f"{alias}: {renamed_body}")
            all_var_decls.extend(renamed_decls)

            # Rename variable values
            for var_name, var_value in op.variables.items():
                all_variables[f"{prefix}{var_name}"] = var_value

        # Determine operation type from first query
        first_query = self._ops[0].query.strip()
        op_type = "mutation" if first_query.startswith("mutation") else "query"

        decl_str = ", ".join(all_var_decls)
        body_str = "\n".join(all_body_parts)
        combined = f"{op_type} ({decl_str}) {{\n{body_str}\n}}"

        result = await self.endpoint.graphql(combined, _serialize_variables(all_variables))
        data = result["data"]

        for i, op in enumerate(self._ops):
            alias = f"_b{i}"
            op_data = data[alias]
            # Wrap in the expected top-level key structure
            _, orig_body = _parse_operation(op.query)
            top_field = orig_body.split("(")[0].split("{")[0].strip()
            op.result = op.model_class.model_validate({top_field: op_data})


class GitHubBaseClient:
    def __init__(self, endpoint: GitHubEndpoint) -> None:
        self.endpoint = endpoint

    async def execute(  # pylint: disable=unused-argument
        self,
        query: str,
        operation_name: Optional[str] = None,
        variables: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        result = await self.endpoint.graphql(query, _serialize_variables(variables))
        return result["data"]

    def get_data(self, response: Dict[str, Any]) -> Dict[str, Any]:
        return response

    def batch(self) -> "GitHubBatch":
        return GitHubBatch(self.endpoint)
