#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path

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
