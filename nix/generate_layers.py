#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

DEFAULT_CACHE_STORES = [
    "https://cache.nixos.org",
    "https://cache.bingyin.org",
]
DEFAULT_HTTP_CONNECTIONS = 25
MAX_HTTP_CONNECTIONS = 64
NARINFO_USER_AGENT = "pkgsMusl-cache-planner/1.0"


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
    targets: list[dict[str, Any]] = []
    order = 0

    for system_entry in plan["systems"]:
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


def evaluate_targets(
    flake: str,
    targets: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    drv_paths: dict[str, str] = {}
    requested_outputs: dict[str, set[str]] = {}
    systems = list(dict.fromkeys(target["system"] for target in targets))

    for system in systems:
        system_targets = [target for target in targets if target["system"] == system]
        names = [target["name"] for target in system_targets]
        names_json = json.dumps(names, separators=(",", ":"))
        expression = (
            "packages: "
            f"let names = builtins.fromJSON {json.dumps(names_json)}; in "
            "builtins.listToAttrs (map (name: let "
            "package = builtins.getAttr name packages; "
            'outputs = package.meta.outputsToInstall or [ (package.outputName or "out") ]; '
            "in { inherit name; value = { "
            "drvPath = package.drvPath; "
            "outputNames = outputs; "
            "}; }) names)"
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
            value = evaluated.get(target["name"])
            if not isinstance(value, dict):
                raise TypeError(f"missing evaluation result for {target['id']}")
            drv_path = value.get("drvPath")
            outputs = value.get("outputNames")
            if not isinstance(drv_path, str):
                raise TypeError(f"missing drvPath for {target['id']}")
            if not isinstance(outputs, list) or not all(
                isinstance(output, str) for output in outputs
            ):
                raise TypeError(f"missing output names for {target['id']}")
            if not outputs:
                raise ValueError(f"target {target['id']} requests no outputs")
            drv_paths[target["id"]] = drv_path
            requested_outputs[target["id"]] = set(outputs)

    return drv_paths, requested_outputs


def store_path(value: str) -> str:
    if value.startswith("/"):
        return value
    return f"/nix/store/{value}"


def derivation_graph(drv_paths: Iterable[str]) -> dict[str, Any]:
    unique_paths = list(dict.fromkeys(drv_paths))
    value = run_json(["nix", "derivation", "show", "--recursive", *unique_paths])
    derivations = value.get("derivations")
    if isinstance(derivations, dict):
        return {
            store_path(path): derivation for path, derivation in derivations.items()
        }
    return value


def derivation_outputs(drv_path: str, derivation: dict[str, Any]) -> dict[str, str]:
    outputs = derivation.get("outputs", {})
    if not isinstance(outputs, dict):
        raise TypeError(f"derivation {drv_path} has invalid outputs")

    environment = derivation.get("env", {})
    if not isinstance(environment, dict):
        raise TypeError(f"derivation {drv_path} has invalid environment")

    result: dict[str, str] = {}
    for name, value in outputs.items():
        path: Any
        if isinstance(value, dict):
            path = value.get("path")
        else:
            path = value
        if path is None:
            environment_path = environment.get(name)
            if isinstance(environment_path, str) and environment_path.startswith(
                "/nix/store/"
            ):
                path = environment_path
        if path is None:
            continue
        if not isinstance(path, str):
            raise TypeError(f"derivation {drv_path} output {name} has invalid path")
        result[name] = store_path(path)
    return result


def derivation_inputs(drv_path: str, derivation: dict[str, Any]) -> dict[str, set[str]]:
    inputs = derivation.get("inputDrvs")
    if inputs is None:
        nested_inputs = derivation.get("inputs", {})
        if not isinstance(nested_inputs, dict):
            raise TypeError(f"derivation {drv_path} has invalid inputs")
        inputs = nested_inputs.get("drvs", {})
    if not isinstance(inputs, dict):
        raise TypeError(f"derivation {drv_path} has invalid inputDrvs")

    result: dict[str, set[str]] = {}
    for input_path, value in inputs.items():
        outputs: Any
        dynamic_outputs: Any = {}
        if isinstance(value, dict):
            outputs = value.get("outputs", [])
            dynamic_outputs = value.get("dynamicOutputs", {})
        else:
            outputs = value
        if dynamic_outputs:
            raise ValueError(
                f"unsupported dynamic derivation input {input_path} in {drv_path}"
            )
        if not isinstance(outputs, list) or not all(
            isinstance(output, str) for output in outputs
        ):
            raise TypeError(
                f"derivation {drv_path} has invalid outputs for {input_path}"
            )
        if not outputs:
            raise ValueError(
                f"derivation {drv_path} requests no outputs from {input_path}"
            )
        result[store_path(input_path)] = set(outputs)
    return result


def derivation_sources(drv_path: str, derivation: dict[str, Any]) -> set[str]:
    sources = derivation.get("inputSrcs")
    if sources is None:
        inputs = derivation.get("inputs", {})
        if not isinstance(inputs, dict):
            raise TypeError(f"derivation {drv_path} has invalid inputs")
        sources = inputs.get("srcs", [])
    if not isinstance(sources, list) or not all(
        isinstance(source, str) for source in sources
    ):
        raise TypeError(f"derivation {drv_path} has invalid input sources")
    return {store_path(source) for source in sources}


def output_paths(
    drv_path: str,
    output_names: set[str],
    graph: dict[str, Any],
) -> set[str]:
    derivation = graph.get(drv_path)
    if derivation is None:
        raise ValueError(f"derivation graph does not contain {drv_path}")
    if not isinstance(derivation, dict):
        raise TypeError(f"derivation graph entry {drv_path} is not an object")
    outputs = derivation_outputs(drv_path, derivation)
    missing_names = output_names - outputs.keys()
    if missing_names:
        raise ValueError(
            f"derivation {drv_path} has dynamic or unknown outputs: {sorted(missing_names)}"
        )
    return {outputs[name] for name in output_names}


def required_cache_paths(
    targets: list[dict[str, Any]],
    drv_paths: dict[str, str],
    requested_outputs: dict[str, set[str]],
    graph: dict[str, Any],
) -> set[str]:
    paths: set[str] = set()
    for target in targets:
        target_id = target["id"]
        paths.update(
            output_paths(drv_paths[target_id], requested_outputs[target_id], graph)
        )

    for drv_path, derivation in graph.items():
        if not isinstance(derivation, dict):
            raise TypeError(f"derivation graph entry {drv_path} is not an object")
        for input_drv, outputs in derivation_inputs(drv_path, derivation).items():
            paths.update(output_paths(input_drv, outputs, graph))
    return paths


def nix_http_connections() -> int:
    try:
        config = run_json(["nix", "config", "show", "--json"])
        setting = config.get("http-connections", {})
        if isinstance(setting, dict):
            value = setting.get("value", setting.get("defaultValue"))
            if isinstance(value, int):
                if value == 0:
                    return MAX_HTTP_CONNECTIONS
                return max(1, min(value, MAX_HTTP_CONNECTIONS))
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        pass
    return DEFAULT_HTTP_CONNECTIONS


def narinfo_url(store: str, path: str) -> str:
    store_hash = Path(path).name.split("-", 1)[0]
    if len(store_hash) != 32:
        raise ValueError(f"invalid Nix store path: {path}")
    return f"{store.rstrip('/')}/{store_hash}.narinfo"


def narinfo_exists(store: str, path: str, timeout: float = 15.0) -> bool | None:
    url = narinfo_url(store, path)
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": NARINFO_USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status in {None, 200}
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 404:
                return False
            if status == 405:
                try:
                    get_request = urllib.request.Request(
                        url,
                        method="GET",
                        headers={"User-Agent": NARINFO_USER_AGENT},
                    )
                    with urllib.request.urlopen(
                        get_request, timeout=timeout
                    ) as response:
                        return response.status in {None, 200}
                except urllib.error.HTTPError as get_error:
                    get_status = get_error.code
                    get_error.close()
                    if get_status == 404:
                        return False
                    if get_status not in {429, 500, 502, 503, 504}:
                        return None
                except urllib.error.URLError:
                    return None
            elif status not in {429, 500, 502, 503, 504}:
                return None
        except urllib.error.URLError:
            if attempt == 1:
                return None
        if attempt == 0:
            time.sleep(0.5)
    return None


def query_cache(store: str, paths: Iterable[str], workers: int) -> set[str]:
    unique_paths = sorted(set(paths))
    if not unique_paths:
        return set()

    print(
        f"+ query {store} ({len(unique_paths)} paths, {workers} connections)",
        file=sys.stderr,
    )
    cached: set[str] = set()
    unknown = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = executor.map(lambda path: narinfo_exists(store, path), unique_paths)
        for path, result in zip(unique_paths, results, strict=True):
            if result is True:
                cached.add(path)
            elif result is None:
                unknown += 1

    if unknown:
        raise RuntimeError(
            f"{store} returned an unknown result for {unknown} paths; "
            "refusing to treat cache failures as misses"
        )
    return cached


def cached_paths(
    stores: list[str],
    paths: Iterable[str],
    workers: int,
) -> set[str]:
    missing = set(paths)
    cached: set[str] = set()

    for store in stores:
        found = query_cache(store, missing, workers)
        cached.update(found)
        missing.difference_update(found)
        print(
            f"cache {store}: found {len(found)}, remaining {len(missing)}",
            file=sys.stderr,
        )
        if not missing:
            break
    return cached


def missing_derivations(
    targets: list[dict[str, Any]],
    drv_paths: dict[str, str],
    requested_outputs: dict[str, set[str]],
    graph: dict[str, Any],
    cached: set[str],
) -> tuple[set[str], dict[str, set[str]], dict[str, dict[str, Any]]]:
    target_by_id = {target["id"]: target for target in targets}
    needed_outputs: dict[str, set[str]] = {}
    owner_by_drv: dict[str, dict[str, Any]] = {}
    pending: deque[str] = deque()

    for target in targets:
        target_id = target["id"]
        drv_path = drv_paths[target_id]
        needed_outputs.setdefault(drv_path, set()).update(requested_outputs[target_id])
        owner_by_drv.setdefault(drv_path, target_by_id[target_id])
        pending.append(drv_path)

    missing: set[str] = set()
    expanded: set[str] = set()
    while pending:
        drv_path = pending.popleft()
        required = needed_outputs[drv_path]
        if output_paths(drv_path, required, graph).issubset(cached):
            continue

        missing.add(drv_path)
        if drv_path in expanded:
            continue
        expanded.add(drv_path)

        derivation = graph[drv_path]
        for input_drv, outputs in derivation_inputs(drv_path, derivation).items():
            before = len(needed_outputs.get(input_drv, set()))
            needed_outputs.setdefault(input_drv, set()).update(outputs)
            owner_by_drv.setdefault(input_drv, owner_by_drv[drv_path])
            if len(needed_outputs[input_drv]) != before or input_drv not in expanded:
                pending.append(input_drv)

    print(
        f"cache pruning: {len(graph) - len(missing)} cached or unreachable, {len(missing)} derivations to build",
        file=sys.stderr,
    )
    return missing, needed_outputs, owner_by_drv


def derived_path(drv_path: str, outputs: set[str]) -> str:
    return f"{drv_path}^{','.join(sorted(outputs))}"


def build_nodes(
    plan: dict[str, Any],
    graph: dict[str, Any],
    missing: set[str],
    needed_outputs: dict[str, set[str]],
    owner_by_drv: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    runner_by_system = {
        system["system"]: system["runner"] for system in plan["systems"]
    }
    graph_order = {drv_path: index for index, drv_path in enumerate(graph)}
    nodes: list[dict[str, Any]] = []
    dependencies: dict[str, set[str]] = {}

    for drv_path in missing:
        derivation = graph[drv_path]
        owner = owner_by_drv[drv_path]
        drv_system = derivation.get("system", owner["system"])
        if drv_system in runner_by_system:
            runner = runner_by_system[drv_system]
            system = drv_system
        elif drv_system == "builtin":
            runner = owner["runner"]
            system = owner["system"]
        else:
            raise ValueError(
                f"no GitHub runner configured for derivation system {drv_system}"
            )

        inputs = derivation_inputs(drv_path, derivation)
        direct_inputs = [
            derived_path(input_drv, outputs)
            for input_drv, outputs in sorted(inputs.items())
        ]
        direct_inputs.extend(sorted(derivation_sources(drv_path, derivation)))
        basename = Path(drv_path).name
        display_name = basename.split("-", 1)[1].removesuffix(".drv")
        nodes.append(
            {
                "id": drv_path,
                "name": display_name,
                "package": owner["name"],
                "ownerSystem": owner["system"],
                "system": system,
                "runner": runner,
                "derivation": drv_path,
                "outputs": ",".join(sorted(needed_outputs[drv_path])),
                "inputs": json.dumps(direct_inputs, separators=(",", ":")),
                "order": owner["order"] * 1_000_000 + graph_order[drv_path],
            }
        )
        dependencies[drv_path] = set(inputs) & missing

    return nodes, dependencies


def topological_layers(
    nodes: list[dict[str, Any]],
    dependencies: dict[str, set[str]],
) -> list[list[dict[str, Any]]]:
    by_id = {node["id"]: node for node in nodes}
    remaining = {node_id: set(values) for node_id, values in dependencies.items()}
    layers: list[list[dict[str, Any]]] = []

    while remaining:
        ready_ids = [node_id for node_id, values in remaining.items() if not values]
        if not ready_ids:
            cycle = {node_id: sorted(values) for node_id, values in remaining.items()}
            raise ValueError(f"derivation dependency graph contains a cycle: {cycle}")

        ready_ids.sort(key=lambda node_id: by_id[node_id]["order"])
        layers.append([by_id[node_id] for node_id in ready_ids])
        ready_set = set(ready_ids)
        remaining = {
            node_id: values - ready_set
            for node_id, values in remaining.items()
            if node_id not in ready_set
        }

    return layers


def matrix_layers(
    layers: list[list[dict[str, Any]]],
    max_layers: int,
    max_jobs: int | None,
) -> list[dict[str, Any]]:
    job_count = sum(len(layer) for layer in layers)
    if max_jobs is not None and job_count > max_jobs:
        raise ValueError(
            f"dependency graph needs {job_count} jobs, workflow supports {max_jobs}"
        )
    if len(layers) > max_layers:
        raise ValueError(
            f"dependency graph needs {len(layers)} layers, workflow supports {max_layers}"
        )
    if any(len(layer) > 256 for layer in layers):
        raise ValueError("a dependency layer exceeds GitHub's 256-job matrix limit")

    matrices: list[dict[str, Any]] = []
    for index in range(max_layers):
        if index < len(layers):
            include = [
                {
                    "enabled": True,
                    "name": node["name"],
                    "package": node["package"],
                    "ownerSystem": node["ownerSystem"],
                    "system": node["system"],
                    "runner": node["runner"],
                    "derivation": node["derivation"],
                    "outputs": node["outputs"],
                    "inputs": node["inputs"],
                }
                for node in layers[index]
            ]
        else:
            include = [
                {
                    "enabled": False,
                    "name": "unused",
                    "package": "unused",
                    "ownerSystem": "none",
                    "system": "none",
                    "runner": "ubuntu-24.04",
                    "derivation": "",
                    "outputs": "",
                    "inputs": "[]",
                }
            ]
        matrices.append({"include": include})
    return matrices


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate dependency-ordered matrices")
    parser.add_argument("--plan", type=Path, default=Path("nix/cache-plan.json"))
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--flake", default=".")
    parser.add_argument("--max-layers", type=int, default=64)
    parser.add_argument(
        "--max-jobs",
        type=int,
        help="optional total job safety limit; each layer is always limited to 256 jobs",
    )
    parser.add_argument("--cache-store", action="append", dest="cache_stores")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plan = json.loads(args.plan.read_text())
    targets = workflow_targets(plan, args.workflow)
    drv_paths, requested_outputs = evaluate_targets(args.flake, targets)
    graph = derivation_graph(drv_paths.values())
    paths = required_cache_paths(targets, drv_paths, requested_outputs, graph)
    workers = nix_http_connections()
    stores = args.cache_stores or DEFAULT_CACHE_STORES
    cached = cached_paths(stores, paths, workers)
    missing, needed_outputs, owner_by_drv = missing_derivations(
        targets,
        drv_paths,
        requested_outputs,
        graph,
        cached,
    )
    nodes, dependencies = build_nodes(
        plan,
        graph,
        missing,
        needed_outputs,
        owner_by_drv,
    )
    layers = topological_layers(nodes, dependencies)
    matrices = matrix_layers(layers, args.max_layers, args.max_jobs)

    counts = [len(layer) for layer in layers]
    print(
        f"generated {len(layers)} layers for {len(nodes)} derivations: {counts}",
        file=sys.stderr,
    )
    print(json.dumps(matrices, separators=(",", ":")))


if __name__ == "__main__":
    main()
