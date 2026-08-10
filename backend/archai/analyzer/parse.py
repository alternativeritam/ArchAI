from __future__ import annotations

import pathlib
import re

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser


JAVA = Language(tsjava.language())


def _build_parser() -> Parser:
    """Create a tree-sitter parser for Java.

    Inputs:
        None. The parser uses the module-level JAVA language object built from
        the pinned tree-sitter-java package.

    Output:
        A configured Parser instance that can parse Java source bytes.

    Working:
        tree-sitter has had small API differences across Python releases. The
        pinned version accepts Parser(JAVA), but the fallback assignment path is
        kept so the backend is tolerant if a compatible parser exposes the older
        "parser.language = JAVA" API instead.
    """
    try:
        return Parser(JAVA)
    except TypeError:
        parser = Parser()
        parser.language = JAVA
        return parser


parser = _build_parser()

TYPE_NODES = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
)
METHOD_NODES = ("method_declaration", "constructor_declaration")


def _annotation_name(annotation: str) -> str:
    """Return the simple name from an annotation source string."""
    match = re.search(r"@([A-Za-z_][\w.]*)", annotation)
    return match.group(1).rsplit(".", 1)[-1] if match else annotation


def extract_annotations(node, src: bytes) -> list[dict]:
    """Extract annotations attached directly to a declaration with source evidence."""
    modifiers = find_child(node, "modifiers")
    if not modifiers:
        return []
    annotations = []
    for child in modifiers.children:
        if child.type not in {"annotation", "marker_annotation"}:
            continue
        raw = node_text(child, src)
        annotations.append(
            {
                "name": _annotation_name(raw),
                "raw": raw,
                "start_line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
            }
        )
    return annotations


def extract_parameters(node, src: bytes) -> list[dict]:
    """Extract direct formal parameters for methods and constructors."""
    parameters = node.child_by_field_name("parameters") or find_child(node, "formal_parameters")
    if not parameters:
        return []
    result = []
    for parameter in parameters.named_children:
        if parameter.type not in {"formal_parameter", "spread_parameter", "receiver_parameter"}:
            continue
        name = parameter.child_by_field_name("name")
        type_node = parameter.child_by_field_name("type")
        if not type_node:
            type_node = next(
                (
                    child
                    for child in parameter.named_children
                    if child.type
                    in {"type_identifier", "generic_type", "scoped_type_identifier", "array_type"}
                ),
                None,
            )
        result.append(
            {
                "name": node_text(name, src) if name else "<unknown>",
                "type": node_text(type_node, src) if type_node else "<unknown>",
                "start_line": parameter.start_point[0] + 1,
                "annotations": extract_annotations(parameter, src),
            }
        )
    return result


def extract_modifiers(node, src: bytes) -> list[str]:
    """Return Java keyword modifiers attached directly to a declaration."""
    modifiers = find_child(node, "modifiers")
    if not modifiers:
        return []
    return [
        node_text(child, src)
        for child in modifiers.children
        if child.type
        in {
            "public",
            "protected",
            "private",
            "static",
            "final",
            "abstract",
            "synchronized",
            "native",
            "strictfp",
            "default",
        }
    ]


def extract_method_invocations(node, src: bytes) -> list[dict]:
    """Extract bounded method/object creation evidence from a method body."""
    invocations: list[dict] = []
    stack = [node]
    while stack and len(invocations) < 200:
        current = stack.pop()
        if current.type == "method_invocation":
            name = current.child_by_field_name("name")
            obj = current.child_by_field_name("object")
            invocations.append(
                {
                    "kind": "method_call",
                    "name": node_text(name, src) if name else "<unknown>",
                    "receiver": node_text(obj, src) if obj else "",
                    "raw": node_text(current, src)[:500],
                    "line": current.start_point[0] + 1,
                }
            )
        if current.type == "object_creation_expression":
            type_node = current.child_by_field_name("type")
            invocations.append(
                {
                    "kind": "constructor_call",
                    "name": node_text(type_node, src) if type_node else "<unknown>",
                    "receiver": "",
                    "raw": node_text(current, src)[:500],
                    "line": current.start_point[0] + 1,
                }
            )
        stack.extend(reversed(current.named_children))
    return invocations


def node_text(node, src: bytes) -> str:
    """Return the source text represented by a tree-sitter node.

    Inputs:
        node: A tree-sitter node with byte offsets into the source file.
        src: The full source file as bytes.

    Output:
        A UTF-8 string for the exact source slice covered by the node.

    Working:
        tree-sitter reports byte offsets rather than character indexes. This
        helper slices the original bytes and decodes with replacement so odd
        encodings do not crash the analyzer.
    """
    return src[node.start_byte : node.end_byte].decode("utf8", errors="replace")


def find_child(node, type_name: str):
    """Find the first direct child of a node with a given tree-sitter type.

    Inputs:
        node: The parent tree-sitter node to inspect.
        type_name: The exact child node type to match, for example
            "package_declaration" or "type_list".

    Output:
        The first matching child node, or None when no direct child matches.

    Working:
        The lookup is intentionally shallow. Callers use this when the grammar
        contract expects a specific child directly below the current node, which
        avoids accidentally finding nested declarations in method bodies.
    """
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def extract_package(root, src: bytes) -> str:
    """Extract the Java package name from a parsed source file.

    Inputs:
        root: The tree-sitter "program" root node for one Java file.
        src: The full source file as bytes.

    Output:
        The package name such as "com.foo.service", or an empty string for the
        Java default package.

    Working:
        The function looks for a top-level package_declaration and then reads its
        identifier/scoped_identifier child. The package is never inferred from
        folders, which keeps the analyzer independent of Gradle, Maven, Ant, or
        custom source layouts.
    """
    pkg = find_child(root, "package_declaration")
    if not pkg:
        return ""
    for child in pkg.children:
        if child.type in ("scoped_identifier", "identifier"):
            return node_text(child, src)
    return ""


def extract_imports(root, src: bytes) -> list[dict]:
    """Extract top-level Java imports from a parsed source file.

    Inputs:
        root: The tree-sitter "program" root node for one Java file.
        src: The full source file as bytes.

    Output:
        A list of dictionaries with:
            raw: Imported name, such as "com.foo.Bar" or "com.foo.Bar.baz".
            static: True when the import is "import static ...".
            wildcard: True when the import ends in ".*".

    Working:
        Only direct children of the program root are considered, because Java
        imports are top-level declarations. Static and wildcard imports are
        tagged here; actual internal/external resolution is handled later by
        resolve_imports().
    """
    imports = []
    for child in root.children:
        if child.type != "import_declaration":
            continue

        is_static = any(ch.type == "static" for ch in child.children)
        is_wildcard = any(ch.type == "asterisk" for ch in child.children)
        name = None
        for ch in child.children:
            if ch.type in ("scoped_identifier", "identifier"):
                name = node_text(ch, src)

        imports.append({"raw": name, "static": is_static, "wildcard": is_wildcard})
    return imports


def _first_type_text(node, src: bytes) -> str | None:
    """Find the first type-like name inside a tree-sitter subtree.

    Inputs:
        node: A tree-sitter subtree that may contain a Java type node.
        src: The full source file as bytes.

    Output:
        The first type text found, such as "AbstractService" or
        "List<User>", or None if no type-like child exists.

    Working:
        Extends and implements grammar nodes can wrap the actual type in a few
        different node shapes. This helper recursively walks the subtree and
        returns the first recognized type/identifier node so the caller does not
        depend on one exact wrapper shape.
    """
    if node.type in {
        "type_identifier",
        "generic_type",
        "scoped_type_identifier",
        "scoped_identifier",
        "identifier",
    }:
        return node_text(node, src)

    for child in node.children:
        text = _first_type_text(child, src)
        if text:
            return text
    return None


def _extract_extends(node, src: bytes) -> str | None:
    """Extract the superclass name from a Java type declaration.

    Inputs:
        node: A top-level class, enum, or record declaration node.
        src: The full source file as bytes.

    Output:
        The superclass text such as "AbstractService", or None when the type has
        no explicit extends clause.

    Working:
        The Java grammar exposes class inheritance through the "superclass" field
        on class-like declarations. A small fallback checks older/alternate node
        names so the parser remains robust across compatible grammar versions.
    """
    superclass = node.child_by_field_name("superclass") or find_child(node, "superclass")
    if not superclass:
        superclass = find_child(node, "extends_interfaces")
    if not superclass:
        return None
    return _first_type_text(superclass, src)


def _extract_implements(node, src: bytes) -> list[str]:
    """Extract implemented or extended interface names from a type declaration.

    Inputs:
        node: A top-level class/interface/enum/record declaration node.
        src: The full source file as bytes.

    Output:
        A list of interface type names as written in source, for example
        ["UserApi"] or ["Comparable<User>"].

    Working:
        Classes use "implements" while interfaces use "extends" for parent
        interfaces. Tree-sitter represents both through interface-related nodes,
        so this helper reads either "interfaces", "super_interfaces", or
        "extends_interfaces" and collects the type entries from the type_list.
    """
    interfaces = (
        node.child_by_field_name("interfaces")
        or find_child(node, "super_interfaces")
        or find_child(node, "extends_interfaces")
    )
    if not interfaces:
        return []

    type_list = find_child(interfaces, "type_list") or interfaces
    out = []
    for child in type_list.children:
        if child.type in {
            "type_identifier",
            "generic_type",
            "scoped_type_identifier",
            "scoped_identifier",
            "identifier",
        }:
            out.append(node_text(child, src))
    return out


def build_type(node, src: bytes) -> dict:
    """Build the analyzer record for one top-level Java type.

    Inputs:
        node: A top-level class, interface, enum, or record declaration node.
        src: The full source file as bytes.

    Output:
        A dictionary containing the type kind, name, source line range,
        superclass, implemented interfaces, and method/constructor summaries.

    Working:
        The function reads declaration metadata directly from tree-sitter fields.
        Method extraction is limited to direct children of the type body, so
        nested/inner classes are not modeled. Constructors and methods
        are represented together because both become RAG chunks and line-range
        anchors for downstream consumers.
    """
    name_node = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    methods = []
    fields = []

    if body:
        for child in body.children:
            if child.type == "field_declaration":
                type_node = child.child_by_field_name("type")
                field_type = node_text(type_node, src) if type_node else "<unknown>"
                for declarator in child.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    field_name = declarator.child_by_field_name("name")
                    fields.append(
                        {
                            "name": node_text(field_name, src) if field_name else "<unknown>",
                            "type": field_type,
                            "modifiers": extract_modifiers(child, src),
                            "annotations": extract_annotations(child, src),
                            "start_line": declarator.start_point[0] + 1,
                        }
                    )
                continue
            if child.type not in METHOD_NODES:
                continue
            method_name = child.child_by_field_name("name")
            methods.append(
                {
                    "name": node_text(method_name, src) if method_name else "<init>",
                    "kind": "constructor" if child.type == "constructor_declaration" else "method",
                    "modifiers": extract_modifiers(child, src),
                    "return_type": (
                        node_text(child.child_by_field_name("type"), src)
                        if child.child_by_field_name("type")
                        else ""
                    ),
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                    "annotations": extract_annotations(child, src),
                    "parameters": extract_parameters(child, src),
                    "invocations": extract_method_invocations(child, src),
                }
            )

    extends = None
    implements = []
    if node.type == "interface_declaration":
        implements = _extract_implements(node, src)
    else:
        extends = _extract_extends(node, src)
        implements = _extract_implements(node, src)

    return {
        "kind": node.type.replace("_declaration", ""),
        "name": node_text(name_node, src) if name_node else "<anon>",
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "extends": extends,
        "implements": implements,
        "modifiers": extract_modifiers(node, src),
        "annotations": extract_annotations(node, src),
        "fields": fields,
        "record_components": extract_parameters(node, src) if node.type == "record_declaration" else [],
        "methods": methods,
    }


def is_test_path(rel_id: str) -> bool:
    """Detect whether a Java file path should be tagged as test code.

    Inputs:
        rel_id: Repository-relative file id using either "/" or "\\" separators.

    Output:
        True for common test paths/names, otherwise False.

    Working:
        The check is path-based and deliberately conservative. Test files are not
        excluded from outputs; they are tagged with is_test so graph and RAG
        consumers can decide whether to filter or down-rank them.
    """
    low = rel_id.replace("\\", "/").lower()
    return "/test/" in low or low.endswith("test.java") or low.endswith("tests.java")


def parse_file(path: str | pathlib.Path, root_dir: str | pathlib.Path) -> dict:
    """Parse one Java file into the normalized file record.

    Inputs:
        path: Absolute or root-relative path to a Java source file.
        root_dir: Repository root used to compute the stable relative file id.

    Output:
        A dictionary with file id, absolute path, language, package, line count,
        test flag, top-level type records, import records, and the in-memory
        source bytes under "_src" for chunk creation.

    Working:
        The file is read as bytes and parsed once with tree-sitter. The relative
        id becomes the stable graph/chunk key. Source bytes are retained only in
        memory so graph.json stays small while chunks.jsonl can include the
        actual code text.
    """
    path = pathlib.Path(path)
    root_dir = pathlib.Path(root_dir)
    src = path.read_bytes()
    root = parser.parse(src).root_node
    rel = str(path.relative_to(root_dir)).replace("\\", "/")
    types = [build_type(child, src) for child in root.children if child.type in TYPE_NODES]

    return {
        "id": rel,
        "path": str(path),
        "language": "java",
        "package": extract_package(root, src),
        "loc": src.count(b"\n") + 1,
        "is_test": is_test_path(rel),
        "types": types,
        "imports": extract_imports(root, src),
        "_src": src,
    }
