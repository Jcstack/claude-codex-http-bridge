"""Statically inventory one public Anthropic SDK snapshot; never import it.

This is a research fixture, not a JSON Schema exporter or a runtime tool policy.
Verify the checkout and its revision independently before interpreting results.
"""

import argparse
import ast
import json
from pathlib import Path


SNAPSHOT = "5752c132041af71055584632caab5ca2325cae5e"
COUNTS = {"browser": 31, "computer": 17}


def parse_file(path):
    if path.is_symlink():
        raise ValueError("A source file is a symbolic link")
    data = path.read_bytes()
    if len(data) > 512 * 1024:
        raise ValueError("A source file exceeds the fixture limit")
    return ast.parse(data.decode("utf-8"))


def one(items):
    items = list(items)
    if len(items) != 1:
        raise ValueError("The SDK source has an unexpected structure")
    return items[0]


def assigned(tree, name):
    return one(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
               and isinstance(node.target, ast.Name) and node.target.id == name)


def named_class(tree, name):
    return one(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)


def literal_names(expression):
    if not (isinstance(expression, ast.Subscript) and isinstance(expression.value, ast.Name)
            and expression.value.id == "Literal"):
        raise ValueError("Expected a Literal annotation")
    values = expression.slice.elts if isinstance(expression.slice, ast.Tuple) else [expression.slice]
    result = [ast.literal_eval(value) for value in values]
    if not all(isinstance(value, str) and value for value in result) or len(set(result)) != len(result):
        raise ValueError("Invalid or duplicate literal members")
    return result


def annotations(cls):
    fields = [node for node in cls.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)]
    if len({field.target.id for field in fields}) != len(fields):
        raise ValueError("Duplicate class fields")
    return {field.target.id: field.annotation for field in fields}


def imported_modules(tree):
    result = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
            continue
        if not node.module.isidentifier():
            raise ValueError("Unexpected relative module name")
        for alias in node.names:
            name = alias.asname or alias.name
            if name in result:
                raise ValueError("Ambiguous imported name")
            result[name] = (node.module, alias.name)
    return result


def members(beta, family):
    title = family.title()
    union = parse_file(beta / f"beta_{family}_tool_use_block.py")
    expression = assigned(union, f"Beta{title}ToolUseBlock")
    if not (isinstance(expression, ast.Subscript) and isinstance(expression.value, ast.Name)
            and expression.value.id == "Annotated" and isinstance(expression.slice, ast.Tuple)):
        raise ValueError("Unexpected tool-use union")
    group = expression.slice.elts[0]
    if not (isinstance(group, ast.Subscript) and isinstance(group.value, ast.Name)
            and group.value.id == "Union" and isinstance(group.slice, ast.Tuple)):
        raise ValueError("Unexpected tool-use union members")
    imports = imported_modules(union)
    result = {}
    for reference in group.slice.elts:
        if not isinstance(reference, ast.Name) or reference.id not in imports:
            raise ValueError("Unknown tool-use member")
        module, class_name = imports[reference.id]
        block = parse_file(beta / (module + ".py"))
        fields = annotations(named_class(block, class_name))
        name = one(literal_names(fields["name"]))
        if one(literal_names(fields["toolset_name"])) != family or one(literal_names(fields["type"])) != "tool_use":
            raise ValueError("A tool-use member has the wrong identity")
        input_type = fields["input"]
        if not isinstance(input_type, ast.Name):
            raise ValueError("Unexpected input model")
        input_imports = imported_modules(block)
        input_module, input_class = input_imports[input_type.id]
        input_source = parse_file(beta / (input_module + ".py"))
        model = named_class(input_source, input_class)
        if name in result:
            raise ValueError("Duplicate tool-use member name")
        result[name] = {"class": input_class, "fields": annotations(model)}
    declared = literal_names(assigned(parse_file(beta / f"beta_{family}_member_name.py"), f"Beta{title}MemberName"))
    if set(result) != set(declared) or len(result) != COUNTS[family]:
        raise ValueError("Member names do not match the expected snapshot")
    return result


def audit(root):
    beta = root / "src" / "anthropic" / "types" / "beta"
    browser = members(beta, "browser")
    computer = members(beta, "computer")
    data = parse_file(root / "src" / "anthropic" / "_data" / "browser_toolset_members.py")
    defaults = ast.literal_eval(assigned(data, "BROWSER_TOOLSET_MEMBERS"))
    if set(defaults) != set(browser) or not all(isinstance(v.get("enabled_by_default"), bool) for v in defaults.values()):
        raise ValueError("Browser defaults do not match the member list")
    disabled = sorted(k for k, v in defaults.items() if not v["enabled_by_default"])
    if len(disabled) != 4:
        raise ValueError("Browser defaults do not match the expected snapshot")
    scroll = ast.unparse(browser["scroll"]["fields"]["scroll_amount"])
    wait = ast.unparse(browser["wait"]["fields"]["duration"])
    if (scroll, wait) != ("Optional[int]", "float"):
        raise ValueError("Expected numeric input annotations changed")
    shared = set(browser) & set(computer)
    different_fields = sorted(name for name in shared
                              if set(browser[name]["fields"]) != set(computer[name]["fields"]))
    return {
        "expected_snapshot": SNAPSHOT,
        "source_integrity_verified_by_fixture": False,
        "source_imported_or_executed": False,
        "browser_members": len(browser),
        "computer_members": len(computer),
        "browser_disabled_by_default": disabled,
        "shared_names_requiring_family_identity": sorted(shared),
        "shared_names_with_different_input_field_names": different_fields,
        "browser_scroll_amount_annotation": scroll,
        "browser_wait_duration_annotation": wait,
        "schemas_or_executor_permissions_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdk_checkout", type=Path, help="Independently verified public SDK checkout")
    args = parser.parse_args()
    try:
        result = audit(args.sdk_checkout)
    except (OSError, SyntaxError, ValueError, KeyError, TypeError):
        raise SystemExit("SDK source does not match this bounded static inventory") from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
