# The version declared in a meson.build's project() call: the first line of
# the form `  version: 'X',`.
file:
let
  lines = builtins.filter builtins.isString (builtins.split "\n" (builtins.readFile file));
  hits = builtins.filter (m: m != null)
    (map (builtins.match "[[:space:]]*version:[[:space:]]*'([^']+)',?[[:space:]]*") lines);
in
if hits == [ ] then throw "no project version in ${toString file}" else builtins.head (builtins.head hits)
