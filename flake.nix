{
  description = "Build and cache nixpkgs musl toolchains";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    nixpkgs-build.url = "github:NixOS/nixpkgs/master";

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
        let
          buildPkgs = import inputs.nixpkgs-build { inherit system; };
        in
        {
          packages = import ./nix/cache-packages.nix {
            pkgs = buildPkgs;
            inherit plan system;
          };

          treefmt.programs = {
            nixfmt.enable = true;
            yamlfmt.enable = true;
          };
        };
    };
}
