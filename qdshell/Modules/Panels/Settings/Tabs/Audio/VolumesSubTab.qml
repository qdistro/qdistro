import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Services.Media
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  property real localVolume: AudioService.volume

  Connections {
    target: AudioService
    function onSinkChanged() {
      localVolume = AudioService.volume;
    }
    function onVolumeChanged() {
      localVolume = AudioService.volume;
    }
  }

  Connections {
    target: AudioService.sink?.audio ? AudioService.sink?.audio : null
    function onVolumeChanged() {
      localVolume = AudioService.volume;
    }
  }

  // Output Volume
  ColumnLayout {
    spacing: Style.marginXXS
    Layout.fillWidth: true

    NValueSlider {
      Layout.fillWidth: true
      label: I18n.tr("panels.osd.types-volume-label")
      description: I18n.tr("panels.audio.volumes-output-volume-description")
      from: 0
      to: Settings.data.audio.volumeOverdrive ? 1.5 : 1.0
      value: localVolume
      stepSize: 0.01
      text: Math.round(AudioService.volume * 100) + "%"
      onMoved: value => localVolume = value
    }

    Timer {
      interval: 100
      running: true
      repeat: true
      onTriggered: {
        if (!AudioService.isSwitchingSink && Math.abs(localVolume - AudioService.volume) >= 0.01) {
          AudioService.setVolume(localVolume);
        }
      }
    }
  }

  // Mute Toggle
  ColumnLayout {
    spacing: Style.marginS
    Layout.fillWidth: true

    NToggle {
      label: I18n.tr("panels.audio.volumes-mute-output-label")
      description: I18n.tr("panels.audio.volumes-mute-output-description")
      checked: AudioService.muted
      onToggled: checked => {
                   if (AudioService.sink && AudioService.sink.audio) {
                     AudioService.sink.audio.muted = checked;
                   }
                 }
    }
  }

  // Volume Feedback sound Toggle
  ColumnLayout {
    spacing: Style.marginS
    Layout.fillWidth: true

    NToggle {
      label: I18n.tr("panels.audio.volumes-volume-feedback-label")
      description: I18n.tr("panels.audio.volumes-volume-feedback-description")
      checked: Settings.data.audio.volumeFeedback
      defaultValue: Settings.getDefaultValue("audio.volumeFeedback")
      onToggled: checked => Settings.data.audio.volumeFeedback = checked
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // Input Volume
  ColumnLayout {
    spacing: Style.marginXS
    Layout.fillWidth: true

    NValueSlider {
      Layout.fillWidth: true
      label: I18n.tr("panels.osd.types-input-volume-label")
      description: I18n.tr("panels.audio.volumes-input-volume-description")
      from: 0
      to: Settings.data.audio.volumeOverdrive ? 1.5 : 1.0
      value: AudioService.inputVolume
      stepSize: 0.01
      text: Math.round(AudioService.inputVolume * 100) + "%"
      onMoved: value => AudioService.setInputVolume(value)
    }
  }

  // Input Mute Toggle
  ColumnLayout {
    spacing: Style.marginS
    Layout.fillWidth: true

    NToggle {
      label: I18n.tr("panels.audio.volumes-mute-input-label")
      description: I18n.tr("panels.audio.volumes-mute-input-description")
      checked: AudioService.inputMuted
      onToggled: checked => AudioService.setInputMuted(checked)
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // The panel and Settings use the same live PipeWire playback streams. A
  // disconnected stream disappears instead of leaving a stale volume row.
  ColumnLayout {
    spacing: Style.marginS
    Layout.fillWidth: true

    NLabel {
      label: I18n.tr("panels.audio.volumes-applications-label")
      description: I18n.tr("panels.audio.volumes-applications-description")
      Layout.fillWidth: true
    }

    PwObjectTracker {
      objects: AudioService.appStreams
    }

    Repeater {
      model: AudioService.appStreams

      NValueSlider {
        required property PwNode modelData
        readonly property var nodeAudio: modelData ? modelData.audio : null
        readonly property var nodeProps: modelData ? modelData.properties : null
        Layout.fillWidth: true
        label: (nodeProps && (nodeProps["application.name"] || nodeProps["application.process.binary"]))
               || modelData.description || modelData.name
               || I18n.tr("panels.audio.volumes-app-unknown")
        from: 0
        to: Settings.data.audio.volumeOverdrive ? 1.5 : 1.0
        value: nodeAudio ? nodeAudio.volume : 0
        stepSize: 0.01
        text: Math.round((nodeAudio ? nodeAudio.volume : 0) * 100) + "%"
        enabled: !!(modelData && modelData.ready && nodeAudio)
        opacity: enabled ? 1.0 : 0.5
        onMoved: value => {
                   if (modelData && modelData.ready && nodeAudio)
                     nodeAudio.volume = value;
                 }

        PwObjectTracker {
          objects: modelData ? [modelData] : []
        }
      }
    }

    NText {
      visible: AudioService.appStreams.length === 0
      text: I18n.tr("panels.audio.panel-applications-empty")
      color: Color.mOnSurfaceVariant
      Layout.fillWidth: true
    }
  }

  // Volume Step Size
  ColumnLayout {
    spacing: Style.marginS
    Layout.fillWidth: true

    NSpinBox {
      Layout.fillWidth: true
      label: I18n.tr("panels.audio.volumes-step-size-label")
      description: I18n.tr("panels.audio.volumes-step-size-description")
      minimum: 1
      maximum: 25
      value: Settings.data.audio.volumeStep
      stepSize: 1
      suffix: "%"
      defaultValue: Settings.getDefaultValue("audio.volumeStep")
      onValueChanged: Settings.data.audio.volumeStep = value
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  // Raise maximum volume above 100%
  ColumnLayout {
    spacing: Style.marginS
    Layout.fillWidth: true

    NToggle {
      label: I18n.tr("panels.audio.volumes-volume-overdrive-label")
      description: I18n.tr("panels.audio.volumes-volume-overdrive-description")
      checked: Settings.data.audio.volumeOverdrive
      defaultValue: Settings.getDefaultValue("audio.volumeOverdrive")
      onToggled: checked => Settings.data.audio.volumeOverdrive = checked
    }
  }
}
