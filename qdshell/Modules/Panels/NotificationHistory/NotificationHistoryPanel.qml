import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Notifications
import Quickshell.Wayland
import qs.Commons
import qs.Modules.MainScreen
import qs.Services.System
import qs.Widgets

// Notification History panel
SmartPanel {
  id: root

  preferredWidth: Math.round((Settings.data.notifications.enableMarkdown ? 540 : 440) * Style.uiScaleRatio)
  preferredHeight: Math.round((Settings.data.notifications.enableMarkdown ? 640 : 540) * Style.uiScaleRatio)

  onOpened: {
    NotificationService.updateLastSeenTs();
  }

  panelContent: PanelShell {
    id: panelContent
    focus: true

    title: I18n.tr("common.notifications")
    icon: "bell"
    onCloseRequested: root.close()

    // Body uses Layout.fillHeight and NScrollView (implicitHeight 0), so the
    // default mainColumn-based size calc collapses to just the header. Pin to
    // SmartPanel's declared height instead.
    contentPreferredHeight: root.preferredHeight

    headerActions: [
      NIconButton {
        icon: NotificationService.doNotDisturb ? "bell-off" : "bell"
        tooltipText: I18n.tr("tooltips.do-not-disturb-enabled")
        baseSize: Style.baseWidgetSize * 0.8
        onClicked: NotificationService.doNotDisturb = !NotificationService.doNotDisturb
      },
      NIconButton {
        icon: "checks"
        tooltipText: I18n.tr("actions.mark-all-read") || "Mark all read"
        baseSize: Style.baseWidgetSize * 0.8
        onClicked: NotificationService.markAllRead()
      },
      NIconButton {
        icon: "trash"
        tooltipText: I18n.tr("actions.clear-history")
        baseSize: Style.baseWidgetSize * 0.8
        onClicked: {
          NotificationService.clearHistory();
          // Close panel as there is nothing more to see.
          root.close();
        }
      }
    ]

    // Force focus when opened
    Connections {
      target: root
      function onOpened() {
        panelContent.forceActiveFocus();
      }
    }

    Keys.onPressed: event => {
                      // Tab navigation for categories
                      if (event.key === Qt.Key_Tab) {
                        currentRange = (currentRange + 1) % 4;
                        event.accepted = true;
                        return;
                      }

                      if (event.key === Qt.Key_Backtab) { // Shift+Tab
                        currentRange = (currentRange - 1 + 4) % 4;
                        event.accepted = true;
                        return;
                      }

                      // Navigation Up/Down
                      if (checkKey(event, 'up')) {
                        moveSelection(-1);
                        event.accepted = true;
                        return;
                      }
                      if (checkKey(event, 'down')) {
                        moveSelection(1);
                        event.accepted = true;
                        return;
                      }

                      // Action Navigation Left/Right
                      if (checkKey(event, 'left')) {
                        moveAction(-1);
                        event.accepted = true;
                        return;
                      }
                      if (checkKey(event, 'right')) {
                        moveAction(1);
                        event.accepted = true;
                        return;
                      }

                      // Activation (Enter)
                      if (checkKey(event, 'enter')) {
                        activateSelection();
                        event.accepted = true;
                        return;
                      }

                      // Removal (Delete/Remove)
                      if (checkKey(event, 'remove') || event.key === Qt.Key_Delete) {
                        removeSelection();
                        event.accepted = true;
                        return;
                      }
                    }

    function parseActions(actions) {
      try {
        return JSON.parse(actions || "[]");
      } catch (e) {
        return [];
      }
    }

    function moveSelection(dir) {
      var m = NotificationService.historyList;
      if (!m || m.count === 0)
        return;

      var newIndex = focusIndex;
      var found = false;
      var count = m.count;

      // If no selection yet, start from beginning (or end if up)
      if (focusIndex === -1) {
        if (dir > 0)
          newIndex = -1;
        else
          newIndex = count;
      }

      // Loop to find next visible item
      var loopCount = 0;
      while (loopCount < count) {
        newIndex += dir;

        // Bounds check
        if (newIndex < 0 || newIndex >= count) {
          break; // Stop at edges
        }

        var item = m.get(newIndex);
        if (item && isInCurrentRange(item.timestamp)) {
          found = true;
          break;
        }
        loopCount++;
      }

      if (found) {
        focusIndex = newIndex;
        actionIndex = -1; // Reset action selection
        scrollToItem(focusIndex);
      }
    }

    function moveAction(dir) {
      if (focusIndex === -1)
        return;
      var item = NotificationService.historyList.get(focusIndex);
      if (!item)
        return;

      var actions = parseActions(item.actionsJson);

      if (actions.length === 0)
        return;

      var newActionIndex = actionIndex + dir;

      // Clamp between -1 (body) and actions.length - 1
      if (newActionIndex < -1)
        newActionIndex = -1;
      if (newActionIndex >= actions.length)
        newActionIndex = actions.length - 1;

      actionIndex = newActionIndex;
    }

    function activateSelection() {
      if (focusIndex === -1)
        return;
      var item = NotificationService.historyList.get(focusIndex);
      if (!item)
        return;

      if (actionIndex >= 0) {
        var actions = parseActions(item.actionsJson);
        if (actionIndex < actions.length) {
          NotificationService.invokeAction(item.id, actions[actionIndex].identifier);
        }
      } else {
        if (scrollView.expandedId === item.id) {
          scrollView.expandedId = "";
        } else {
          scrollView.expandedId = item.id;
        }
      }
    }

    function removeSelection() {
      if (focusIndex === -1)
        return;
      var item = NotificationService.historyList.get(focusIndex);
      if (!item)
        return;

      NotificationService.removeFromHistory(item.id);
    }

    function scrollToItem(index) {
      // Find the delegate item
      if (index < 0 || index >= notificationColumn.children.length)
        return;

      var item = notificationColumn.children[index];
      if (item && item.visible) {
        // Use the internal flickable from NScrollView for accurate scrolling
        var flickable = scrollView._internalFlickable;
        if (!flickable || !flickable.contentItem)
          return;

        var pos = flickable.contentItem.mapFromItem(item, 0, 0);
        var itemY = pos.y;
        var itemHeight = item.height;

        var currentContentY = flickable.contentY;
        var viewHeight = flickable.height;

        // Check if above visible area
        if (itemY < currentContentY) {
          flickable.contentY = Math.max(0, itemY - Style.marginM);
        } else
          // Check if below visible area
          if (itemY + itemHeight > currentContentY + viewHeight) {
            flickable.contentY = (itemY + itemHeight) - viewHeight + Style.marginM;
          }
      }
    }

    property real layoutWidth: Math.max(1, root.preferredWidth - (Style.marginL * 2))

    // State (lazy-loaded with panelContent)
    property var rangeCounts: [0, 0, 0, 0]
    property var lastKnownDate: null  // Track the current date to detect day changes

    // UI state (lazy-loaded with panelContent)
    // 0 = All, 1 = Today, 2 = Yesterday, 3 = Earlier
    property int currentRange: 1  // start on Today by default
    property bool groupByDate: true
    onCurrentRangeChanged: resetFocus()

    // Keyboard navigation state
    property int focusIndex: -1
    property int actionIndex: -1  // For actions within a notification

    function resetFocus() {
      focusIndex = -1;
      actionIndex = -1;
    }

    function checkKey(event, settingName) {
      return Keybinds.checkKey(event, settingName, Settings);
    }

    // Helper functions (lazy-loaded with panelContent)
    function dateOnly(d) {
      return new Date(d.getFullYear(), d.getMonth(), d.getDate());
    }

    function getDateKey(d) {
      var date = dateOnly(d);
      return date.getFullYear() + "-" + date.getMonth() + "-" + date.getDate();
    }

    function rangeForTimestamp(ts) {
      var dt = new Date(ts);
      var today = dateOnly(new Date());
      var thatDay = dateOnly(dt);

      var diffMs = today - thatDay;
      var diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

      if (diffDays === 0)
        return 0;
      if (diffDays === 1)
        return 1;
      return 2;
    }

    function recalcRangeCounts() {
      var m = NotificationService.historyList;
      if (!m || typeof m.count === "undefined" || m.count <= 0) {
        panelContent.rangeCounts = [0, 0, 0, 0];
        return;
      }

      var counts = [0, 0, 0, 0];

      counts[0] = m.count;

      for (var i = 0; i < m.count; ++i) {
        var item = m.get(i);
        if (!item || typeof item.timestamp === "undefined")
          continue;
        var r = rangeForTimestamp(item.timestamp);
        counts[r + 1] = counts[r + 1] + 1;
      }

      panelContent.rangeCounts = counts;
    }

    function isInCurrentRange(ts) {
      if (currentRange === 0)
        return true;
      return rangeForTimestamp(ts) === (currentRange - 1);
    }

    function countForRange(range) {
      return rangeCounts[range] || 0;
    }

    function hasNotificationsInCurrentRange() {
      var m = NotificationService.historyList;
      if (!m || m.count === 0) {
        return false;
      }
      for (var i = 0; i < m.count; ++i) {
        var item = m.get(i);
        if (item && isInCurrentRange(item.timestamp))
          return true;
      }
      return false;
    }

    Component.onCompleted: {
      recalcRangeCounts();
      // Initialize lastKnownDate
      lastKnownDate = getDateKey(new Date());
    }

    Connections {
      target: NotificationService.historyList
      function onCountChanged() {
        panelContent.recalcRangeCounts();
      }
    }

    // Timer to check for day changes at midnight
    Timer {
      id: dayChangeTimer
      interval: 60000  // Check every minute
      repeat: true
      running: true  // Always runs when panelContent exists (panel is open)
      onTriggered: {
        var currentDateKey = panelContent.getDateKey(new Date());
        if (panelContent.lastKnownDate !== null && panelContent.lastKnownDate !== currentDateKey) {
          // Day has changed, recalculate counts
          panelContent.recalcRangeCounts();
        }
        panelContent.lastKnownDate = currentDateKey;
      }
    }

    // Time range tabs ([All] / [Today] / [Yesterday] / [Earlier])
    // Lifted out of the header now that PanelShell owns the title row.
    NBox {
      id: headerBox
      Layout.fillWidth: true
      visible: NotificationService.historyList.count > 0 && panelContent.groupByDate
      implicitHeight: tabsBox.implicitHeight + Style.marginM * 2

      NTabBar {
        id: tabsBox
        anchors.fill: parent
        anchors.margins: Style.marginM
        currentIndex: panelContent.currentRange
        tabHeight: Style.toOdd(Style.baseWidgetSize * 0.8)
        spacing: Style.marginXS
        distributeEvenly: true

        NTabButton {
          tabIndex: 0
          text: I18n.tr("launcher.categories.all") + " (" + panelContent.countForRange(0) + ")"
          checked: tabsBox.currentIndex === 0
          onClicked: panelContent.currentRange = 0
          pointSize: Style.fontSizeXS
        }

        NTabButton {
          tabIndex: 1
          text: I18n.tr("notifications.range.today") + " (" + panelContent.countForRange(1) + ")"
          checked: tabsBox.currentIndex === 1
          onClicked: panelContent.currentRange = 1
          pointSize: Style.fontSizeXS
        }

        NTabButton {
          tabIndex: 2
          text: I18n.tr("notifications.range.yesterday") + " (" + panelContent.countForRange(2) + ")"
          checked: tabsBox.currentIndex === 2
          onClicked: panelContent.currentRange = 2
          pointSize: Style.fontSizeXS
        }

        NTabButton {
          tabIndex: 3
          text: I18n.tr("notifications.range.earlier") + " (" + panelContent.countForRange(3) + ")"
          checked: tabsBox.currentIndex === 3
          onClicked: panelContent.currentRange = 3
          pointSize: Style.fontSizeXS
        }
      }
    }

    // Notification list container with gradient overlay
    Item {
      Layout.fillWidth: true
      Layout.fillHeight: true

      NScrollView {
        id: scrollView
        anchors.fill: parent
        horizontalPolicy: ScrollBar.AlwaysOff
        verticalPolicy: ScrollBar.AsNeeded
        reserveScrollbarSpace: false
        gradientColor: Color.mSurface

        // Track which notification is expanded
        property string expandedId: ""

        ColumnLayout {
          width: panelContent.layoutWidth
          spacing: Style.marginM

          // Empty state when no notifications
          NBox {
            visible: !panelContent.hasNotificationsInCurrentRange()
            Layout.fillWidth: true
            Layout.preferredHeight: emptyState.implicitHeight + Style.marginXL

            ColumnLayout {
              id: emptyState
              anchors.fill: parent
              anchors.margins: Style.marginM
              spacing: Style.marginM

              Item {
                Layout.fillHeight: true
              }

              NIcon {
                icon: "bell-off"
                pointSize: (NotificationService.historyList.count === 0) ? 48 : Style.baseWidgetSize
                color: Color.mOnSurfaceVariant
                Layout.alignment: Qt.AlignHCenter
              }

              NText {
                text: I18n.tr("notifications.panel.no-notifications")
                pointSize: (NotificationService.historyList.count === 0) ? Style.fontSizeL : Style.fontSizeM
                color: Color.mOnSurfaceVariant
                Layout.alignment: Qt.AlignHCenter
              }

              NText {
                visible: NotificationService.historyList.count === 0
                text: I18n.tr("notifications.panel.description")
                pointSize: Style.fontSizeS
                color: Color.mOnSurfaceVariant
                horizontalAlignment: Text.AlignHCenter
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
              }

              Item {
                Layout.fillHeight: true
              }
            }
          }

          // Notification list container
          Item {
            visible: panelContent.hasNotificationsInCurrentRange()
            Layout.fillWidth: true
            Layout.preferredHeight: notificationColumn.implicitHeight

            Column {
              id: notificationColumn
              width: panelContent.layoutWidth
              spacing: Style.marginM

              Repeater {
                model: NotificationService.historyList

                delegate: Item {
                  id: notificationDelegate
                  width: parent.width
                  visible: panelContent.isInCurrentRange(model.timestamp)
                  height: visible && !isRemoving ? contentColumn.height + Style.marginXL : 0

                  property int listIndex: index
                  property string notificationId: model.id
                  property bool isRead: model.read || false
                  property bool isExpanded: scrollView.expandedId === notificationId
                  property bool canExpand: summaryText.truncated || bodyText.truncated
                  property real swipeOffset: 0
                  property real pressGlobalX: 0
                  property real pressGlobalY: 0
                  property bool isSwiping: false
                  property bool isRemoving: false
                  property string pendingLink: ""
                  readonly property real swipeStartThreshold: Math.round(16 * Style.uiScaleRatio)
                  readonly property real swipeDismissThreshold: Math.max(110, width * 0.3)
                  readonly property int removeAnimationDuration: Style.animationNormal
                  readonly property int notificationTextFormat: (Settings.data.notifications.enableMarkdown && notificationDelegate.isExpanded) ? Text.MarkdownText : Text.StyledText
                  readonly property real actionButtonSize: Style.baseWidgetSize * 0.7
                  readonly property real buttonClusterWidth: notificationDelegate.actionButtonSize * 2 + Style.marginXS
                  readonly property real iconSize: Math.round(40 * Style.uiScaleRatio)

                  function isSafeLink(link) {
                    if (!link)
                      return false;
                    const lower = link.toLowerCase();
                    const schemes = ["http://", "https://", "mailto:"];
                    return schemes.some(scheme => lower.startsWith(scheme));
                  }

                  function linkAtPoint(x, y) {
                    if (!Settings.data.notifications.enableMarkdown || !notificationDelegate.isExpanded)
                      return "";

                    if (summaryText) {
                      const summaryPoint = summaryText.mapFromItem(historyInteractionArea, x, y);
                      if (summaryPoint.x >= 0 && summaryPoint.y >= 0 && summaryPoint.x <= summaryText.width && summaryPoint.y <= summaryText.height) {
                        const summaryLink = summaryText.linkAt ? summaryText.linkAt(summaryPoint.x, summaryPoint.y) : "";
                        if (isSafeLink(summaryLink))
                          return summaryLink;
                      }
                    }

                    if (bodyText) {
                      const bodyPoint = bodyText.mapFromItem(historyInteractionArea, x, y);
                      if (bodyPoint.x >= 0 && bodyPoint.y >= 0 && bodyPoint.x <= bodyText.width && bodyPoint.y <= bodyText.height) {
                        const bodyLink = bodyText.linkAt ? bodyText.linkAt(bodyPoint.x, bodyPoint.y) : "";
                        if (isSafeLink(bodyLink))
                          return bodyLink;
                      }
                    }

                    return "";
                  }

                  function updateCursorAt(x, y) {
                    if (notificationDelegate.isExpanded && notificationDelegate.linkAtPoint(x, y)) {
                      historyInteractionArea.cursorShape = Qt.PointingHandCursor;
                    } else {
                      historyInteractionArea.cursorShape = Qt.ArrowCursor;
                    }
                  }

                  transform: Translate {
                    x: notificationDelegate.swipeOffset
                  }

                  function dismissBySwipe() {
                    if (isRemoving)
                      return;
                    isRemoving = true;
                    isSwiping = false;

                    if (Settings.data.general.animationDisabled) {
                      NotificationService.removeFromHistory(notificationId);
                      return;
                    }

                    swipeOffset = swipeOffset >= 0 ? width + Style.marginL : -width - Style.marginL;
                    opacity = 0;
                    removeTimer.restart();
                  }

                  Timer {
                    id: removeTimer
                    interval: notificationDelegate.removeAnimationDuration
                    repeat: false
                    onTriggered: NotificationService.removeFromHistory(notificationId)
                  }

                  Behavior on swipeOffset {
                    enabled: !Settings.data.general.animationDisabled && !notificationDelegate.isSwiping
                    NumberAnimation {
                      duration: notificationDelegate.removeAnimationDuration
                      easing.type: Easing.OutCubic
                    }
                  }

                  Behavior on opacity {
                    enabled: !Settings.data.general.animationDisabled && notificationDelegate.isRemoving
                    NumberAnimation {
                      duration: notificationDelegate.removeAnimationDuration
                      easing.type: Easing.OutCubic
                    }
                  }

                  Behavior on height {
                    enabled: !Settings.data.general.animationDisabled && notificationDelegate.isRemoving
                    NumberAnimation {
                      duration: notificationDelegate.removeAnimationDuration
                      easing.type: Easing.OutCubic
                    }
                  }

                  Behavior on y {
                    enabled: !Settings.data.general.animationDisabled && notificationDelegate.isRemoving
                    NumberAnimation {
                      duration: notificationDelegate.removeAnimationDuration
                      easing.type: Easing.OutCubic
                    }
                  }

                  // Parse actions safely
                  property var actionsList: parseActions(model.actionsJson)

                  property bool isFocused: index === panelContent.focusIndex

                  Rectangle {
                    anchors.fill: parent
                    radius: Style.radiusM
                    color: Color.mSurfaceVariant
                    border.color: {
                      if (notificationDelegate.isFocused)
                        return Color.mPrimary;
                      if (Settings.data.ui.boxBorderEnabled)
                        return Qt.alpha(Color.mOutline, Style.opacityHeavy);
                      return "transparent";
                    }
                    border.width: notificationDelegate.isFocused ? Style.borderM : Style.borderS

                    Behavior on color {
                      enabled: !Settings.data.general.animationDisabled
                      ColorAnimation {
                        duration: Style.animationFast
                      }
                    }
                  }

                  // Context menu for per-entry actions
                  NContextMenu {
                    id: notifContextMenu
                    model: [
                      {
                        "label": notificationDelegate.isRead
                                 ? (I18n.tr("actions.mark-unread") || "Mark as unread")
                                 : (I18n.tr("actions.mark-read") || "Mark as read"),
                        "action": "toggleRead",
                        "icon": notificationDelegate.isRead ? "mail" : "mail-opened"
                      },
                      {
                        "label": I18n.tr("actions.copy-text") || "Copy text",
                        "action": "copy",
                        "icon": "copy"
                      },
                      {
                        "label": I18n.tr("actions.delete") || "Delete",
                        "action": "delete",
                        "icon": "trash"
                      }
                    ]
                    onTriggered: action => {
                      if (action === "toggleRead") {
                        if (notificationDelegate.isRead)
                          NotificationService.markUnread(notificationDelegate.notificationId);
                        else
                          NotificationService.markRead(notificationDelegate.notificationId);
                      } else if (action === "copy") {
                        NotificationService.copyNotificationText(notificationDelegate.notificationId);
                      } else if (action === "delete") {
                        NotificationService.removeFromHistory(notificationDelegate.notificationId);
                      }
                    }
                  }

                  // Click to expand/collapse
                  MouseArea {
                    id: historyInteractionArea
                    anchors.fill: parent
                    anchors.rightMargin: notificationDelegate.buttonClusterWidth + Style.marginM
                    enabled: !notificationDelegate.isRemoving
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    hoverEnabled: true
                    cursorShape: Qt.ArrowCursor
                    onPressed: mouse => {
                                 panelContent.focusIndex = index;
                                 panelContent.actionIndex = -1;

                                 if (mouse.button === Qt.RightButton) {
                                   notifContextMenu.openAtItem(historyInteractionArea, mouse.x, mouse.y);
                                   return;
                                 }

                                 if (notificationDelegate.isExpanded) {
                                   const link = notificationDelegate.linkAtPoint(mouse.x, mouse.y);
                                   if (link) {
                                     notificationDelegate.pendingLink = link;
                                   } else {
                                     notificationDelegate.pendingLink = "";
                                   }
                                 }

                                 if (mouse.button !== Qt.LeftButton)
                                 return;
                                 const globalPoint = historyInteractionArea.mapToGlobal(mouse.x, mouse.y);
                                 notificationDelegate.pressGlobalX = globalPoint.x;
                                 notificationDelegate.pressGlobalY = globalPoint.y;
                                 notificationDelegate.isSwiping = false;
                               }
                    onPositionChanged: mouse => {
                                         if (!(mouse.buttons & Qt.LeftButton) || notificationDelegate.isRemoving)
                                         return;

                                         const globalPoint = historyInteractionArea.mapToGlobal(mouse.x, mouse.y);
                                         const deltaX = globalPoint.x - notificationDelegate.pressGlobalX;
                                         const deltaY = globalPoint.y - notificationDelegate.pressGlobalY;

                                         if (!notificationDelegate.isSwiping) {
                                           if (Math.abs(deltaX) < notificationDelegate.swipeStartThreshold)
                                           return;

                                           // Only start a swipe-dismiss when horizontal movement is dominant.
                                           if (Math.abs(deltaX) <= Math.abs(deltaY) * 1.15) {
                                             return;
                                           }
                                           notificationDelegate.isSwiping = true;
                                         }

                                         if (notificationDelegate.pendingLink && Math.abs(deltaX) >= notificationDelegate.swipeStartThreshold) {
                                           notificationDelegate.pendingLink = "";
                                         }

                                         notificationDelegate.swipeOffset = deltaX;
                                       }
                    onReleased: mouse => {
                                  if (mouse.button !== Qt.LeftButton)
                                  return;

                                  if (notificationDelegate.isSwiping) {
                                    if (Math.abs(notificationDelegate.swipeOffset) >= notificationDelegate.swipeDismissThreshold) {
                                      notificationDelegate.dismissBySwipe();
                                    } else {
                                      notificationDelegate.swipeOffset = 0;
                                    }
                                    notificationDelegate.isSwiping = false;
                                    notificationDelegate.pendingLink = "";
                                    return;
                                  }

                                  if (notificationDelegate.pendingLink) {
                                    Qt.openUrlExternally(notificationDelegate.pendingLink);
                                    notificationDelegate.pendingLink = "";
                                    return;
                                  }
                                }
                    onCanceled: {
                      notificationDelegate.isSwiping = false;
                      notificationDelegate.swipeOffset = 0;
                      notificationDelegate.pendingLink = "";
                      historyInteractionArea.cursorShape = Qt.ArrowCursor;
                    }
                  }

                  HoverHandler {
                    target: historyInteractionArea
                    onPointChanged: notificationDelegate.updateCursorAt(point.position.x, point.position.y)
                    onActiveChanged: {
                      if (!active) {
                        historyInteractionArea.cursorShape = Qt.ArrowCursor;
                      }
                    }
                  }

                  onVisibleChanged: {
                    if (!visible) {
                      notificationDelegate.isSwiping = false;
                      notificationDelegate.swipeOffset = 0;
                      notificationDelegate.opacity = 1;
                      notificationDelegate.isRemoving = false;
                      removeTimer.stop();
                    }
                  }

                  Component.onDestruction: removeTimer.stop()

                  Column {
                    id: contentColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: Style.marginM
                    spacing: Style.marginM

                    Row {
                      width: parent.width
                      spacing: Style.marginM

                      // Icon
                      NImageRounded {
                        anchors.verticalCenter: notificationDelegate.isExpanded ? undefined : parent.verticalCenter
                        width: notificationDelegate.iconSize
                        height: notificationDelegate.iconSize
                        radius: Math.min(Style.radiusL, width / 2)
                        imagePath: model.cachedImage || model.originalImage || ""
                        borderColor: "transparent"
                        borderWidth: 0
                        fallbackIcon: "bell"
                        fallbackIconSize: 24
                      }

                      // Content
                      Column {
                        width: parent.width - notificationDelegate.iconSize - notificationDelegate.buttonClusterWidth - (Style.marginM * 2)
                        spacing: Style.marginXS

                        // Header row with app name and timestamp
                        Row {
                          width: parent.width
                          spacing: Style.marginS

                          // Unread indicator dot
                          Rectangle {
                            width: 6
                            height: 6
                            anchors.verticalCenter: parent.verticalCenter
                            radius: 3
                            visible: !notificationDelegate.isRead
                            color: Color.mPrimary
                          }

                          // Urgency indicator
                          Rectangle {
                            width: 6
                            height: 6
                            anchors.verticalCenter: parent.verticalCenter
                            radius: 3
                            visible: model.urgency !== 1
                            color: {
                              if (model.urgency === 2)
                                return Color.mError;
                              else if (model.urgency === 0)
                                return Color.mOnSurfaceVariant;
                              else
                                return "transparent";
                            }
                          }

                          NText {
                            text: model.appName || "Unknown App"
                            pointSize: Style.fontSizeXS
                            font.weight: Style.fontWeightBold
                            color: Color.mSecondary
                          }

                          NText {
                            textFormat: Text.PlainText
                            text: " " + Time.formatRelativeTime(model.timestamp)
                            pointSize: Style.fontSizeXXS
                            color: Color.mOnSurfaceVariant
                            anchors.bottom: parent.bottom
                          }
                        }

                        // Summary (bold for unread, normal for read)
                        NText {
                          id: summaryText
                          width: parent.width
                          text: (Settings.data.notifications.enableMarkdown && notificationDelegate.isExpanded) ? (model.summaryMarkdown || I18n.tr("common.no-summary")) : (model.summary || I18n.tr("common.no-summary"))
                          pointSize: Style.fontSizeM
                          font.weight: notificationDelegate.isRead ? Font.Normal : Font.Bold
                          color: notificationDelegate.isRead ? Color.mOnSurfaceVariant : Color.mOnSurface
                          textFormat: notificationDelegate.notificationTextFormat
                          wrapMode: Text.Wrap
                          maximumLineCount: notificationDelegate.isExpanded ? 999 : 2
                          elide: Text.ElideRight
                        }

                        // Body
                        NText {
                          id: bodyText
                          width: parent.width
                          text: (Settings.data.notifications.enableMarkdown && notificationDelegate.isExpanded) ? (model.bodyMarkdown || "") : (model.body || "")
                          pointSize: Style.fontSizeS
                          color: Color.mOnSurfaceVariant
                          textFormat: notificationDelegate.notificationTextFormat
                          wrapMode: Text.Wrap
                          maximumLineCount: notificationDelegate.isExpanded ? 999 : 3
                          elide: Text.ElideRight
                          visible: text.length > 0
                        }

                        // Actions Flow
                        Flow {
                          width: parent.width
                          spacing: Style.marginS
                          visible: notificationDelegate.actionsList.length > 0

                          Repeater {
                            model: notificationDelegate.actionsList

                            delegate: NButton {
                              text: modelData.text
                              fontSize: Style.fontSizeS

                              readonly property bool actionNavActive: notificationDelegate.isFocused && panelContent.actionIndex !== -1
                              readonly property bool isSelected: actionNavActive && panelContent.actionIndex === index

                              backgroundColor: isSelected ? Color.mSecondary : Color.mPrimary
                              textColor: isSelected ? Color.mOnSecondary : Color.mOnPrimary

                              outlined: false
                              implicitHeight: 24

                              onHoveredChanged: {
                                if (hovered) {
                                  panelContent.focusIndex = notificationDelegate.listIndex;
                                }
                              }

                              // Capture modelData in a property to avoid reference errors
                              property var actionData: modelData
                              onClicked: {
                                NotificationService.invokeAction(notificationDelegate.notificationId, actionData.identifier);
                              }
                            }
                          }
                        }
                      }

                      Item {
                        width: notificationDelegate.buttonClusterWidth
                        height: notificationDelegate.actionButtonSize

                        Row {
                          anchors.right: parent.right
                          spacing: Style.marginXS

                          NIconButton {
                            id: expandButton
                            icon: notificationDelegate.isExpanded ? "chevron-up" : "chevron-down"
                            tooltipText: notificationDelegate.isExpanded ? I18n.tr("notifications.panel.click-to-collapse") || "Click to collapse" : I18n.tr("notifications.panel.click-to-expand") || "Click to expand"
                            baseSize: notificationDelegate.actionButtonSize
                            opacity: (notificationDelegate.canExpand || notificationDelegate.isExpanded) ? 1.0 : 0.0
                            enabled: notificationDelegate.canExpand || notificationDelegate.isExpanded

                            onClicked: {
                              notificationDelegate.pendingLink = "";
                              historyInteractionArea.cursorShape = Qt.ArrowCursor;
                              if (scrollView.expandedId === notificationId) {
                                scrollView.expandedId = "";
                              } else {
                                scrollView.expandedId = notificationId;
                              }
                            }
                          }

                          // Delete button
                          NIconButton {
                            icon: "trash"
                            tooltipText: I18n.tr("tooltips.delete-notification")
                            baseSize: notificationDelegate.actionButtonSize

                            onClicked: {
                              NotificationService.removeFromHistory(notificationId);
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
