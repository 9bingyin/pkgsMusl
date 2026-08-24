{ muslPkgs }:
let
  findStage =
    expectedName: packageSet:
    let
      stdenvName = packageSet.stdenv.name or "unknown";
    in
    if stdenvName == expectedName then
      packageSet
    else if packageSet.stdenv ? __bootPackages then
      findStage expectedName packageSet.stdenv.__bootPackages
    else
      throw "musl bootstrap stage '${expectedName}' was not found; stopped at '${stdenvName}'";
in
{
  inherit findStage;
}
