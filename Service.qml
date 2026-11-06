pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

// Museum Wallpapers service. All network, file and wallpaper work happens in
// museum.py; this file schedules it, keeps the status JSON it prints, and
// renders the optional desktop placard. The bar widget reads the same status.
Item {
  id: root

  property var shell: null

  readonly property string helperPath: decodeURIComponent(String(Qt.resolvedUrl("museum.py")).replace(/^file:\/\//, ""))

  // Last status printed by the helper (see museum.py:status).
  property var status: ({})
  readonly property var info: status && status.info ? status.info : null
  readonly property bool active: !!status && status.active === true
  readonly property var config: status && status.config ? status.config : ({})
  readonly property string placard: config.placard || "off"
  readonly property string error: status && status.error ? status.error : ""
  readonly property bool busy: helper.running && helper.kind !== "tick"

  // Bar widget instances, one per bar; `info` opens the popup on one of them.
  property var widgets: []
  // One helper run at a time; later requests wait in order. A tick is only
  // queued when nothing else is waiting, since any run reports the status.
  property var pendingRuns: []

  function byline(i) {
    if (!i) return ""
    return [i.artist, i.year].filter(function(x) { return !!x }).join(" · ")
  }

  function run(args) {
    // A Next pressed while another one is still switching is dropped, so a
    // held or repeated hotkey cannot queue up a string of switches.
    if (args[0] === "next" && ((helper.running && helper.kind === "next")
        || root.pendingRuns.some(function(r) { return r[0] === "next" }))) return
    if (helper.running) {
      if (args[0] !== "tick" || root.pendingRuns.length === 0) root.pendingRuns = root.pendingRuns.concat([args])
      return
    }
    helper.kind = args[0] === "filter" ? "filter" : args[0]
    helper.command = ["python3", root.helperPath].concat(args)
    helper.running = true
  }

  function handleOutput(text) {
    var lines = String(text || "").trim().split("\n")
    try {
      var parsed = JSON.parse(lines[lines.length - 1])
      if (parsed && typeof parsed === "object") root.status = parsed
    } catch (e) {
      if (text) console.warn("museum-wallpapers: unexpected helper output: " + text)
    }
  }

  function next() { run(["next"]) }
  // After a settings change: shell.json is written asynchronously.
  function refreshSoon() { refreshTimer.restart() }
  function clearFilter() { run(["filter", "clear", "--switch"]) }
  // kind is "artist" or "movement"; id is a Wikidata id, name what to show.
  function filterBy(kind, id, name) { run(["filter", "set", "--" + kind + "=" + id, "--label=" + name, "--switch"]) }
  function openArticle() { Quickshell.execDetached(["python3", root.helperPath, "open"]) }
  function keep() { Quickshell.execDetached(["python3", root.helperPath, "keep"]) }
  function notify() { Quickshell.execDetached(["python3", root.helperPath, "notify"]) }

  function registerWidget(widget) {
    if (root.widgets.indexOf(widget) === -1) root.widgets = root.widgets.concat([widget])
  }

  function unregisterWidget(widget) {
    root.widgets = root.widgets.filter(function(w) { return w !== widget })
  }

  // The popup when a bar widget is on the bar (preferring the focused
  // monitor's bar), a notification otherwise.
  function showInfo() {
    var target = focusedWidget()
    if (target) target.togglePopup()
    else notify()
  }

  // The settings panel; needs the widget on the bar.
  function showSettings() {
    var target = focusedWidget()
    if (target) target.toggleSettings()
  }

  // The bar widget on the focused monitor, or any, or null without one.
  function focusedWidget() {
    if (root.widgets.length === 0) return null
    var focused = Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name : ""
    var target = root.widgets[0]
    for (var i = 0; i < root.widgets.length; i++) {
      if (root.widgets[i].screenName && root.widgets[i].screenName() === focused) target = root.widgets[i]
    }
    return target
  }

  Process {
    id: helper
    property string kind: ""
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.handleOutput(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("museum-wallpapers: " + text.trim())
    }
    onRunningChanged: {
      if (running || root.pendingRuns.length === 0) return
      var args = root.pendingRuns[0]
      root.pendingRuns = root.pendingRuns.slice(1)
      Qt.callLater(function() { root.run(args) })
    }
  }

  // The helper decides whether the interval has passed, so rotation survives
  // shell restarts and suspend without drifting.
  Timer {
    interval: 60 * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.run(["tick"])
  }

  Timer {
    id: refreshTimer
    interval: 700
    onTriggered: root.run(["status"])
  }

  IpcHandler {
    target: "museum-wallpapers"

    function next(): void { root.next() }
    function info(): void { root.showInfo() }
    function settings(): void { root.showSettings() }
    function notify(): void { root.notify() }
    function open(): void { root.openArticle() }
    function keep(): void { root.keep() }
    function clearFilter(): void { root.clearFilter() }
    function refresh(): void { root.run(["status"]) }
    function status(): string { return JSON.stringify(root.status) }
  }

  // ---------------------------------------------------------------- placard

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: placardWindow

      required property var modelData
      readonly property int edgeMargin: 28

      screen: modelData
      visible: root.placard !== "off" && root.active && root.info !== null
      color: "transparent"

      anchors {
        top: root.placard.indexOf("top") === 0
        bottom: root.placard.indexOf("bottom") === 0
        left: root.placard.indexOf("left") > 0
        right: root.placard.indexOf("right") > 0
      }
      margins {
        top: edgeMargin
        bottom: edgeMargin
        left: edgeMargin
        right: edgeMargin
      }

      implicitWidth: card.width
      implicitHeight: card.height
      exclusiveZone: 0
      // Click-through: the placard never takes input or focus.
      mask: Region {}

      WlrLayershell.namespace: "museum-wallpapers-placard"
      WlrLayershell.layer: WlrLayer.Bottom
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None

      Rectangle {
        id: card
        readonly property int maxTextWidth: 520
        width: Math.min(maxTextWidth, Math.max(titleText.implicitWidth, bylineText.implicitWidth)) + 28
        height: column.implicitHeight + 18
        radius: 6
        color: Qt.rgba(0, 0, 0, 0.45)

        Column {
          id: column
          anchors.centerIn: parent
          width: parent.width - 28
          spacing: 2

          Text {
            id: titleText
            width: parent.width
            textFormat: Text.PlainText
            text: root.info ? root.info.title : ""
            color: "#f2f2f2"
            font.family: Style.font.family
            font.pixelSize: 15
            font.italic: true
            elide: Text.ElideRight
          }

          Text {
            id: bylineText
            width: parent.width
            textFormat: Text.PlainText
            text: root.byline(root.info) + (root.info && root.info.museum ? "  ·  " + root.info.museum : "")
            color: Qt.rgba(1, 1, 1, 0.72)
            font.family: Style.font.family
            font.pixelSize: 12
            elide: Text.ElideRight
          }
        }
      }
    }
  }
}
