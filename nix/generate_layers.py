#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def run_json(command: list[str]) -> dict[str, Any]:
    print("+ " + " ".join(command), file=sys.stderr)
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise TypeError(f"command returned {type(value).__name__}, expected object")
    return value


def workflow_targets(plan: dict[str, Any], workflow: str) -> list[dict[str, Any]]:
    phases = plan["workflows"][workflow]
    systems = plan["systems"]
    targets: list[dict[str, Any]] = []
    order = 0

    for system_entry in systems:
        system = system_entry["system"]
        runner = system_entry["runner"]
        for phase in phases:
            for package in plan["phases"][phase]:
                supported_systems = package.get("systems", [system])
                if system not in supported_systems:
                    continue
                targets.append(
                    {
                        "id": f"{system}:{package['name']}",
                        "name": package["name"],
                        "system": system,
                        "runner": runner,
                        "order": order,
                    }
                )
                order += 1

    ids = [target["id"] for target in targets]
    if len(ids) != len(set(ids)):
        raise ValueError(f"workflow {workflow!r} contains duplicate package targets")
    return targets


def evaluate_drv_paths(
    flake: str,
    targets: list[dict[str, Any]],
) -> dict[str, str]:
    drv_paths: dict[str, str] = {}
    systems = list(dict.fromkeys(target["system"] for target in targets))

    for system in systems:
        system_targets = [target for target in targets if target["system"] == system]
        names = [target["name"] for target in system_targets]
        names_json = json.dumps(names, separators=(",", ":"))
        expression = (
            "packages: "
            f"let names = builtins.fromJSON {json.dumps(names_json)}; in "
            "builtins.listToAttrs (map (name: { "
            "inherit name; "
            "value = (builtins.getAttr name packages).drvPath; "
            "}) names)"
        )
        evaluated = run_json(
            [
                "nix",
                "eval",
                "--json",
                "--apply",
                expression,
                f"{flake}#packages.{system}",
            ]
        )
        for target in system_targets:
            drv_path = evaluated.get(target["name"])
            if not isinstance(drv_path, str):
                raise TypeError(f"missing drvPath for {target['id']}")
            drv_paths[target["id"]] = drv_path

    return drv_paths


def store_path(value: str) -> str:
    if value.startswith("/"):
        return value
    return f"/nix/store/{value}"


def derivation_graph(drv_paths: Iterable[str]) -> dict[str, Any]:
    unique_paths = list(dict.fromkeys(drv_paths))
    value = run_json(["nix", "derivation", "show", "--recursive", *unique_paths])
    derivations = value.get("derivations")
    if isinstance(derivations, dict):
        return {store_path(path): derivation for path, derivation in derivations.items()}
    return value


def input_drvs(derivation: dict[str, Any]) -> set[str]:
    inputs = derivation.get("inputDrvs")
    if inputs is None:
        nested_inputs = derivation.get("inputs", {})
        if isinstance(nested_inputs, dict):
            inputs = nested_inputs.get("drvs", {})
        else:
            inputs = {}
    if isinstance(inputs, dict):
        return {store_path(value) for value in inputs}
    if isinstance(inputs, list):
        return {store_path(value) for value in inputs if isinstance(value, str)}
    raise ValueError(f"unsupported inputDrvs value: {type(inputs).__name__}")


def reachable_drvs(root: str, graph: dict[str, Any]) -> set[str]:
    reached: set[str] = set()
    pending = list(input_drvs(graph[root]))

    while pending:
        drv_path = pending.pop()
        if drv_path in reached:
            continue
        reached.add(drv_path)
        derivation = graph.get(drv_path)
        if isinstance(derivation, dict):
            pending.extend(input_drvs(derivation))

    return reached


def package_dependencies(
    targets: list[dict[str, Any]],
    drv_paths: dict[str, str],
    graph: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    canonical_targets: list[dict[str, Any]] = []
    canonical_by_system_drv: dict[tuple[str, str], str] = {}

    for target in targets:
        drv_path = drv_paths[target["id"]]
        key = (target["system"], drv_path)
        if key in canonical_by_system_drv:
            print(
                f"deduplicating {target['id']} with {canonical_by_system_drv[key]} at {drv_path}",
                file=sys.stderr,
            )
            continue
        canonical_by_system_drv[key] = target["id"]
        canonical_targets.append(target)

    dependencies: dict[str, set[str]] = {}
    for target in canonical_targets:
        target_id = target["id"]
        root_drv = drv_paths[target_id]
        if root_drv not in graph:
            raise ValueError(f"derivation graph does not contain {root_drv}")
        reachable = reachable_drvs(root_drv, graph)
        dependencies[target_id] = {
            dependency_id
            for (system, drv_path), dependency_id in canonical_by_system_drv.items()
            if system == target["system"] and drv_path in reachable
        }

    return canonical_targets, dependencies


def topological_layers(
    targets: list[dict[str, Any]],
    dependencies: dict[str, set[str]],
) -> list[list[dict[str, Any]]]:
    by_id = {target["id"]: target for target in targets}
    remaining = {target_id: set(values) for target_id, values in dependencies.items()}
    layers: list[list[dict[str, Any]]] = []

    while remaining:
        ready_ids = [target_id for target_id, values in remaining.items() if not values]
        if not ready_ids:
            cycle = {target_id: sorted(values) for target_id, values in remaining.items()}
            raise ValueError(f"package dependency graph contains a cycle: {cycle}")

        ready_ids.sort(key=lambda target_id: by_id[target_id]["order"])
        layers.append([by_id[target_id] for target_id in ready_ids])
        ready_set = set(ready_ids)
        remaining = {
            target_id: values - ready_set
            for target_id, values in remaining.items()
            if target_id not in ready_set
        }

    return layers


def matrix_layers(
    layers: list[list[dict[str, Any]]],
    max_layers: int,
) -> list[dict[str, Any]]:
    if len(layers) > max_layers:
        raise ValueError(
            f"dependency graph needs {len(layers)} layers, workflow supports {max_layers}"
        )

    matrices: list[dict[str, Any]] = []
    for index in range(max_layers):
        if index < len(layers):
            include = [
                {
                    "enabled": True,
                    "package": target["name"],
                    "system": target["system"],
                    "runner": target["runner"],
                }
                for target in layers[index]
            ]
        else:
            include = [
                {
                    "enabled": False,
                    "package": "unused",
                    "system": "none",
                    "runner": "ubuntu-24.04",
                }
            ]
        matrices.append({"include": include})

    return matrices


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate dependency-ordered matrices")
    parser.add_argument("--plan", type=Path, default=Path("nix/cache-plan.json"))
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--flake", default=".")
    parser.add_argument("--max-layers", type=int, default=32)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plan = json.loads(args.plan.read_text())
    targets = workflow_targets(plan, args.workflow)
    drv_paths = evaluate_drv_paths(args.flake, targets)
    graph = derivation_graph(drv_paths.values())
    canonical_targets, dependencies = package_dependencies(targets, drv_paths, graph)
    layers = topological_layers(canonical_targets, dependencies)
    matrices = matrix_layers(layers, args.max_layers)

    counts = [len(layer) for layer in layers]
    print(
        f"generated {len(layers)} layers for {len(canonical_targets)} targets: {counts}",
        file=sys.stderr,
    )
    print(json.dumps(matrices, separators=(",", ":")))


if __name__ == "__main__":
    main()
