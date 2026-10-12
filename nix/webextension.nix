# qdistro browser-bridge WebExtensions (Chromium MV3 / Firefox MV3).
# Mirrors qci's host gate rows: `npm test && npm run build` with the drift
# env (_ext_drift_env) — QDISTRO_REQUIRE_SIBLING=1 and the sibling
# extension's golden-frames fixture, so the cross-extension wire-format
# drift test runs instead of skipping. Output: the unpacked dist tree plus
# the packed zip/xpi from scripts/build-extension.sh.
{
  lib,
  buildNpmPackage,
  nodejs,
  zip,
}:
{
  pname,
  src,
  npmDepsHash,
  siblingGolden,
  description,
  # Test files that cannot run in the nix sandbox; each needs a reason.
  excludeTests ? [ ],
}:

buildNpmPackage {
  inherit pname src npmDepsHash;
  version = (lib.importJSON (src + "/package.json")).version;

  inherit nodejs;
  nativeBuildInputs = [ zip ];

  postPatch = ''
    patchShebangs scripts
  '';

  # npm run build (default npmBuildScript) → scripts/build-extension.sh → dist/.
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export QDISTRO_REQUIRE_SIBLING=1
    export QDISTRO_SIBLING_GOLDEN=${siblingGolden}
    npm test -- ${lib.concatMapStringsSep " " (t: "--exclude ${t}") excludeTests}
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/qdistro/webextensions"
    cp -r dist/. "$out/share/qdistro/webextensions/${pname}"
    runHook postInstall
  '';

  meta.description = description;
}
