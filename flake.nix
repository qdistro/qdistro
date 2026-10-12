{
  description = "qdistro component derivations — phase-0 native-stack spike";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/6510408d147d8c5e6d76043e899e9251756f6a75";

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          vendoredSrc = builtins.path {
            path = ./qdwin/libweston-vendored/src;
            filter =
              path: type:
              !(pkgs.lib.hasPrefix "build-" (baseNameOf path) || pkgs.lib.hasSuffix ".lock" path);
          };
        in
        {
          libweston-vendored = pkgs.callPackage ./nix/libweston-vendored.nix { };
          weston-qdistro = pkgs.callPackage ./nix/weston-qdistro.nix { inherit vendoredSrc; };
          qdwin = pkgs.callPackage ./nix/qdwin.nix { westonQdistro = self.packages.${system}.weston-qdistro; };
        }
      );
    };
}
