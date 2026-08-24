{
  pkgs,
  plan,
  system,
}:
let
  inherit (pkgs) lib;

  muslPkgs = pkgs.pkgsMusl;
  stages = import ./musl-stages.nix { inherit muslPkgs; };

  entries = lib.concatLists (lib.attrValues plan.phases);
  enabledEntries = builtins.filter (
    entry: !entry ? systems || builtins.elem system entry.systems
  ) entries;

  names = map (entry: entry.name) enabledEntries;
  uniqueNames = lib.unique names;

  resolve =
    entry:
    let
      source = entry.source;
      packageSet =
        if source.kind == "musl" then
          muslPkgs
        else if source.kind == "stage" then
          stages.findStage source.stage muslPkgs
        else
          throw "unsupported cache package source kind '${source.kind}'";
    in
    lib.getAttrFromPath source.path packageSet;
in
assert lib.assertMsg (
  builtins.length names == builtins.length uniqueNames
) "cache package names must be unique";
lib.listToAttrs (
  map (entry: {
    inherit (entry) name;
    value = resolve entry;
  }) enabledEntries
)
