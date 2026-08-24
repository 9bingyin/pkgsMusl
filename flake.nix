{
  description = "Build and cache nixpkgs musl toolchains";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/master";

    flake-parts.url = "github:hercules-ci/flake-parts";

    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    let
      plan = builtins.fromJSON (builtins.readFile ./nix/cache-plan.json);
    in
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [
        inputs.treefmt-nix.flakeModule
      ];

      systems = map (entry: entry.system) plan.systems;

      perSystem =
        { pkgs, system, ... }:
        {
          packages = import ./nix/cache-packages.nix {
            inherit pkgs plan system;
          };

          treefmt.programs = {
            nixfmt.enable = true;
            yamlfmt.enable = true;
          };
        };
    };
}
