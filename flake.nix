{
  description = "qdistro component derivations — native stack, Python apps, WebExtensions";

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
          p = self.packages.${system};
          qtPytestCheck = import ./nix/qt-pytest-check.nix { inherit (pkgs) dbus qt6; };
          webExtension = pkgs.callPackage ./nix/webextension.nix { };
          pyApp =
            file: args:
            pkgs.callPackage file (
              {
                inherit qtPytestCheck;
              }
              // args
            );
        in
        {
          libweston-vendored = pkgs.callPackage ./nix/libweston-vendored.nix { };
          weston-qdistro = pkgs.callPackage ./nix/weston-qdistro.nix {
            vendoredSrc = vendoredSrcFor pkgs;
          };
          qdwin = pkgs.callPackage ./nix/qdwin.nix { westonQdistro = self.packages.${system}.weston-qdistro; };
          quickshell-vendored = pkgs.callPackage ./nix/quickshell-vendored.nix { };
          qdshell = pkgs.callPackage ./nix/qdshell.nix {
            inherit (p) qdwin;
            quickshell = p.quickshell-vendored;
          };
          daemons = pkgs.callPackage ./nix/daemons.nix { qdwin = self.packages.${system}.qdwin; };
          qdistro-presentation = pkgs.callPackage ./nix/qdistro-presentation.nix { };
          qdistro-app-sdk = pkgs.callPackage ./nix/qdistro-app-sdk.nix { };
          qtermwidget-py = pkgs.callPackage ./nix/qtermwidget-py.nix { };
          qfileman = pyApp ./nix/qfileman.nix {
            inherit (p) qdistro-presentation qdistro-app-sdk;
          };
          qterminator = pyApp ./nix/qterminator.nix {
            inherit (p) qdistro-presentation qdistro-app-sdk qtermwidget-py;
          };
          qnotebook = pyApp ./nix/qnotebook.nix {
            inherit (p) qdistro-presentation qdistro-app-sdk;
          };
          qdbrowser = pyApp ./nix/qdbrowser.nix {
            inherit (p) qdistro-presentation qdistro-app-sdk;
          };
          qdlocker = pyApp ./nix/qdlocker.nix { inherit (p) qdistro-presentation; };
          qdgreeter = pyApp ./nix/qdgreeter.nix { };

          qdchrome-extension = webExtension {
            pname = "qdchrome-extension";
            src = ./qdchrome-extension;
            npmDepsHash = "sha256-sSjhPh7eIZDGB8DFgZc2buIgJcThOOt9PEFwtC/gS0s=";
            siblingGolden = ./qdfirefox-extension/tests/fixtures/golden-frames.js;
            description = "qdistro Chromium MV3 native-messaging WebExtension";
          };
          qdfirefox-extension = webExtension {
            pname = "qdfirefox-extension";
            src = ./qdfirefox-extension;
            npmDepsHash = "sha256-7PqHW0RTNrGo/Zs2Xn/gXDPvRp7CyFVVi0oElukNTfk=";
            siblingGolden = ./qdchrome-extension/tests/fixtures/golden-frames.js;
            description = "qdistro Firefox MV3 native-messaging WebExtension";
            # Execs `bash` with PATH pinned to /usr/bin:/bin — absent in the
            # nix sandbox (ENOENT → status 1). It tests the FHS native-host
            # install script (/usr/lib/qdistro/browser-bridge), which is
            # install-chain/module scope, not the packaged extension.
            excludeTests = [ "tests/install-native-host.test.js" ];
          };
        }
      );

      # Every package (so `nix flake check` builds each one and runs its
      # checkPhase suite) plus the cross-package gates in nix/checks.nix.
      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        self.packages.${system}
        // import ./nix/checks.nix {
          inherit (pkgs)
            lib
            runCommand
            runtimeShell
            binutils
            coreutils
            dbus
            pkg-config
            python314
            wayland-scanner
            wayland-protocols
            ;
          apps = self.packages.${system};
          westonQdistro = self.packages.${system}.weston-qdistro;
          libwestonVendored = self.packages.${system}.libweston-vendored;
          qdwin = self.packages.${system}.qdwin;
        }
      );
    };
}
