pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "../Services/Theming/ColorPalette.js" as ColorPalette

/*
Qdshell is not strictly a Material Design project, it supports both some predefined
color schemes and dynamic color generation from the wallpaper.

We ultimately decided to use a restricted set of colors that follows the
Material Design 3 naming convention.

NOTE: All color names are prefixed with 'm' (e.g., mPrimary) to prevent QML from
misinterpreting them as signals (e.g., the 'onPrimary' property name).
*/
Singleton {
  id: root

  property bool reloadColors: false

  // Suppress transition animations until the first colors.json load completes
  property bool skipTransition: true

  // Flag indicating theme colors are currently transitioning (for widgets to disable their own animations)
  property bool isTransitioning: false

  // Committed (unanimated) target palette for first-party app export.
  property bool committingTarget: false
  property int nextRequestId: 0
  property int pendingRequestId: 0
  property int acceptedRequestId: 0
  property string pendingMode: ""
  property string acceptedMode: "dark"
  property var acceptedPalette: ({})
  signal acceptedTargetChanged(int requestId, string mode, var palette)

  // Timer to reset isTransitioning after animation completes
  Timer {
    id: transitionTimer
    interval: Style.animationSlowest + 50 // Small buffer after animation
    onTriggered: root.isTransitioning = false
  }

  // --- Key Colors: These are the main accent colors that define your app's style
  property color mPrimary: defaultColors.mPrimary
  property color mOnPrimary: defaultColors.mOnPrimary
  property color mSecondary: defaultColors.mSecondary
  property color mOnSecondary: defaultColors.mOnSecondary
  property color mTertiary: defaultColors.mTertiary
  property color mOnTertiary: defaultColors.mOnTertiary

  // --- Utility Colors: These colors serve specific, universal purposes like indicating errors
  property color mError: defaultColors.mError
  property color mOnError: defaultColors.mOnError

  // --- Surface and Variant Colors: These provide additional options for surfaces and their contents, creating visual hierarchy
  property color mSurface: defaultColors.mSurface
  property color mOnSurface: defaultColors.mOnSurface

  property color mSurfaceVariant: defaultColors.mSurfaceVariant
  property color mOnSurfaceVariant: defaultColors.mOnSurfaceVariant

  property color mOutline: defaultColors.mOutline
  property color mShadow: defaultColors.mShadow

  property color mHover: defaultColors.mHover
  property color mOnHover: defaultColors.mOnHover

  // --- Color transition animations ---
  Behavior on mPrimary {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOnPrimary {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mSecondary {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOnSecondary {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mTertiary {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOnTertiary {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mError {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOnError {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mSurface {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOnSurface {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mSurfaceVariant {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOnSurfaceVariant {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOutline {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mShadow {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mHover {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }
  Behavior on mOnHover {
    enabled: !root.skipTransition
    ColorAnimation {
      duration: Style.animationSlowest
      easing.type: Easing.OutCubic
    }
  }

  // Helper to start transition and update a color
  function startTransition() {
    root.isTransitioning = true;
    transitionTimer.restart();
  }

  // Update colors when customColorsData changes (imperative assignment enables Behavior animations)
  Connections {
    target: customColorsData
    function onMPrimaryChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mPrimary = customColorsData.mPrimary;
    }
    function onMOnPrimaryChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOnPrimary = customColorsData.mOnPrimary;
    }
    function onMSecondaryChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mSecondary = customColorsData.mSecondary;
    }
    function onMOnSecondaryChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOnSecondary = customColorsData.mOnSecondary;
    }
    function onMTertiaryChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mTertiary = customColorsData.mTertiary;
    }
    function onMOnTertiaryChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOnTertiary = customColorsData.mOnTertiary;
    }
    function onMErrorChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mError = customColorsData.mError;
    }
    function onMOnErrorChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOnError = customColorsData.mOnError;
    }
    function onMSurfaceChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mSurface = customColorsData.mSurface;
    }
    function onMOnSurfaceChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOnSurface = customColorsData.mOnSurface;
    }
    function onMSurfaceVariantChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mSurfaceVariant = customColorsData.mSurfaceVariant;
    }
    function onMOnSurfaceVariantChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOnSurfaceVariant = customColorsData.mOnSurfaceVariant;
    }
    function onMOutlineChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOutline = customColorsData.mOutline;
    }
    function onMShadowChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mShadow = customColorsData.mShadow;
    }
    function onMHoverChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mHover = customColorsData.mHover;
    }
    function onMOnHoverChanged() {
      if (!root.skipTransition && !root.committingTarget) {
        startTransition();
      }
      root.mOnHover = customColorsData.mOnHover;
    }
  }

  function resolveColorKey(key) {
    switch (key) {
    case "primary":
      return root.mPrimary;
    case "secondary":
      return root.mSecondary;
    case "tertiary":
      return root.mTertiary;
    case "error":
      return root.mError;
    default:
      return root.mOnSurface;
    }
  }

  function resolveOnColorKey(key) {
    switch (key) {
    case "primary":
      return root.mOnPrimary;
    case "secondary":
      return root.mOnSecondary;
    case "tertiary":
      return root.mOnTertiary;
    case "error":
      return root.mOnError;
    default:
      return root.mSurface;
    }
  }

  function resolveColorKeyOptional(key) {
    switch (key) {
    case "primary":
      return root.mPrimary;
    case "secondary":
      return root.mSecondary;
    case "tertiary":
      return root.mTertiary;
    case "error":
      return root.mError;
    default:
      return "transparent";
    }
  }

  readonly property var colorKeyModel: [
    {
      "key": "none",
      "name": I18n.tr("common.none")
    },
    {
      "key": "primary",
      "name": I18n.tr("common.primary")
    },
    {
      "key": "secondary",
      "name": I18n.tr("common.secondary")
    },
    {
      "key": "tertiary",
      "name": I18n.tr("common.tertiary")
    },
    {
      "key": "error",
      "name": I18n.tr("common.error")
    }
  ]

  function beginRequest(mode) {
    root.nextRequestId += 1;
    root.pendingRequestId = root.nextRequestId;
    root.pendingMode = (mode === "light") ? "light" : "dark";
    return root.pendingRequestId;
  }

  function cancelRequest(requestId) {
    if (requestId === root.pendingRequestId)
      root.pendingRequestId = root.acceptedRequestId;
  }

  function paletteFromAdapter() {
    return ColorPalette.completePalette({
                                          "mPrimary": customColorsData.mPrimary,
                                          "mOnPrimary": customColorsData.mOnPrimary,
                                          "mSecondary": customColorsData.mSecondary,
                                          "mOnSecondary": customColorsData.mOnSecondary,
                                          "mTertiary": customColorsData.mTertiary,
                                          "mOnTertiary": customColorsData.mOnTertiary,
                                          "mError": customColorsData.mError,
                                          "mOnError": customColorsData.mOnError,
                                          "mSurface": customColorsData.mSurface,
                                          "mOnSurface": customColorsData.mOnSurface,
                                          "mSurfaceVariant": customColorsData.mSurfaceVariant,
                                          "mOnSurfaceVariant": customColorsData.mOnSurfaceVariant,
                                          "mOutline": customColorsData.mOutline,
                                          "mShadow": customColorsData.mShadow,
                                          "mHover": customColorsData.mHover,
                                          "mOnHover": customColorsData.mOnHover
                                        });
  }

  function commitTargetPalette(requestId, mode, palette) {
    if (requestId !== root.pendingRequestId && requestId < root.pendingRequestId)
      return false;
    const pal = ColorPalette.completePalette(palette);
    if (!pal) {
      Logger.w("Color", "refusing incomplete target palette");
      return false;
    }
    root.committingTarget = true;
    customColorsData.mPrimary = pal.mPrimary;
    customColorsData.mOnPrimary = pal.mOnPrimary;
    customColorsData.mSecondary = pal.mSecondary;
    customColorsData.mOnSecondary = pal.mOnSecondary;
    customColorsData.mTertiary = pal.mTertiary;
    customColorsData.mOnTertiary = pal.mOnTertiary;
    customColorsData.mError = pal.mError;
    customColorsData.mOnError = pal.mOnError;
    customColorsData.mSurface = pal.mSurface;
    customColorsData.mOnSurface = pal.mOnSurface;
    customColorsData.mSurfaceVariant = pal.mSurfaceVariant;
    customColorsData.mOnSurfaceVariant = pal.mOnSurfaceVariant;
    customColorsData.mOutline = pal.mOutline;
    customColorsData.mShadow = pal.mShadow;
    customColorsData.mHover = pal.mHover;
    customColorsData.mOnHover = pal.mOnHover;
    root.committingTarget = false;
    if (!root.skipTransition)
      startTransition();
    root.acceptedRequestId = requestId;
    root.pendingRequestId = requestId;
    root.acceptedMode = (mode === "light") ? "light" : "dark";
    root.acceptedPalette = pal;
    root.acceptedTargetChanged(requestId, root.acceptedMode, pal);
    return true;
  }

  function _commitFromAdapterIfNeeded() {
    const pal = paletteFromAdapter();
    if (!pal)
      return;
    if (root.pendingRequestId !== root.acceptedRequestId) {
      commitTargetPalette(root.pendingRequestId, root.pendingMode, pal);
      return;
    }
    if (root.acceptedRequestId === 0) {
      const mode = (Settings.data.colorSchemes && Settings.data.colorSchemes.darkMode) ? "dark" : "light";
      commitTargetPalette(beginRequest(mode), mode, pal);
      return;
    }
    if (root.skipTransition)
      return;
    if (ColorPalette.palettesEqual(pal, root.acceptedPalette))
      return;
    const req = beginRequest(root.acceptedMode);
    commitTargetPalette(req, root.acceptedMode, pal);
  }

  // --------------------------------
  // Default colors: Qdshell (default) dark — must match Assets/ColorScheme/Qdshell-default
  QtObject {
    id: defaultColors

    readonly property color mPrimary: "#fff59b"
    readonly property color mOnPrimary: "#0e0e43"

    readonly property color mSecondary: "#a9aefe"
    readonly property color mOnSecondary: "#0e0e43"

    readonly property color mTertiary: "#9BFECE"
    readonly property color mOnTertiary: "#0e0e43"

    readonly property color mError: "#FD4663"
    readonly property color mOnError: "#0e0e43"

    readonly property color mSurface: "#070722"
    readonly property color mOnSurface: "#f3edf7"

    readonly property color mSurfaceVariant: "#11112d"
    readonly property color mOnSurfaceVariant: "#7c80b4"

    readonly property color mOutline: "#21215F"
    readonly property color mShadow: "#070722"

    readonly property color mHover: "#9BFECE"
    readonly property color mOnHover: "#0e0e43"
  }

  // ----------------------------------------------------------------
  // FileView to load custom colors data from colors.json
  FileView {
    id: customColorsFile
    path: Settings.directoriesCreated ? (Settings.configDir + "colors.json") : undefined
    printErrors: false
    watchChanges: true
    onFileChanged: {
      Logger.d("Color", "Reloading colors from disk");
      reloadColors = true;
      reload();
    }
    onAdapterUpdated: {
      Logger.d("Color", "Writing colors to disk");
      writeAdapter();
    }

    onLoaded: {
      if (root.skipTransition) {
        Qt.callLater(function () {
          root.skipTransition = false;
        });
      }
      Qt.callLater(function () {
        root._commitFromAdapterIfNeeded();
      });
    }

    // Trigger initial load when path changes from empty to actual path
    onPathChanged: {
      if (path !== undefined) {
        reload();
      }
    }
    onLoadFailed: function (error) {
      if (reloadColors) {
        reloadColors = false;
        return;
      }

      if (root.skipTransition) {
        Qt.callLater(function () {
          root.skipTransition = false;
        });
      }

      // Error code 2 = ENOENT (No such file or directory)
      if (error === 2 || error.toString().includes("No such file")) {
        // File doesn't exist, create it with default values
        writeAdapter();
      }
    }
    JsonAdapter {
      id: customColorsData

      property color mPrimary: defaultColors.mPrimary
      property color mOnPrimary: defaultColors.mOnPrimary

      property color mSecondary: defaultColors.mSecondary
      property color mOnSecondary: defaultColors.mOnSecondary

      property color mTertiary: defaultColors.mTertiary
      property color mOnTertiary: defaultColors.mOnTertiary

      property color mError: defaultColors.mError
      property color mOnError: defaultColors.mOnError

      property color mSurface: defaultColors.mSurface
      property color mOnSurface: defaultColors.mOnSurface

      property color mSurfaceVariant: defaultColors.mSurfaceVariant
      property color mOnSurfaceVariant: defaultColors.mOnSurfaceVariant

      property color mOutline: defaultColors.mOutline
      property color mShadow: defaultColors.mShadow

      property color mHover: defaultColors.mHover
      property color mOnHover: defaultColors.mOnHover
    }
  }
}
