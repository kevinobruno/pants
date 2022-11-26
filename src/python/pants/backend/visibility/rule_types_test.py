# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import re
from pathlib import PurePath
from textwrap import dedent
from typing import Any

import pytest

from pants.backend.visibility.glob import TargetGlob
from pants.backend.visibility.rule_types import (
    BuildFileVisibilityRules,
    BuildFileVisibilityRulesError,
    VisibilityRule,
    VisibilityRuleSet,
    flatten,
)
from pants.backend.visibility.rules import rules as visibility_rules
from pants.core.target_types import FilesGeneratorTarget, GenericTarget, ResourcesGeneratorTarget
from pants.engine.addresses import Address, Addresses, AddressInput
from pants.engine.internals.dep_rules import (
    DependencyRuleAction,
    DependencyRuleActionDeniedError,
    DependencyRuleApplication,
)
from pants.engine.internals.target_adaptor import TargetAdaptor
from pants.engine.target import DependenciesRuleApplication, DependenciesRuleApplicationRequest
from pants.testutil.pytest_util import assert_logged, no_exception
from pants.testutil.rule_runner import QueryRule, RuleRunner, engine_error
from pants.util.strutil import softwrap

# -----------------------------------------------------------------------------------------------
# Rule type classes tests.
# -----------------------------------------------------------------------------------------------


def parse_address(raw: str, description_of_origin: str = repr("test"), **kwargs) -> Address:
    parsed = AddressInput.parse(raw, description_of_origin=description_of_origin, **kwargs)
    return parsed.dir_to_address() if "." not in raw else parsed.file_to_address()


def parse_rule(rule: str, relpath: str = "test/path") -> VisibilityRule:
    return VisibilityRule.parse(rule, relpath)


def parse_ruleset(rules: Any, build_file: str = "test/path/BUILD") -> VisibilityRuleSet:
    return VisibilityRuleSet.parse(build_file, rules)


@pytest.mark.parametrize(
    "expected, xs",
    [
        (
            ["foo"],
            "foo",
        ),
        (
            ["foo", "bar"],
            ("foo", "bar"),
        ),
        (
            ["foo", "bar", "baz"],
            (
                "foo",
                (
                    "bar",
                    ("baz",),
                ),
            ),
        ),
        (
            ["foo", "bar", "baz"],
            (
                "foo",
                (
                    "bar",
                    "baz",
                ),
            ),
        ),
        (
            ["foo", "bar", "baz"],
            (
                (
                    "foo",
                    "bar",
                    "baz",
                ),
            ),
        ),
        (
            ["src/test"],
            PurePath("src/test"),
        ),
        (
            ["src/test", "", "# comment", "last/rule", ""],
            """\
            src/test

            # comment
            last/rule
            """,
        ),
    ],
)
def test_flatten(expected, xs) -> None:
    assert expected == list(flatten(xs, str))


@pytest.mark.parametrize(
    "expected, rule, path, relpath",
    [
        (True, "src/a", "src/a", ""),
        (True, "?src/a", "src/a", ""),
        (True, "!src/a", "src/a", ""),
        (False, "src/a", "src/b", ""),
        (False, "?src/a", "src/b", ""),
        (False, "!src/a", "src/b", ""),
        (True, "src/a/*", "src/a/b", ""),
        (False, "src/a/*", "src/a/b/c/d", ""),
        (True, "src/a/**", "src/a/b/c/d", ""),
        (True, "src/a/**", "src/a", ""),
        (False, "src/a/*", "src/a", ""),
        (False, "src/a/*/c", "src/a/b/c/d", ""),
        (True, "src/a/**/c", "src/a/b/d/c", ""),
        (True, ".", "src/a", "src/a"),
        (False, ".", "src/a", "src/b"),
        (False, ".", "src/a/b", "src/a"),
        (True, "./*", "src/a/b", "src/a"),
        (False, "./*", "src/a/b", "src/a/b/c"),
        (True, ".ext", "my_file.ext", ""),
        (True, ".ext", "path/my_file.ext", ""),
        (True, "my_file.ext", "my_file.ext", ""),
        (True, "my_file.ext", "path/my_file.ext", ""),
        (False, "my_file.ext", "not_my_file.ext", ""),
        (True, "*my_file.ext", "path/some_of_my_file.ext", ""),
    ],
)
def test_visibility_rule(expected: bool, rule: str, path: str, relpath: str) -> None:
    assert parse_rule(rule).match(path, relpath) == expected


@pytest.mark.parametrize(
    "expected, arg",
    [
        (
            VisibilityRuleSet(
                "test/path/BUILD",
                (TargetGlob.parse("target", ""),),
                (parse_rule("src/*"),),
            ),
            ("target", "src/*"),
        ),
        (
            VisibilityRuleSet(
                "test/path/BUILD",
                (TargetGlob.parse("files", ""), TargetGlob.parse("resources", "")),
                (
                    parse_rule(
                        "src/*",
                    ),
                    parse_rule("res/*"),
                    parse_rule("!*"),
                ),
            ),
            (("files", "resources"), "src/*", "res/*", "!*"),
        ),
    ],
)
def test_visibility_rule_set_parse(expected: VisibilityRuleSet, arg: Any) -> None:
    rule_set = parse_ruleset(arg)
    assert expected == rule_set


@pytest.mark.parametrize(
    "expected, target, rule_spec",
    [
        (
            True,
            "python_sources",
            ("python_*", ""),
        ),
        (
            False,
            "shell_sources",
            ("python_*", ""),
        ),
        (
            True,
            "files",
            (("files", "resources"), ""),
        ),
        (
            True,
            "resources",
            (("files", "resources"), ""),
        ),
        (
            False,
            "resource",
            (("files", "resources"), ""),
        ),
    ],
)
def test_visibility_rule_set_match(expected: bool, target: str, rule_spec: tuple) -> None:
    assert expected == parse_ruleset(rule_spec, "").match(
        Address(""), TargetAdaptor(target, None), ""
    )


@pytest.fixture
def dependencies_rules() -> BuildFileVisibilityRules:
    return BuildFileVisibilityRules(
        "src/BUILD",
        # Rules for outgoing dependency.
        (
            parse_ruleset(("requirement", "!//3rdparty/req#restrict*", "//3rdparty/**"), "BUILD"),
            parse_ruleset(("*", ("tgt/ok/*", "?tgt/dubious/*", "!tgt/blocked/*")), "src/BUILD"),
        ),
    )


@pytest.fixture
def dependents_rules() -> BuildFileVisibilityRules:
    return BuildFileVisibilityRules(
        "tgt/BUILD",
        # Rules for incoming dependency.
        (
            parse_ruleset(("requirement", "*"), "BUILD"),
            parse_ruleset(("*", ("src/ok/*", "?src/dubious/*", "!src/blocked/*")), "tgt/BUILD"),
        ),
    )


@pytest.mark.parametrize(
    "target_type, source_path, target_path, expected_action, expected_rule",
    [
        ("test", "src/ok/a", "tgt/ok/b", "allow", "src/BUILD[tgt/ok/*] -> tgt/BUILD[src/ok/*]"),
        (
            "test",
            "src/ok/a",
            "tgt/dubious/b",
            "warn",
            "src/BUILD[?tgt/dubious/*] -> tgt/BUILD[src/ok/*]",
        ),
        (
            "test",
            "src/ok/a",
            "tgt/blocked/b",
            "deny",
            "src/BUILD[!tgt/blocked/*] -> tgt/BUILD[src/ok/*]",
        ),
        (
            "test",
            "src/dubious/a",
            "tgt/ok/b",
            "warn",
            "src/BUILD[tgt/ok/*] -> tgt/BUILD[?src/dubious/*]",
        ),
        (
            "test",
            "src/dubious/a",
            "tgt/dubious/b",
            "warn",
            "src/BUILD[?tgt/dubious/*] -> tgt/BUILD[?src/dubious/*]",
        ),
        (
            "test",
            "src/dubious/a",
            "tgt/blocked/b",
            "deny",
            "src/BUILD[!tgt/blocked/*] -> tgt/BUILD[?src/dubious/*]",
        ),
        (
            "test",
            "src/blocked/a",
            "tgt/ok/b",
            "deny",
            "src/BUILD[tgt/ok/*] -> tgt/BUILD[!src/blocked/*]",
        ),
        (
            "test",
            "src/blocked/a",
            "tgt/dubious/b",
            "deny",
            "src/BUILD[?tgt/dubious/*] -> tgt/BUILD[!src/blocked/*]",
        ),
        (
            "test",
            "src/blocked/a",
            "tgt/blocked/b",
            "deny",
            "src/BUILD[!tgt/blocked/*] -> tgt/BUILD[!src/blocked/*]",
        ),
        (
            "requirement",
            "src/proj/code.ext",
            "3rdparty/req#lib",
            "allow",
            "BUILD[//3rdparty/**] -> BUILD[*]",
        ),
        (
            "requirement",
            "src/proj/code.ext",
            "3rdparty/req#restricted",
            "deny",
            "BUILD[!//3rdparty/req#restrict*] -> BUILD[*]",
        ),
    ],
)
def test_check_dependency_rules(
    dependencies_rules: BuildFileVisibilityRules,
    dependents_rules: BuildFileVisibilityRules,
    target_type: str,
    source_path: str,
    target_path: str,
    expected_action: str,
    expected_rule: str,
) -> None:
    origin_address = parse_address(source_path)
    dependency_address = parse_address(target_path)
    assert DependencyRuleApplication(
        action=DependencyRuleAction(expected_action),
        rule_description=expected_rule,
        origin_address=origin_address,
        origin_type=target_type,
        dependency_address=dependency_address,
        dependency_type=target_type,
    ) == BuildFileVisibilityRules.check_dependency_rules(
        origin_address=origin_address,
        origin_adaptor=TargetAdaptor(target_type, "source"),
        dependencies_rules=dependencies_rules,
        dependency_address=dependency_address,
        dependency_adaptor=TargetAdaptor(target_type, "target"),
        dependents_rules=dependents_rules,
    )


# -----------------------------------------------------------------------------------------------
# BUILD file level tests.
# -----------------------------------------------------------------------------------------------


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *visibility_rules(),
            QueryRule(DependenciesRuleApplication, (DependenciesRuleApplicationRequest,)),
        ],
        target_types=[FilesGeneratorTarget, GenericTarget, ResourcesGeneratorTarget],
    )


def denied(pattern: str = "!*", side: int = 1):
    build_files = (
        f"src/origin -> src/dependency/BUILD[{pattern}]"
        if side > 0
        else f"src/origin/BUILD[{pattern}] -> src/dependency"
    )
    return pytest.raises(
        DependencyRuleActionDeniedError,
        match=re.escape(
            dedent(
                f"""\
                src/origin:origin has 1 dependency violation:

                  * {build_files} : DENY
                    target src/origin:origin -> target src/dependency:dependency
                """
            ).strip()
        ),
    )


@pytest.mark.parametrize(
    "rules, expect_error",
    [
        (["*"], None),
        (["!*"], denied()),
        (["src/origin", "!*"], None),
        (["!src/origin", "*"], denied("!src/origin")),
        (["!src/origin/nested", "*"], None),
        (["src/origin/nested", "!*"], denied()),
        (["!src/a", "!src/b", "!src/origin", "!src/c", "*"], denied("!src/origin")),
        (["!src/a", "!src/b", "!src/c", "*"], None),
        (["src/a", "src/b", "src/origin", "src/c", "!*"], None),
        (["src/a", "src/b", "src/c", "!*"], denied()),
    ],
)
def test_dependents_rules(rule_runner: RuleRunner, rules: list[str], expect_error) -> None:
    rule_runner.write_files(
        {
            "src/dependency/BUILD": dedent(
                f"""\
                __dependents_rules__((target, {rules}))
                target()
                """
            ),
            "src/origin/BUILD": dedent(
                """\
                target(dependencies=["src/dependency:tgt"])
                """
            ),
        },
    )

    rsp = rule_runner.request(
        DependenciesRuleApplication,
        [
            DependenciesRuleApplicationRequest(
                Address("src/origin"),
                dependencies=Addresses([Address("src/dependency")]),
                description_of_origin="test",
            )
        ],
    )
    with expect_error or no_exception():
        rsp.execute_actions()


@pytest.mark.parametrize(
    "rules, expect_error",
    [
        (["*"], None),
        (["src/dependency", "!*"], None),
        (["src/dependency/nested", "!*"], denied(side=-1)),
        (["src/*", "!*"], None),
        (["!src/*", "*"], denied("!src/*", side=-1)),
    ],
)
def test_dependencies_rules(rule_runner: RuleRunner, rules: list[str], expect_error) -> None:
    rule_runner.write_files(
        {
            "src/dependency/BUILD": "target()",
            "src/origin/BUILD": dedent(
                f"""\
                __dependencies_rules__((target, {rules}))
                target(dependencies=["src/dependency:tgt"])
                """
            ),
        },
    )

    rsp = rule_runner.request(
        DependenciesRuleApplication,
        [
            DependenciesRuleApplicationRequest(
                Address("src/origin"),
                dependencies=Addresses([Address("src/dependency")]),
                description_of_origin="test",
            )
        ],
    )
    with expect_error or no_exception():
        rsp.execute_actions()


def assert_dependency_rules(
    rule_runner: RuleRunner, origin: str, *dependencies: tuple[str, DependencyRuleAction]
) -> None:
    desc = repr("assert_dependency_rules")
    source = parse_address(origin, description_of_origin=desc)
    addresses = Addresses(
        [
            parse_address(dep, relative_to=source.spec_path, description_of_origin=desc)
            for dep, _ in dependencies
        ]
    )
    rsp = rule_runner.request(
        DependenciesRuleApplication,
        [
            DependenciesRuleApplicationRequest(
                source,
                dependencies=addresses,
                description_of_origin=desc,
            )
        ],
    )

    for address, (_, action) in zip(addresses, dependencies):
        application = rsp.dependencies_rule[address]
        print(
            "-",
            application.rule_description,
            "\n ",
            application.origin_address.spec,
            application.action.name,
            application.dependency_address.spec,
        )
        assert action == application.action


def test_dependency_rules(rule_runner: RuleRunner, caplog) -> None:
    ROOT_BUILD = dedent(
        """
        # ROOT RULES
        #
        # Parent rules apply to whole subtree unless overridden in a child BUILD file.

        __dependencies_rules__(
          # Deny internal resources from depending on outside files.
          (resources, ".", "!*"),

          # Allow files to use anything.
          (files, "*"),

          # Allow all by default, with a warning
          ("*", "?*"),

          # Ignore (accept) empty values as no-op
          None,
          (),
        )

        __dependents_rules__(
          # Deny outside access to "private" resources.
          (resources, ".", "!*"),

          # Anyone may depend on `files` targets.
          ("files", "*"),

          # Allow all by default, with a warning
          ("*", "?*"),
        )
        """
    )

    def BUILD(dependencies: tuple = (), dependents: tuple = (), extra: str = "") -> str:
        return dedent(
            f"""
            # `files` are "public"
            files()

            # `resources` are "private"
            resources(name="internal")

            {extra}

            __dependencies_rules__(
              *{dependencies},
              extend=True,
            )

            __dependents_rules__(
              *{dependents},
              extend=True,
            )
            """
        )

    rule_runner.write_files(
        {
            "src/BUILD": ROOT_BUILD,
            "src/a/BUILD": BUILD(),
            "src/a/a2/BUILD": BUILD(
                extra="""target(name="joker")""",
            ),
            "src/b/BUILD": BUILD(
                dependents=(),
            ),
            "src/b/b2/BUILD": BUILD(
                dependents=(
                    # Override default, any target in `b` may depend on internal targets
                    ("resources", "src/b", "src/b/*", "!*"),
                    # Only `b` may depend on our nested modules.
                    ("*", "src/b/*", "!*"),
                ),
            ),
        },
    )

    allowed = DependencyRuleAction.ALLOW
    denied = DependencyRuleAction.DENY
    warned = DependencyRuleAction.WARN
    caplog.set_level(logging.DEBUG)

    assert_dependency_rules(
        rule_runner,
        "src/a",
        ("src/a:internal", allowed),
        ("src/a/a2:internal", denied),
        ("src/b", allowed),
        ("src/b:internal", denied),
        ("src/b/b2", denied),
        ("src/a/a2:joker", warned),
    )
    assert_logged(
        caplog,
        expect_logged=[
            (
                logging.DEBUG,
                "WARN: type:target name:joker path:'src/a' relpath:'src/a/a2' address:src/a:a "
                "rule:'?*' src/a/a2/BUILD: ?*",
            ),
        ],
        exclusively=False,
    )

    caplog.clear()
    assert_dependency_rules(
        rule_runner,
        "src/b",
        ("src/b:internal", allowed),
        ("src/b/b2:internal", allowed),
        ("src/a", allowed),
        ("src/a:internal", denied),
        ("src/a/a2", allowed),
    )
    assert_logged(
        caplog,
        expect_logged=[
            (
                logging.DEBUG,
                "DENY: type:resources name:internal path:'src/b' relpath:'src/a' "
                "address:src/b:b rule:'!*' src/a/BUILD: ., !*",
            ),
        ],
        exclusively=False,
    )

    caplog.clear()
    assert_dependency_rules(
        rule_runner,
        "src/a:internal",
        ("src/a", allowed),
        ("src/b", denied),
    )
    assert_logged(
        caplog,
        expect_logged=[
            (
                logging.DEBUG,
                "DENY: type:resources name:internal path:'src/b' relpath:'src/a' address:src/b:b "
                "rule:'!*' src/a/BUILD: ., !*",
            ),
        ],
        exclusively=False,
    )


def test_missing_rule_error_message(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "src/BUILD": dedent(
                """
                __dependencies_rules__(
                  (target, ".", "!*"),
                  (resources, "!nope"),
                )

                __dependents_rules__(
                  (resources, "res/*"),
                )

                resources(name="res")
                target(name="tgt")
                """
            ),
        },
    )

    msg = softwrap(
        """
        There is no matching rule from the `__dependencies_rules__` defined in src/BUILD for the
        `resources` target src:res for the dependency on the `target` target src:tgt

        Consider adding the required catch-all rule at the end of the rules spec. Example adding a
        "deny all" at the end:

          (('resources',), '!nope', '!*')
        """
    )
    with engine_error(BuildFileVisibilityRulesError, contains=msg):
        rule_runner.request(
            DependenciesRuleApplication,
            [
                DependenciesRuleApplicationRequest(
                    Address("src", target_name="res"),
                    dependencies=Addresses([Address("src", target_name="tgt")]),
                    description_of_origin=repr("test"),
                )
            ],
        )

    msg = softwrap(
        """
        There is no matching rule from the `__dependents_rules__` defined in src/BUILD for the
        `target` target src:tgt for the dependency on the `resources` target src:res

        Consider adding the required catch-all rule at the end of the rules spec. Example adding a
        "deny all" at the end:

          (('resources',), 'res/*', '!*')
        """
    )
    with engine_error(BuildFileVisibilityRulesError, contains=msg):
        rule_runner.request(
            DependenciesRuleApplication,
            [
                DependenciesRuleApplicationRequest(
                    Address("src", target_name="tgt"),
                    dependencies=Addresses([Address("src", target_name="res")]),
                    description_of_origin=repr("test"),
                )
            ],
        )


def test_gitignore_style_syntax(rule_runner: RuleRunner) -> None:
    allowed = DependencyRuleAction.ALLOW
    denied = DependencyRuleAction.DENY
    warned = DependencyRuleAction.WARN

    rule_runner.write_files(
        {
            "src/BUILD": dedent(
                """
                __dependencies_rules__(
                  (
                    "*",
                    '''
                    # Anything in `pub` directories
                    pub/*

                    # Everything rooted in `src/inc`
                    /inc/**

                    # Nothing from `src/priv/` trees
                    !src/priv/**
                    ''',

                    # Warn for anything else
                    "?*",
                  ),
                )
                """
            ),
            "src/proj/BUILD": "files(name='a')",
            "src/inc/proj/interfaces/BUILD": "files()",
            "src/proj/pub/docs/BUILD": "files()",
            "src/proj/pub/docs/internal/BUILD": "files()",
            "tests/proj/src/priv/data/BUILD": "files()",
        },
    )

    assert_dependency_rules(
        rule_runner,
        "src/proj:a",
        ("src/inc/proj/interfaces", allowed),
        ("src/proj/pub/docs", allowed),
        (
            "src/proj/pub/docs/internal",
            warned,
        ),
        ("tests/proj/src/priv/data", denied),
    )


def test_file_specific_rules(rule_runner: RuleRunner) -> None:
    files = (
        "src/lib/root.txt",
        "src/lib/pub/ok.txt",
        "src/lib/pub/exception.txt",
        "src/lib/priv/secret.txt",
        "src/app/root.txt",
        "src/app/sub/nested/impl.txt",
    )
    rule_runner.write_files(
        {
            "src/lib/BUILD": dedent(
                """
                __dependencies_rules__(
                  # Limit lib dependencies to only files from the lib/ tree.
                  ("*", "/**", "!*"),
                )

                __dependents_rules__(

                  # Limit dependencies upon lib to only come from within the lib/ tree, except this
                  # one particular impl file when it comes from a nested/ folder, we warn about it
                  # though.
                  ("*", "/**", "?nested/impl.txt", "!*"),
                )

                files(sources=["**/*.txt"])
                """
            ),
            "src/lib/pub/BUILD": dedent(
                """
                __dependents_rules__(
                  # Allow all to depend on files from lib/pub/
                  # TODO: support to except one file. work around now is to have the rule in
                  # src/app/BUILD
                  ("*", "*"),
                )
                """
            ),
            "src/app/BUILD": dedent(
                """
                __dependencies_rules__(
                  # TODO: this exception should live in src/lib/pub/BUILD
                  ("*", "!//src/lib/pub/exception.txt", "*"),
                )

                files(sources=["**/*.txt"])
                """
            ),
            **{path: "" for path in files},
        },
    )

    assert_dependency_rules(
        rule_runner,
        "src/lib/root.txt",
        ("src/lib/pub/ok.txt:../lib", DependencyRuleAction.ALLOW),
        ("src/lib/priv/secret.txt:../lib", DependencyRuleAction.ALLOW),
        ("src/app/root.txt", DependencyRuleAction.DENY),
    )

    assert_dependency_rules(
        rule_runner,
        "src/app/root.txt",
        ("src/lib/root.txt", DependencyRuleAction.DENY),
        ("src/lib/pub/ok.txt:../lib", DependencyRuleAction.ALLOW),
        ("src/lib/pub/exception.txt:../lib", DependencyRuleAction.DENY),
        ("src/lib/priv/secret.txt:../lib", DependencyRuleAction.DENY),
    )

    assert_dependency_rules(
        rule_runner,
        "src/app/sub/nested/impl.txt:../../app",
        ("src/lib/root.txt", DependencyRuleAction.WARN),
        ("src/lib/pub/ok.txt:../lib", DependencyRuleAction.ALLOW),
        ("src/lib/pub/exception.txt:../lib", DependencyRuleAction.DENY),
        ("src/lib/priv/secret.txt:../lib", DependencyRuleAction.WARN),
    )


def test_relpath_for_file_targets(rule_runner: RuleRunner) -> None:
    # Testing purpose:
    #
    # When a file is owned by a target declared in a parent directory, make sure the correct BUILD
    # file is consulted for the rules set to apply, and with the correct relpath for the matching.
    rule_runner.write_files(
        {
            "anchor-mode/invoked/BUILD": dedent(
                """
                files(name="inv", sources=["**/*.inv"])

                __dependencies_rules__(
                  ("*", "../dependency/*", "!*"),
                )

                __dependents_rules__(
                  ("*", "../origin/*", "!*"),
                )
                """
            ),
            "anchor-mode/invoked/origin/file1.inv:../inv": "",
            "anchor-mode/invoked/dependency/file2.inv:../inv": "",
        },
    )

    assert_dependency_rules(
        rule_runner,
        "anchor-mode/invoked/origin/file1.inv:../inv",
        ("anchor-mode/invoked/dependency/file2.inv:../inv", DependencyRuleAction.ALLOW),
    )