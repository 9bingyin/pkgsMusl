#!/usr/bin/env python3

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import generate_layers


class GenerateLayersTest(unittest.TestCase):
    def test_builds_topological_layers(self) -> None:
        targets = [
            {
                "id": "x86_64-linux:a",
                "name": "a",
                "system": "x86_64-linux",
                "runner": "ubuntu-24.04",
                "order": 0,
            },
            {
                "id": "x86_64-linux:b",
                "name": "b",
                "system": "x86_64-linux",
                "runner": "ubuntu-24.04",
                "order": 1,
            },
            {
                "id": "x86_64-linux:c",
                "name": "c",
                "system": "x86_64-linux",
                "runner": "ubuntu-24.04",
                "order": 2,
            },
            {
                "id": "x86_64-linux:d",
                "name": "d",
                "system": "x86_64-linux",
                "runner": "ubuntu-24.04",
                "order": 3,
            },
        ]
        drv_paths = {
            "x86_64-linux:a": "/nix/store/a.drv",
            "x86_64-linux:b": "/nix/store/b.drv",
            "x86_64-linux:c": "/nix/store/c.drv",
            "x86_64-linux:d": "/nix/store/d.drv",
        }
        graph = {
            "/nix/store/a.drv": {"inputDrvs": {}},
            "/nix/store/b.drv": {"inputDrvs": {"/nix/store/a.drv": {}}},
            "/nix/store/c.drv": {"inputDrvs": {"/nix/store/a.drv": {}}},
            "/nix/store/d.drv": {
                "inputDrvs": {
                    "/nix/store/b.drv": {},
                    "/nix/store/c.drv": {},
                }
            },
        }

        canonical, dependencies = generate_layers.package_dependencies(
            targets, drv_paths, graph
        )
        layers = generate_layers.topological_layers(canonical, dependencies)

        self.assertEqual(
            [[target["name"] for target in layer] for layer in layers],
            [["a"], ["b", "c"], ["d"]],
        )

    def test_retries_nix_batch_query_by_bisection(self) -> None:
        paths = [f"/nix/store/{name}" for name in ["a", "b", "c", "d"]]
        responses = [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"error: path '{paths[1]}' is not valid",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({paths[0]: {}, paths[1]: None}),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({paths[2]: {}, paths[3]: None}),
                stderr="",
            ),
        ]

        with patch.object(
            generate_layers.subprocess, "run", side_effect=responses
        ) as run:
            result = generate_layers.query_cache("https://cache.example", paths)

        self.assertEqual(result, {paths[0], paths[2]})
        self.assertEqual(run.call_count, 3)
        first_command = run.call_args_list[0].args[0]
        self.assertNotIn("http-connections", first_command)
        self.assertNotIn("--max-jobs", first_command)

    def test_does_not_split_network_failures(self) -> None:
        paths = ["/nix/store/a", "/nix/store/b"]
        response = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="error: unable to download narinfo: TLS failure",
        )

        with patch.object(
            generate_layers.subprocess, "run", return_value=response
        ) as run:
            result = generate_layers.query_cache("https://cache.example", paths)

        self.assertEqual(result, set())
        self.assertEqual(run.call_count, 1)

    def test_prunes_only_fully_cached_targets(self) -> None:
        targets = [
            {"id": "system:a"},
            {"id": "system:b"},
            {"id": "system:c"},
        ]
        output_paths = {
            "system:a": ["/nix/store/a"],
            "system:b": ["/nix/store/b", "/nix/store/b-dev"],
            "system:c": [],
        }

        result = generate_layers.uncached_targets(
            targets,
            output_paths,
            {"/nix/store/a", "/nix/store/b"},
        )

        self.assertEqual([target["id"] for target in result], ["system:b", "system:c"])

    def test_compresses_layers_after_cache_pruning(self) -> None:
        targets = [
            {
                "id": "x86_64-linux:b",
                "name": "b",
                "system": "x86_64-linux",
                "runner": "ubuntu-24.04",
                "order": 1,
            }
        ]
        drv_paths = {"x86_64-linux:b": "/nix/store/b.drv"}
        graph = {
            "/nix/store/a.drv": {"inputDrvs": {}},
            "/nix/store/b.drv": {"inputDrvs": {"/nix/store/a.drv": {}}},
        }

        canonical, dependencies = generate_layers.package_dependencies(
            targets, drv_paths, graph
        )
        layers = generate_layers.topological_layers(canonical, dependencies)

        self.assertEqual(
            [[target["name"] for target in layer] for layer in layers], [["b"]]
        )

    def test_adds_disabled_matrices_for_unused_layers(self) -> None:
        layers = [
            [
                {
                    "name": "a",
                    "system": "x86_64-linux",
                    "runner": "ubuntu-24.04",
                }
            ]
        ]

        matrices = generate_layers.matrix_layers(layers, 3)

        self.assertTrue(matrices[0]["include"][0]["enabled"])
        self.assertFalse(matrices[1]["include"][0]["enabled"])
        self.assertFalse(matrices[2]["include"][0]["enabled"])

    def test_reads_current_derivation_json_inputs(self) -> None:
        self.assertEqual(
            generate_layers.input_drvs({"inputs": {"drvs": {"abc-package.drv": {}}}}),
            {"/nix/store/abc-package.drv"},
        )

    def test_rejects_too_many_layers(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs 2 layers"):
            generate_layers.matrix_layers([[], []], 1)


if __name__ == "__main__":
    unittest.main()
