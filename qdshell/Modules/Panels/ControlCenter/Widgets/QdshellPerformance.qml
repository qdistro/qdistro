import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Services.Power
import qs.Widgets

NIconButtonHot {
  property ShellScreen screen

  icon: PowerProfileService.qdshellPerformanceMode ? "rocket" : "rocket-off"
  tooltipText: I18n.tr("tooltips.qdshell-performance-enabled")
  hot: PowerProfileService.qdshellPerformanceMode
  onClicked: PowerProfileService.toggleQdshellPerformance()
}
