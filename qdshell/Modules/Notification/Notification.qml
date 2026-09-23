import QtQuick
import QtQuick.Effects
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Notifications
import Quickshell.Wayland
import Quickshell.Widgets
import qs.Commons
import qs.Services.System
import qs.Widgets
import "NotificationLayout.js" as NotificationLayout
import "NotificationTheme.js" as NotificationTheme

// Simple notification popup - displays multiple notifications
Variants {
  // If no notification display activated in settings, then show them all
  model: Quickshell.screens.filter(screen => (Settings.data.notifications.monitors.includes(screen.name) || (Settings.data.notifications.monitors.length === 0)))

  delegate: Loader {
    id: root

    required property ShellScreen modelData

    property ListModel notificationModel: NotificationService.activeList

    // Deferred activation to prevent re-entrant QML incubation crash.
    // Direct binding to notificationModel.count would activate the Loader
    // synchronously during ListModel.insert(), causing nested incubation
    // (Loader + inner Repeater both processing the model) which crashes
    // the V4 engine in QV4::Object::insertMember.
    property bool shouldBeActive: false
    active: shouldBeActive || delayTimer.running

    // Keep loader active briefly after last notification to allow animations to complete
    Timer {
      id: delayTimer
      interval: Style.animationSlow + 200
      repeat: false
    }

    // Deferred activation timer - activates Loader on next event loop iteration
    Timer {
      id: activationTimer
      interval: 0
      repeat: false
      onTriggered: root.shouldBeActive = true
    }

    Connections {
      target: notificationModel
      function onCountChanged() {
        if (notificationModel.count > 0) {
          if (!root.shouldBeActive) {
            activationTimer.restart();
          }
        } else if (root.shouldBeActive) {
          root.shouldBeActive = false;
          delayTimer.restart();
        }
      }
    }

    sourceComponent: PanelWindow {
      id: notifWindow
      screen: modelData

      WlrLayershell.namespace: "qdshell-notifications-" + (screen?.name || "unknown")
      WlrLayershell.layer: (Settings.data.notifications?.overlayLayer) ? WlrLayer.Overlay : WlrLayer.Top
      WlrLayershell.exclusionMode: ExclusionMode.Ignore

      color: "transparent"

      // Make shadow area click-through, only notification content is clickable
      mask: Region {
        x: 0
        y: 0
        width: notifWindow.width
        height: notifWindow.height
        intersection: Intersection.Xor

        Region {
          // The clickable content area is inset by shadowPadding from all edges
          x: notifWindow.shadowPadding
          y: notifWindow.shadowPadding
          width: notifWindow.notifWidth
          height: Math.max(0, notifWindow.height - notifWindow.shadowPadding * 2)
          intersection: Intersection.Subtract
        }
      }

      // Parse location setting
      readonly property string location: Settings.data.notifications.location || "top_right"
      readonly property bool isTop: location.startsWith("top")
      readonly property bool isBottom: location.startsWith("bottom")
      readonly property bool isLeft: location.endsWith("_left")
      readonly property bool isRight: location.endsWith("_right")
      readonly property bool isCentered: location === "top" || location === "bottom"

      readonly property string barPos: Settings.getBarPositionForScreen(notifWindow.screen?.name)
      readonly property bool isFloating: Settings.data.bar.floating
      readonly property real barHeight: Style.getBarHeightForScreen(notifWindow.screen?.name)

      readonly property bool isFramed: Settings.data.bar.barType === "framed"
      readonly property real frameThickness: Settings.data.bar.frameThickness ?? 8

      // Detail mode: "compact" (title only) | "normal" (title + body) |
      // "detailed" (title + body + actions + timestamp).
      readonly property string detailMode: NotificationLayout.sanitizeDetailMode(Settings.data.notifications.detailMode)
      // "compact" layout is driven either by the density preset or by the
      // compact detail mode.
      readonly property bool isCompact: Settings.data.notifications.density === "compact" || detailMode === NotificationLayout.MODE_COMPACT
      readonly property bool showBody: NotificationLayout.showBody(detailMode)
      readonly property bool showActions: NotificationLayout.showActions(detailMode)
      readonly property bool showTimestamp: NotificationLayout.showTimestamp(detailMode)
      // Effective toast width: max(density base, user minimum) * UI scale.
      readonly property int notifWidth: NotificationLayout.effectiveWidth(Settings.data.notifications.density === "compact" ? 320 : 440, Settings.data.notifications.minWidth, Style.uiScaleRatio)
      readonly property int shadowPadding: Style.shadowBlurMax + Style.marginL

      // Notification theme: a named set of *semantic* visual parameters resolved
      // by the pure NotificationTheme.js module. The mapping helpers below turn
      // those semantic keys into concrete qs.Commons Style/Color tokens so that
      // no colors or pixel sizes are hardcoded here.
      readonly property var theme: NotificationTheme.resolveTheme(Settings.data.notifications.notificationTheme)
      function themeRadius() {
        switch (theme.cornerRadius) {
        case "none":
          return 0;
        case "small":
          return Style.radiusXS;
        case "medium":
          return Style.radiusM;
        default:
          return Style.radiusL;
        }
      }
      function themeBorderWidth() {
        switch (theme.borderWidth) {
        case "none":
          return 0;
        case "thick":
          return Style.borderM;
        default:
          return Style.borderS;
        }
      }
      function themePadding() {
        switch (theme.padding) {
        case "tight":
          return Style.marginS;
        case "roomy":
          return Style.marginL;
        default:
          return Style.marginM;
        }
      }
      function themeBackgroundColor() {
        return theme.background === "surfaceVariant" ? Color.mSurfaceVariant : Color.mSurface;
      }
      function themeIconSize() {
        switch (theme.iconSize) {
        case "small":
          return Math.round(24 * Style.uiScaleRatio);
        case "large":
          return Math.round(48 * Style.uiScaleRatio);
        default:
          return Math.round(40 * Style.uiScaleRatio);
        }
      }
      // Resolve the accent/urgency color for a given urgency level per the
      // theme's accentSource. "none" yields a neutral outline tint.
      function themeAccentColor(urgency) {
        if (theme.accentSource === "primary")
          return Color.mPrimary;
        if (theme.accentSource === "none")
          return Color.mOutline;
        // "urgency" (default): critical -> error, low -> on-surface, else primary.
        return urgency === 2 ? Color.mError : urgency === 0 ? Color.mOnSurface : Color.mPrimary;
      }

      // Calculate bar and frame offsets for each edge separately
      readonly property int barOffsetTop: {
        if (barPos !== "top")
          return isFramed ? frameThickness : 0;
        const floatMarginV = isFloating ? Math.ceil(Settings.data.bar.marginVertical) : 0;
        return barHeight + floatMarginV;
      }

      readonly property int barOffsetBottom: {
        if (barPos !== "bottom")
          return isFramed ? frameThickness : 0;
        const floatMarginV = isFloating ? Math.ceil(Settings.data.bar.marginVertical) : 0;
        return barHeight + floatMarginV;
      }

      readonly property int barOffsetLeft: {
        if (barPos !== "left")
          return isFramed ? frameThickness : 0;
        const floatMarginH = isFloating ? Math.ceil(Settings.data.bar.marginHorizontal) : 0;
        return barHeight + floatMarginH;
      }

      readonly property int barOffsetRight: {
        if (barPos !== "right")
          return isFramed ? frameThickness : 0;
        const floatMarginH = isFloating ? Math.ceil(Settings.data.bar.marginHorizontal) : 0;
        return barHeight + floatMarginH;
      }

      // Anchoring
      anchors.top: isTop
      anchors.bottom: isBottom
      anchors.left: isLeft
      anchors.right: isRight

      // Margins for PanelWindow - only apply bar offset for the specific edge where the bar is
      margins.top: isTop ? barOffsetTop - shadowPadding + Style.marginM : 0
      margins.bottom: isBottom ? barOffsetBottom - shadowPadding : 0
      margins.left: isLeft ? barOffsetLeft - shadowPadding + Style.marginM : 0
      margins.right: isRight ? barOffsetRight - shadowPadding + Style.marginM : 0

      implicitWidth: notifWidth + shadowPadding * 2
      implicitHeight: notificationStack.implicitHeight + Style.marginL

      property var animateConnection: null

      Component.onCompleted: {
        animateConnection = function (notificationId) {
          var delegate = null;
          if (notificationRepeater) {
            for (var i = 0; i < notificationRepeater.count; i++) {
              var item = notificationRepeater.itemAt(i);
              if (item?.notificationId === notificationId) {
                delegate = item;
                break;
              }
            }
          }

          try {
            if (delegate && typeof delegate.animateOut === "function" && !delegate.isRemoving) {
              delegate.animateOut();
            }
          } catch (e) {
            // Service fallback if delegate is already invalid
            NotificationService.dismissActiveNotification(notificationId);
          }
        };

        NotificationService.animateAndRemove.connect(animateConnection);
      }

      Component.onDestruction: {
        if (animateConnection) {
          NotificationService.animateAndRemove.disconnect(animateConnection);
          animateConnection = null;
        }
      }

      ColumnLayout {
        id: notificationStack

        // qmllint disable Quick.anchor-combinations
        // (At runtime exactly one of left/right/horizontalCenter is set
        //  via the conditional `: undefined` pattern — qmllint can't
        //  tell so it flags the static union.)
        anchors {
          top: parent.isTop ? parent.top : undefined
          bottom: parent.isBottom ? parent.bottom : undefined
          left: parent.isLeft ? parent.left : undefined
          right: parent.isRight ? parent.right : undefined
          horizontalCenter: parent.isCentered ? parent.horizontalCenter : undefined
        }
        // qmllint enable Quick.anchor-combinations

        spacing: -notifWindow.shadowPadding * 2 + Style.marginM

        Behavior on implicitHeight {
          enabled: !Settings.data.general.animationDisabled
          SpringAnimation {
            spring: 2.0
            damping: 0.4
            epsilon: 0.01
            mass: 0.8
          }
        }

        Repeater {
          id: notificationRepeater
          model: notificationModel

          delegate: Item {
            id: card

            property string notificationId: model.id
            property var notificationData: model
            property int hoverCount: 0
            property bool isRemoving: false

            readonly property int animationDelay: index * 100
            readonly property int slideDistance: 300

            Layout.preferredWidth: notifWidth + notifWindow.shadowPadding * 2
            Layout.preferredHeight: (notifWindow.isCompact ? compactContent.implicitHeight : notificationContent.implicitHeight) + Style.marginXL + notifWindow.shadowPadding * 2
            Layout.maximumHeight: Layout.preferredHeight

            // Animation properties
            property real scaleValue: 0.8
            property real opacityValue: 0.0
            property real slideOffset: 0
            property real swipeOffset: 0
            property real swipeOffsetY: 0
            property real pressGlobalX: 0
            property real pressGlobalY: 0
            property bool isSwiping: false
            property bool suppressClick: false
            readonly property bool useVerticalSwipe: notifWindow.location === "bottom" || notifWindow.location === "top"
            readonly property real swipeStartThreshold: Math.round(18 * Style.uiScaleRatio)
            readonly property real swipeDismissThreshold: Math.max(110, cardBackground.width * 0.32)
            readonly property real verticalSwipeDismissThreshold: Math.max(70, cardBackground.height * 0.35)

            scale: scaleValue
            opacity: opacityValue
            transform: Translate {
              x: card.swipeOffset
              y: card.slideOffset + card.swipeOffsetY
            }

            readonly property real slideInOffset: notifWindow.isTop ? -slideDistance : slideDistance
            readonly property real slideOutOffset: slideInOffset

            function clampSwipeDelta(deltaX) {
              if (notifWindow.isRight)
                return Math.max(0, deltaX);
              if (notifWindow.isLeft)
                return Math.min(0, deltaX);
              return deltaX;
            }

            function clampVerticalSwipeDelta(deltaY) {
              if (notifWindow.isBottom)
                return Math.max(0, deltaY);
              if (notifWindow.isTop)
                return Math.min(0, deltaY);
              return deltaY;
            }

            // Background with border
            Rectangle {
              id: cardBackground
              anchors.fill: parent
              anchors.margins: notifWindow.shadowPadding
              radius: notifWindow.themeRadius()
              border.color: Qt.alpha(Color.mOutline, Settings.data.notifications.backgroundOpacity || 1.0)
              border.width: notifWindow.themeBorderWidth()
              color: Qt.alpha(notifWindow.themeBackgroundColor(), Settings.data.notifications.backgroundOpacity || 1.0)

              // Accent bar: a vertical colored strip on the left (leading) edge,
              // shown only by themes that opt in (e.g. "accent-bar"). Always on
              // the left so it reads consistently regardless of toast position
              // and never collides with the close button (top-right). Its outer
              // corner radii are clamped to its own width so it follows the
              // card's rounded corner without bleeding past it.
              Rectangle {
                id: accentBar
                visible: notifWindow.theme.accentBar && notifWindow.theme.accentBarWidth > 0
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                width: Math.round(notifWindow.theme.accentBarWidth * Style.uiScaleRatio)
                // Match the card's left corner radius exactly so the bar's
                // rounded corners trace the card's edge with no bleed. (Using a
                // smaller radius would round inside the card and expose the
                // background at the corner.)
                topLeftRadius: parent.radius
                bottomLeftRadius: parent.radius
                topRightRadius: 0
                bottomRightRadius: 0
                color: Qt.alpha(notifWindow.themeAccentColor(model.urgency), Settings.data.notifications.backgroundOpacity || 1.0)
              }

              // Progress bar
              Rectangle {
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: 2
                color: "transparent"

                Rectangle {
                  id: progressBar
                  readonly property real progressWidth: cardBackground.width - (2 * cardBackground.radius)
                  height: parent.height
                  x: cardBackground.radius + (progressWidth * (1 - model.progress)) / 2
                  width: progressWidth * model.progress

                  color: {
                    var baseColor = model.urgency === 2 ? Color.mError : model.urgency === 0 ? Color.mOnSurface : Color.mPrimary;
                    return Qt.alpha(baseColor, Settings.data.notifications.backgroundOpacity || 1.0);
                  }

                  antialiasing: true

                  Behavior on width {
                    enabled: !card.isRemoving
                    NumberAnimation {
                      duration: 100
                      easing.type: Easing.Linear
                    }
                  }

                  Behavior on x {
                    enabled: !card.isRemoving
                    NumberAnimation {
                      duration: 100
                      easing.type: Easing.Linear
                    }
                  }
                }
              }
            }

            NDropShadow {
              anchors.fill: cardBackground
              source: cardBackground
              autoPaddingEnabled: true
            }

            // Hover handling
            onHoverCountChanged: {
              if (hoverCount > 0) {
                resumeTimer.stop();
                NotificationService.pauseTimeout(notificationId);
              } else {
                resumeTimer.start();
              }
            }

            Timer {
              id: resumeTimer
              interval: 50
              repeat: false
              onTriggered: {
                if (hoverCount === 0) {
                  NotificationService.resumeTimeout(notificationId);
                }
              }
            }

            // Right-click to dismiss
            MouseArea {
              id: cardDragArea
              anchors.fill: cardBackground
              acceptedButtons: Qt.LeftButton | Qt.RightButton
              hoverEnabled: true
              onEntered: card.hoverCount++
              onExited: card.hoverCount--
              onPressed: mouse => {
                           if (mouse.button === Qt.LeftButton) {
                             const globalPoint = cardDragArea.mapToGlobal(mouse.x, mouse.y);
                             card.pressGlobalX = globalPoint.x;
                             card.pressGlobalY = globalPoint.y;
                             card.isSwiping = false;
                             card.suppressClick = false;
                           }
                         }
              onPositionChanged: mouse => {
                                   if (!(mouse.buttons & Qt.LeftButton) || card.isRemoving)
                                   return;
                                   const globalPoint = cardDragArea.mapToGlobal(mouse.x, mouse.y);
                                   const rawDeltaX = globalPoint.x - card.pressGlobalX;
                                   const rawDeltaY = globalPoint.y - card.pressGlobalY;
                                   const deltaX = card.clampSwipeDelta(rawDeltaX);
                                   const deltaY = card.clampVerticalSwipeDelta(rawDeltaY);
                                   if (!card.isSwiping) {
                                     if (card.useVerticalSwipe) {
                                       if (Math.abs(deltaY) < card.swipeStartThreshold)
                                       return;
                                       card.isSwiping = true;
                                     } else {
                                       if (Math.abs(deltaX) < card.swipeStartThreshold)
                                       return;
                                       card.isSwiping = true;
                                     }
                                   }
                                   if (card.useVerticalSwipe) {
                                     card.swipeOffset = 0;
                                     card.swipeOffsetY = deltaY;
                                   } else {
                                     card.swipeOffset = deltaX;
                                     card.swipeOffsetY = 0;
                                   }
                                 }
              onReleased: mouse => {
                            if (mouse.button === Qt.RightButton) {
                              card.animateOut();
                              if (Settings.data.notifications.clearDismissed) {
                                NotificationService.removeFromHistory(notificationId);
                              }
                              return;
                            }

                            if (mouse.button !== Qt.LeftButton)
                            return;

                            if (card.isSwiping) {
                              const dismissDistance = card.useVerticalSwipe ? Math.abs(card.swipeOffsetY) : Math.abs(card.swipeOffset);
                              const threshold = card.useVerticalSwipe ? card.verticalSwipeDismissThreshold : card.swipeDismissThreshold;
                              if (dismissDistance >= threshold) {
                                card.dismissBySwipe();
                                if (Settings.data.notifications.clearDismissed) {
                                  NotificationService.removeFromHistory(notificationId);
                                }
                              } else {
                                card.swipeOffset = 0;
                                card.swipeOffsetY = 0;
                              }
                              card.suppressClick = true;
                              card.isSwiping = false;
                              return;
                            }

                            if (card.suppressClick)
                            return;

                            var actions = model.actionsJson ? JSON.parse(model.actionsJson) : [];
                            var hasDefault = actions.some(function (a) {
                              return a.identifier === "default";
                            });
                            if (hasDefault) {
                              card.runDeferredTimer("default", false);
                            }
                          }
              onCanceled: {
                card.isSwiping = false;
                card.swipeOffset = 0;
                card.swipeOffsetY = 0;
              }
            }
            // Animation setup
            function triggerEntryAnimation() {
              animInDelayTimer.stop();
              removalTimer.stop();
              resumeTimer.stop();
              isRemoving = false;
              hoverCount = 0;
              isSwiping = false;
              swipeOffset = 0;
              swipeOffsetY = 0;
              if (Settings.data.general.animationDisabled) {
                slideOffset = 0;
                scaleValue = 1.0;
                opacityValue = 1.0;
                return;
              }

              slideOffset = slideInOffset;
              scaleValue = 0.8;
              opacityValue = 0.0;
              animInDelayTimer.interval = animationDelay;
              animInDelayTimer.start();
            }

            Component.onCompleted: triggerEntryAnimation()

            onNotificationIdChanged: triggerEntryAnimation()

            Timer {
              id: animInDelayTimer
              interval: 0
              repeat: false
              onTriggered: {
                if (card.isRemoving)
                  return;
                slideOffset = 0;
                scaleValue = 1.0;
                opacityValue = 1.0;
              }
            }

            function animateOut() {
              if (isRemoving)
                return;
              animInDelayTimer.stop();
              resumeTimer.stop();
              isRemoving = true;
              isSwiping = false;
              swipeOffset = 0;
              swipeOffsetY = 0;
              if (!Settings.data.general.animationDisabled) {
                slideOffset = slideOutOffset;
                scaleValue = 0.8;
                opacityValue = 0.0;
              }
            }

            function dismissBySwipe() {
              if (isRemoving)
                return;
              animInDelayTimer.stop();
              resumeTimer.stop();
              isRemoving = true;
              isSwiping = false;
              if (!Settings.data.general.animationDisabled) {
                if (useVerticalSwipe) {
                  swipeOffset = 0;
                  swipeOffsetY = swipeOffsetY >= 0 ? cardBackground.height + Style.marginXL : -cardBackground.height - Style.marginXL;
                } else {
                  swipeOffset = swipeOffset >= 0 ? cardBackground.width + Style.marginXL : -cardBackground.width - Style.marginXL;
                  swipeOffsetY = 0;
                }
                scaleValue = 0.8;
                opacityValue = 0.0;
              } else {
                swipeOffset = 0;
                swipeOffsetY = 0;
              }
            }

            function runDeferredTimer(actionId, isDismissed) {
              if (Style.animationSlow <= 0) {
                if (!isDismissed) {
                  NotificationService.invokeAction(notificationId, actionId);
                } else if (Settings.data.notifications.clearDismissed) {
                  NotificationService.removeFromHistory(notificationId);
                }
                card.animateOut();
                return;
              }

              deferredActionTimer.stop();
              deferredActionTimer.actionId = actionId || "";
              deferredActionTimer.isDismissed = isDismissed;
              deferredActionTimer.interval = Math.min(50, Math.max(1, Style.animationSlow - 1));
              card.animateOut();
              deferredActionTimer.start();
            }

            Timer {
              id: removalTimer
              interval: Style.animationSlow
              repeat: false
              onTriggered: {
                NotificationService.dismissActiveNotification(notificationId);
              }
            }

            Timer {
              id: deferredActionTimer
              interval: 50
              property string actionId: ""
              property bool isDismissed: false
              onTriggered: {
                if (!isDismissed) {
                  NotificationService.invokeAction(notificationId, actionId);
                } else if (Settings.data.notifications.clearDismissed) {
                  NotificationService.removeFromHistory(notificationId);
                }
              }
            }

            onIsRemovingChanged: {
              if (isRemoving) {
                removalTimer.start();
              }
            }

            Behavior on scale {
              enabled: !Settings.data.general.animationDisabled
              SpringAnimation {
                spring: 3
                damping: 0.4
                epsilon: 0.01
                mass: 0.8
              }
            }

            Behavior on opacity {
              enabled: !Settings.data.general.animationDisabled
              NumberAnimation {
                duration: Style.animationNormal
                easing.type: Easing.OutCubic
              }
            }

            Behavior on slideOffset {
              enabled: !Settings.data.general.animationDisabled
              SpringAnimation {
                spring: 2.5
                damping: 0.3
                epsilon: 0.01
                mass: 0.6
              }
            }

            Behavior on swipeOffset {
              enabled: !Settings.data.general.animationDisabled && !card.isSwiping
              NumberAnimation {
                duration: Style.animationFast
                easing.type: Easing.OutCubic
              }
            }

            Behavior on swipeOffsetY {
              enabled: !Settings.data.general.animationDisabled && !card.isSwiping
              NumberAnimation {
                duration: Style.animationFast
                easing.type: Easing.OutCubic
              }
            }

            // Content
            ColumnLayout {
              id: notificationContent
              visible: !notifWindow.isCompact
              anchors.fill: cardBackground
              anchors.margins: notifWindow.themePadding()
              anchors.leftMargin: notifWindow.themePadding() + (notifWindow.theme.accentBar ? Math.round(notifWindow.theme.accentBarWidth * Style.uiScaleRatio) : 0)
              spacing: Style.marginM

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.marginL
                Layout.leftMargin: Style.marginM
                Layout.rightMargin: Style.marginM
                Layout.topMargin: Style.marginM
                Layout.bottomMargin: Style.marginM

                NImageRounded {
                  visible: notifWindow.theme.iconPlacement !== "hidden"
                  Layout.preferredWidth: visible ? notifWindow.themeIconSize() : 0
                  Layout.preferredHeight: notifWindow.themeIconSize()
                  Layout.alignment: Qt.AlignVCenter
                  radius: Math.min(Style.radiusL, Layout.preferredWidth / 2)
                  imagePath: model.originalImage || ""
                  borderColor: "transparent"
                  borderWidth: 0
                  fallbackIcon: "bell"
                  fallbackIconSize: 24
                }

                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: Style.marginS

                  // Header with urgency indicator
                  RowLayout {
                    Layout.fillWidth: true
                    spacing: Style.marginS

                    Rectangle {
                      Layout.preferredWidth: 6
                      Layout.preferredHeight: 6
                      Layout.alignment: Qt.AlignVCenter
                      radius: Style.radiusXS
                      color: model.urgency === 2 ? Color.mError : model.urgency === 0 ? Color.mOnSurface : Color.mPrimary
                    }

                    NText {
                      text: model.appName || "Unknown App"
                      pointSize: Style.fontSizeXS
                      font.weight: Style.fontWeightBold
                      color: Color.mSecondary
                    }

                    NText {
                      visible: notifWindow.showTimestamp
                      textFormat: Text.PlainText
                      text: " " + Time.formatRelativeTime(model.timestamp)
                      pointSize: Style.fontSizeXXS
                      color: Color.mOnSurfaceVariant
                      Layout.alignment: Qt.AlignBottom
                    }

                    Item {
                      Layout.fillWidth: true
                    }
                  }

                  NText {
                    text: model.summary || I18n.tr("common.no-summary")
                    pointSize: Style.fontSizeM
                    font.weight: Style.fontWeightMedium
                    color: Color.mOnSurface
                    textFormat: Text.StyledText
                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                    maximumLineCount: 3
                    elide: Text.ElideRight
                    visible: text.length > 0
                    Layout.fillWidth: true
                    Layout.rightMargin: Style.marginM
                  }

                  NText {
                    text: model.body || ""
                    pointSize: Style.fontSizeM
                    color: Color.mOnSurface
                    textFormat: Text.StyledText
                    wrapMode: Text.WrapAtWordBoundaryOrAnywhere

                    maximumLineCount: 5
                    elide: Text.ElideRight
                    visible: notifWindow.showBody && text.length > 0
                    Layout.fillWidth: true
                    Layout.rightMargin: Style.marginXL
                  }

                  // Actions
                  Flow {
                    Layout.fillWidth: true
                    spacing: Style.marginS
                    Layout.topMargin: Style.marginM
                    flow: Flow.LeftToRight

                    property string parentNotificationId: notificationId
                    property var parsedActions: {
                      try {
                        return model.actionsJson ? JSON.parse(model.actionsJson) : [];
                      } catch (e) {
                        return [];
                      }
                    }
                    visible: notifWindow.showActions && parsedActions.length > 0

                    Repeater {
                      model: parent.parsedActions

                      delegate: NButton {
                        property var actionData: modelData

                        onEntered: card.hoverCount++
                        onExited: card.hoverCount--

                        text: {
                          var actionText = actionData.text || "Open";
                          if (actionText.includes(",")) {
                            return actionText.split(",")[1] || actionText;
                          }
                          return actionText;
                        }
                        fontSize: Style.fontSizeS
                        backgroundColor: Color.mPrimary
                        textColor: hovered ? Color.mOnHover : Color.mOnPrimary
                        hoverColor: Color.mHover
                        outlined: false
                        implicitHeight: 24
                        onClicked: {
                          card.runDeferredTimer(actionData.identifier, false);
                        }
                      }
                    }
                  }
                }
              }
            }

            // Close button
            NIconButton {
              visible: !notifWindow.isCompact
              icon: "close"
              tooltipText: I18n.tr("tooltips.dismiss-notification")
              baseSize: Style.baseWidgetSize * 0.6
              anchors.top: cardBackground.top
              anchors.topMargin: Style.marginM
              anchors.right: cardBackground.right
              anchors.rightMargin: Style.marginM

              onClicked: {
                card.runDeferredTimer("", true);
              }
            }

            // Compact content
            RowLayout {
              id: compactContent
              visible: notifWindow.isCompact
              anchors.fill: cardBackground
              anchors.margins: notifWindow.themePadding()
              anchors.leftMargin: notifWindow.themePadding() + (notifWindow.theme.accentBar ? Math.round(notifWindow.theme.accentBarWidth * Style.uiScaleRatio) : 0)
              spacing: Style.marginS

              NImageRounded {
                visible: notifWindow.theme.iconPlacement !== "hidden"
                Layout.preferredWidth: visible ? Math.round(24 * Style.uiScaleRatio) : 0
                Layout.preferredHeight: Math.round(24 * Style.uiScaleRatio)
                Layout.alignment: Qt.AlignVCenter
                radius: Style.radiusXS
                imagePath: model.originalImage || ""
                borderColor: "transparent"
                borderWidth: 0
                fallbackIcon: "bell"
                fallbackIconSize: 16
              }

              ColumnLayout {
                Layout.fillWidth: true
                spacing: Style.marginXS

                NText {
                  text: model.summary || I18n.tr("common.no-summary")
                  pointSize: Style.fontSizeM
                  font.weight: Style.fontWeightMedium
                  color: Color.mOnSurface
                  textFormat: Text.StyledText
                  maximumLineCount: 1
                  elide: Text.ElideRight
                  Layout.fillWidth: true
                }

                NText {
                  visible: model.body && model.body.length > 0
                  Layout.fillWidth: true
                  text: model.body || ""
                  pointSize: Style.fontSizeS
                  color: Color.mOnSurfaceVariant
                  textFormat: Text.StyledText
                  wrapMode: Text.Wrap
                  maximumLineCount: 2
                  elide: Text.ElideRight
                }
              }
            }
          }
        }
      }
    }
  }
}
