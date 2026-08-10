from __future__ import annotations

from collections import defaultdict


def build_symbol_table(files: list[dict]) -> dict[str, str]:
    """Build a fully-qualified top-level type name to file id map.

    Inputs:
        files: Parsed file records from parse_file().

    Output:
        A dictionary mapping Java FQNs such as "com.foo.UserService" to the
        repository-relative file id that declares that top-level type.

    Working:
        Every top-level type in each file is registered. The package statement is
        combined with the type name; default-package classes use only the type
        name. Nested classes are intentionally absent because they are outside
        the scope.
    """
    table = {}
    for file in files:
        package = file["package"]
        for type_info in file["types"]:
            fqn = f"{package}.{type_info['name']}" if package else type_info["name"]
            table[fqn] = file["id"]
    return table


def resolve_imports(files: list[dict], table: dict[str, str]) -> list[dict]:
    """Resolve parsed Java imports to internal repository files when possible.

    Inputs:
        files: Parsed file records. Their import dictionaries are updated in
            place.
        table: Symbol table produced by build_symbol_table().

    Output:
        The same files list, with each import augmented by:
            resolved: Target file id when the import points to an internal type.
            external: True when no internal match/package exists.

    Working:
        Normal imports are resolved by exact FQN. Static imports drop the final
        member segment so "com.foo.Helpers.log" resolves against
        "com.foo.Helpers". Wildcard imports are not expanded into many edges;
        they are only marked internal or external based on whether the package
        exists in the parsed repository.
    """
    packages = defaultdict(list)
    for file in files:
        packages[file["package"]].append(file["id"])

    for file in files:
        for imp in file["imports"]:
            raw = imp["raw"]
            if not raw:
                imp["resolved"] = None
                imp["external"] = True
                continue

            if imp["wildcard"]:
                imp["resolved"] = None
                if imp["static"]:
                    imp["external"] = raw not in table
                else:
                    imp["external"] = raw not in packages
                continue

            fqn = raw.rsplit(".", 1)[0] if imp["static"] else raw
            imp["resolved"] = table.get(fqn)
            imp["external"] = imp["resolved"] is None
    return files


def _base_type_name(name: str) -> str:
    """Normalize a type reference for simple-name matching.

    Inputs:
        name: A Java type reference, possibly with generic parameters.

    Output:
        The type name without generic parameters and surrounding whitespace.

    Working:
        Inheritance clauses can include generic forms such as
        "BaseService<User>". Import resolution only needs the declared type, so
        everything after the first "<" is removed before matching.
    """
    return name.split("<", 1)[0].strip()


def resolve_simple_name(simple: str, file: dict, table: dict[str, str]) -> str | None:
    """Resolve an inheritance type reference to an internal file id.

    Inputs:
        simple: Type name from an extends/implements clause. It may be simple,
            fully qualified, or generic.
        file: The parsed file record containing the inheritance clause.
        table: Symbol table produced by build_symbol_table().

    Output:
        The target file id if the parent type is internal, otherwise None.

    Working:
        Fully-qualified names are looked up directly. Simple names first check
        explicit resolved imports, then fall back to the same package. This
        matches the design and avoids full Java symbol resolution.
    """
    base = _base_type_name(simple)
    if "." in base:
        return table.get(base)

    for imp in file["imports"]:
        raw = imp["raw"]
        if (
            raw
            and not imp["wildcard"]
            and raw.rsplit(".", 1)[-1] == base
            and imp.get("resolved")
        ):
            return imp["resolved"]

    fqn = f"{file['package']}.{base}" if file["package"] else base
    return table.get(fqn)
