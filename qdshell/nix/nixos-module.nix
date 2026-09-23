{
  config,
  lib,
  ...
}:
let
  cfg = config.services.qdshell-shell;
in
{
  options.services.qdshell-shell = {
    enable = lib.mkEnableOption "Qdshell shell systemd service";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The qdshell-shell package to use";
    };

    target = lib.mkOption {
      type = lib.types.str;
      default = "graphical-session.target";
      example = "hyprland-session.target";
      description = "The systemd target for the qdshell-shell service.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.qdshell-shell = {
      description = "Qdshell Shell - Wayland desktop shell";
      documentation = [ "https://docs.qdshell.dev" ];
      after = [ cfg.target ];
      partOf = [ cfg.target ];
      wantedBy = [ cfg.target ];
      restartTriggers = [ cfg.package ];

      environment = {
        PATH = lib.mkForce null;
      };

      serviceConfig = {
        ExecStart = lib.getExe cfg.package;
        Restart = "on-failure";
      };
    };

    environment.systemPackages = [ cfg.package ];
  };
}
