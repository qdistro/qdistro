{
  description = "qdistro component derivations — phase-0 native-stack spike";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/6510408d147d8c5e6d76043e899e9251756f6a75";

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: nixpkgs.legacyPackages.${system};
      vendoredSrcFor =
        pkgs:
        builtins.path {
          path = ./qdwin/libweston-vendored/src;
          filter =
            path: type:
            !(pkgs.lib.hasPrefix "build" (baseNameOf path) || pkgs.lib.hasSuffix ".lock" path);
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          libweston-vendored = pkgs.callPackage ./nix/libweston-vendored.nix { };
          weston-qdistro = pkgs.callPackage ./nix/weston-qdistro.nix {
            vendoredSrc = vendoredSrcFor pkgs;
          };
          qdwin = pkgs.callPackage ./nix/qdwin.nix { westonQdistro = self.packages.${system}.weston-qdistro; };
          quickshell-vendored = pkgs.callPackage ./nix/quickshell-vendored.nix { };
          qdshell = pkgs.callPackage ./nix/qdshell.nix { qdwin = self.packages.${system}.qdwin; };
          daemons = pkgs.callPackage ./nix/daemons.nix { qdwin = self.packages.${system}.qdwin; };
          qdistro-presentation = pkgs.callPackage ./nix/qdistro-presentation.nix { };
          qdistro-app-sdk = pkgs.callPackage ./nix/qdistro-app-sdk.nix { };
          qfileman = pkgs.callPackage ./nix/qfileman.nix {
            qdistro-presentation = self.packages.${system}.qdistro-presentation;
            qdistro-app-sdk = self.packages.${system}.qdistro-app-sdk;
          };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        import ./nix/checks.nix {
          inherit (pkgs) lib runCommand binutils;
          westonQdistro = self.packages.${system}.weston-qdistro;
          libwestonVendored = self.packages.${system}.libweston-vendored;
          qdwin = self.packages.${system}.qdwin;
        }
      );
    };
}
