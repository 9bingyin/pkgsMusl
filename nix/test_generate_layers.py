#!/usr/bin/env python3

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import generate_layers


def drv(hash_character: str, name: str) -> str:
    return f"/nix/store/{hash_character * 32}-{name}.drv"


def output(hash_character: str, name: str) -> str:
    return f"/nix/store/{hash_character * 32}-{name}"


def derivation(
    name: str,
    hash_character: str,
    inputs: dict[str, list[str]] | None = None,
    outputs: tuple[str, ...] = ("out",),
) -> dict[str, object]:
    input_values = {
        Path(input_drv).name: {"dynamicOutputs": {}, "outputs": input_outputs}
        for input_drv, input_outputs in (inputs or {}).items()
    }
    output_values = {
        output_name: {
            "path": Path(output(hash_character, f"{name}-{output_name}")).name
        }
        for output_name in outputs
    }
    return {
        "system": "x86_64-linux",
        "outputs": output_values,
        "inputs": {"drvs": input_values, "srcs": []},
    }


def target(name: str, order: int = 0) -> dict[str, object]:
    return {
        "id": f"x86_64-linux:{name}",
        "name": name,
        "system": "x86_64-linux",
        "runner": "ubuntu-24.04",
        "order": order,
    }


class GenerateLayersTest(unittest.TestCase):
    def test_layers_all_missing_internal_derivations(self) -> None:
        a = drv("a", "a")
        b = drv("b", "b")
        c = drv("c", "c")
        d = drv("d", "d")
        graph = {
            a: derivation("a", "e"),
            b: derivation("b", "f", {a: ["out"]}),
            c: derivation("c", "g", {a: ["out"]}),
            d: derivation("d", "h", {b: ["out"], c: ["out"]}),
        }
        targets = [target("d")]
        drv_paths = {"x86_64-linux:d": d}
        requested_outputs = {"x86_64-linux:d": {"out"}}

        missing, needed, owners = generate_layers.missing_derivations(
            targets, drv_paths, requested_outputs, graph, set()
        )
        nodes, dependencies = generate_layers.build_nodes(
            {"systems": [{"system": "x86_64-linux", "runner": "ubuntu-24.04"}]},
            graph,
            missing,
            needed,
            owners,
        )
        layers = generate_layers.topological_layers(nodes, dependencies)

        self.assertEqual(
            [[node["name"] for node in layer] for layer in layers],
            [["a"], ["b", "c"], ["d"]],
        )
        self.assertTrue(all(node["package"] == "d" for node in nodes))

    def test_cached_dependency_cuts_the_graph(self) -> None:
        a = drv("a", "a")
        b = drv("b", "b")
        graph = {
            a: derivation("a", "c"),
            b: derivation("b", "d", {a: ["out"]}),
        }
        targets = [target("b")]
        drv_paths = {"x86_64-linux:b": b}
        requested_outputs = {"x86_64-linux:b": {"out"}}
        cached = {output("c", "a-out")}

        missing, needed, owners = generate_layers.missing_derivations(
            targets, drv_paths, requested_outputs, graph, cached
        )

        self.assertEqual(missing, {b})
        self.assertEqual(needed[a], {"out"})
        self.assertEqual(owners[b]["name"], "b")

    def test_unions_outputs_from_multiple_roots(self) -> None:
        shared = drv("a", "shared")
        left = drv("b", "left")
        right = drv("c", "right")
        graph = {
            shared: derivation("shared", "d", outputs=("out", "dev")),
            left: derivation("left", "e", {shared: ["out"]}),
            right: derivation("right", "f", {shared: ["dev"]}),
        }
        targets = [target("left", 0), target("right", 1)]
        drv_paths = {
            "x86_64-linux:left": left,
            "x86_64-linux:right": right,
        }
        requested_outputs = {
            "x86_64-linux:left": {"out"},
            "x86_64-linux:right": {"out"},
        }
        cached = {output("d", "shared-out")}

        missing, needed, _ = generate_layers.missing_derivations(
            targets, drv_paths, requested_outputs, graph, cached
        )

        self.assertEqual(missing, {shared, left, right})
        self.assertEqual(needed[shared], {"out", "dev"})

    def test_cached_root_does_not_expand_inputs(self) -> None:
        dependency = drv("a", "dependency")
        root = drv("b", "root")
        graph = {
            dependency: derivation("dependency", "c"),
            root: derivation("root", "d", {dependency: ["out"]}),
        }
        targets = [target("root")]
        drv_paths = {"x86_64-linux:root": root}
        requested_outputs = {"x86_64-linux:root": {"out"}}
        cached = {output("d", "root-out")}

        missing, needed, _ = generate_layers.missing_derivations(
            targets, drv_paths, requested_outputs, graph, cached
        )

        self.assertEqual(missing, set())
        self.assertNotIn(dependency, needed)

    def test_narinfo_request_uses_explicit_user_agent(self) -> None:
        path = output("a", "missing")

        def reject_missing(request, timeout):
            self.assertEqual(
                request.get_header("User-agent"), generate_layers.NARINFO_USER_AGENT
            )
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                None,
            )

        with patch.object(generate_layers.urllib.request, "urlopen", reject_missing):
            self.assertFalse(
                generate_layers.narinfo_exists("https://cache.example", path)
            )

    def test_queries_cache_with_requested_worker_count(self) -> None:
        paths = [output("a", "a"), output("b", "b"), output("c", "c")]

        with patch.object(
            generate_layers,
            "narinfo_exists",
            side_effect=lambda _store, path: path != paths[1],
        ) as exists:
            result = generate_layers.query_cache(
                "https://cache.example", paths, workers=7
            )

        self.assertEqual(result, {paths[0], paths[2]})
        self.assertEqual(exists.call_count, 3)

    def test_rejects_unknown_cache_results(self) -> None:
        path = output("a", "unknown")

        with (
            patch.object(generate_layers, "narinfo_exists", return_value=None),
            self.assertRaisesRegex(RuntimeError, "unknown result for 1 paths"),
        ):
            generate_layers.query_cache("https://cache.example", [path], workers=1)

    def test_reads_nix_http_connection_setting(self) -> None:
        with patch.object(
            generate_layers,
            "run_json",
            return_value={"http-connections": {"value": 31, "defaultValue": 25}},
        ):
            self.assertEqual(generate_layers.nix_http_connections(), 31)

    def test_reads_fixed_output_path_from_environment(self) -> None:
        fixed_drv = drv("a", "source")
        fixed_output = output("b", "source")
        value = derivation("source", "b")
        value["outputs"] = {"out": {"hash": "sha256-example", "method": "flat"}}
        value["env"] = {"out": fixed_output}

        self.assertEqual(
            generate_layers.derivation_outputs(fixed_drv, value),
            {"out": fixed_output},
        )

    def test_rejects_dynamic_derivation_inputs(self) -> None:
        dependency = drv("a", "dependency")
        root = drv("b", "root")
        value = derivation("root", "c", {dependency: ["out"]})
        value["inputs"]["drvs"][Path(dependency).name]["dynamicOutputs"] = {"out": {}}

        with self.assertRaisesRegex(ValueError, "dynamic derivation"):
            generate_layers.derivation_inputs(root, value)

    def test_adds_disabled_matrices_for_unused_layers(self) -> None:
        node = {
            "name": "a",
            "package": "root",
            "ownerSystem": "x86_64-linux",
            "system": "x86_64-linux",
            "runner": "ubuntu-24.04",
            "derivation": drv("a", "a"),
            "outputs": "out",
            "inputs": "[]",
        }

        matrices = generate_layers.matrix_layers([[node]], 3, 10)

        self.assertTrue(matrices[0]["include"][0]["enabled"])
        self.assertFalse(matrices[1]["include"][0]["enabled"])
        self.assertFalse(matrices[2]["include"][0]["enabled"])

    def test_rejects_explicit_job_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs 2 jobs"):
            generate_layers.matrix_layers([[{}, {}]], 1, 1)

    def test_has_no_default_total_job_limit(self) -> None:
        node = {
            "name": "a",
            "package": "root",
            "ownerSystem": "x86_64-linux",
            "system": "x86_64-linux",
            "runner": "ubuntu-24.04",
            "derivation": drv("a", "a"),
            "outputs": "out",
            "inputs": "[]",
        }

        matrices = generate_layers.matrix_layers([[node] * 200, [node] * 126], 2, None)

        self.assertEqual(sum(len(matrix["include"]) for matrix in matrices), 326)


if __name__ == "__main__":
    unittest.main()
