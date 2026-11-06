import QtQuick
import Quickshell
import qs.Ui
import qs.Commons
import qs.Commons as Commons

// Bar button for Museum Wallpapers: hover shows "Title — Artist", a click
// opens a card about the current painting, a middle click skips to the next.
// Everything comes from the plugin's service; this file only renders it.
BarWidget {
  id: root
  moduleName: "io.github.dpsk.museum-wallpapers"

  readonly property var service: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("io.github.dpsk.museum-wallpapers") : null
  readonly property var status: service ? service.status : ({})
  readonly property var info: service ? service.info : null
  readonly property bool active: service ? service.active : false
  readonly property bool busy: service ? service.busy : false
  readonly property var activeFilter: status && status.filter ? status.filter : null
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property bool popupOpen: false

  // Interface text from museum.py (strings.json, per the `language` setting);
  // the English given here is used until the first status arrives.
  readonly property var ui: status && status.ui ? status.ui : ({})
  function tr(key, fallback, name) {
    var text = root.ui[key] || fallback
    // split/join rather than replace(): no "$&"-style patterns from names.
    return name === undefined ? text : text.split("{name}").join(name)
  }

  readonly property var intervals: [
    { minutes: 15, key: "interval_15", label: "15m" }, { minutes: 30, key: "interval_30", label: "30m" },
    { minutes: 60, key: "interval_60", label: "1h" }, { minutes: 180, key: "interval_180", label: "3h" },
    { minutes: 360, key: "interval_360", label: "6h" }, { minutes: 1440, key: "interval_1440", label: "1d" },
    { minutes: 0, key: "interval_0", label: "Off" }
  ]
  readonly property int intervalMinutes: service && service.config.intervalMinutes !== undefined
    ? service.config.intervalMinutes : 60
  readonly property var fames: [
    { value: "famous", key: "fame_famous", label: "Famous" }, { value: "known", key: "fame_known", label: "Known" },
    { value: "any", key: "fame_any", label: "Any" }
  ]
  readonly property var fits: [
    { value: "blur", key: "fit_blur", label: "Blur" }, { value: "auto", key: "fit_auto", label: "Auto" },
    { value: "wall", key: "fit_wall", label: "Wall" }, { value: "cover", key: "fit_cover", label: "Fill" }
  ]
  // "Auto", then every interface language as a short code with its own name
  // as the tooltip; the same list as UI_LANGUAGES in museum.py. A constant, so
  // the settings rows are not rebuilt (and tooltips not dropped) by each status.
  readonly property var languages: [
    { value: "auto", key: "lang_auto", label: "Auto" },
    { value: "en", label: "EN", tip: "English" }, { value: "ru", label: "RU", tip: "Русский" },
    { value: "uk", label: "UK", tip: "Українська" }, { value: "de", label: "DE", tip: "Deutsch" },
    { value: "fr", label: "FR", tip: "Français" }, { value: "es", label: "ES", tip: "Español" },
    { value: "it", label: "IT", tip: "Italiano" }, { value: "nl", label: "NL", tip: "Nederlands" },
    { value: "pl", label: "PL", tip: "Polski" }, { value: "pt", label: "PT", tip: "Português" }
  ]

  // Written to this widget's bar entry, like the Omarchy settings editor does;
  // museum.py reads that entry over the `plugins` one.
  function setSetting(name, value) {
    if (!bar || !bar.shell || typeof bar.shell.updateEntryInline !== "function") return
    var next = {}
    for (var key in root.settings) next[key] = root.settings[key]
    next[name] = value
    bar.shell.updateEntryInline(root.moduleName, next)
    if (root.service) root.service.refreshSoon()
  }

  function close() { popupOpen = false }
  function togglePopup() { popupOpen = !popupOpen }
  function screenName() {
    try {
      var window = root.QsWindow.window
      return window && window.screen ? window.screen.name : ""
    } catch (e) {
      return ""
    }
  }

  function filterText(filter) {
    if (!filter) return ""
    var parts = []
    var lists = [filter.artists, filter.movements, filter.genres]
    for (var i = 0; i < lists.length; i++) {
      for (var j = 0; lists[i] && j < lists[i].length; j++) parts.push(lists[i][j].label || lists[i][j].id)
    }
    if (filter.yearFrom !== null && filter.yearFrom !== undefined || filter.yearTo !== null && filter.yearTo !== undefined)
      parts.push((filter.yearFrom !== null && filter.yearFrom !== undefined ? filter.yearFrom : "") + "–"
        + (filter.yearTo !== null && filter.yearTo !== undefined ? filter.yearTo : ""))
    return parts.join(", ")
  }

  implicitWidth: glyph.implicitWidth + Style.space(14)
  implicitHeight: barSize

  Component.onCompleted: if (service) service.registerWidget(root)
  Component.onDestruction: if (service) service.unregisterWidget(root)
  onServiceChanged: if (service) service.registerWidget(root)

  Text {
    id: glyph
    anchors.centerIn: parent
    textFormat: Text.PlainText
    text: "󰏘"
    readonly property color barColor: root.bar ? root.bar.barForeground : root.foreground
    color: root.active ? barColor : Qt.darker(barColor, 1.5)
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    opacity: root.busy ? 0.5 : 1.0
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton | Qt.RightButton

    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) {
        if (root.service) root.service.next()
      } else {
        root.togglePopup()
      }
    }
    onEntered: if (root.bar) root.bar.showTooltip(root, root.info
      ? root.info.title + (root.info.artist ? " — " + root.info.artist : "")
      : root.tr("app", "Museum Wallpapers"))
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(380))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      anchors.fill: parent
      spacing: Style.space(8)

      BorderSurface {
        width: parent.width
        height: Math.round(width * 9 / 16)
        radius: Style.spacing.labelGap
        visible: root.active && !!root.status.image
        color: Style.normalFillFor(root.foreground, Commons.Color.accent)
        borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)

        Image {
          anchors.fill: parent
          anchors.margins: Style.space(2)
          fillMode: Image.PreserveAspectCrop
          asynchronous: true
          cache: false
          sourceSize.width: 760
          source: root.active && root.status.image
            ? "file://" + root.status.image.split("/").map(encodeURIComponent).join("/") : ""
        }
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.info ? root.info.title : (root.active ? "" : root.tr("not_ours", "The current background is not from Museum Wallpapers"))
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.subtitle
        font.bold: true
        wrapMode: Text.Wrap
        maximumLineCount: 3
        elide: Text.ElideRight
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.service ? root.service.byline(root.info) : ""
        color: Qt.darker(root.foreground, 1.2)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
        visible: text !== ""
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.info ? [root.info.museum, root.info.movement].filter(function(x) { return !!x }).join(" · ") : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
        visible: text !== ""
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.info && root.info.summary ? root.info.summary : ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.Wrap
        maximumLineCount: 9
        elide: Text.ElideRight
        lineHeight: 1.15
        visible: text !== ""
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.service ? root.service.error : ""
        color: Commons.Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
        visible: text !== ""
      }

      PanelSeparator { foreground: root.foreground }

      Flow {
        width: parent.width
        spacing: Style.space(6)

        Button {
          iconText: "󰒭"
          text: root.busy ? root.tr("loading", "Loading…") : root.tr("next", "Next")
          foreground: root.foreground
          enabled: !root.busy
          opacity: enabled ? 1.0 : 0.5
          onClicked: if (root.service) root.service.next()
        }

        Button {
          iconText: "󰏌"
          text: root.tr("read_more", "Read more")
          foreground: root.foreground
          visible: !!root.info
          onClicked: {
            if (root.service) root.service.openArticle()
            root.close()
          }
        }

        Button {
          iconText: "󰋑"
          text: root.tr("save", "Save")
          foreground: root.foreground
          visible: !!root.info && root.status.fromCache === true
          onClicked: if (root.service) root.service.keep()
        }
      }

      Flow {
        width: parent.width
        spacing: Style.space(6)
        visible: !!root.info && (!!root.info.artistId || !!root.info.movementId)

        Button {
          text: root.info ? root.tr("more_by", "More by {name}", root.info.artist) : ""
          foreground: root.foreground
          fontSize: Style.font.bodySmall
          visible: !!root.info && !!root.info.artistId
          onClicked: if (root.service) root.service.filterBy("artist", root.info.artistId, root.info.artist)
        }

        Button {
          text: root.info && root.info.movement ? root.tr("more_of", "More {name}", root.info.movement) : ""
          foreground: root.foreground
          fontSize: Style.font.bodySmall
          visible: !!root.info && !!root.info.movementId && !!root.info.movement
          onClicked: if (root.service) root.service.filterBy("movement", root.info.movementId, root.info.movement)
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(2)

        Text {
          id: intervalLabel
          anchors.top: parent.top
          topPadding: Style.space(4)
          textFormat: Text.PlainText
          text: root.tr("change_every", "Change every")
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          rightPadding: Style.space(4)
        }

        Flow {
          width: parent.width - intervalLabel.width - parent.spacing
          spacing: Style.space(2)

          Repeater {
            model: root.intervals

            Button {
              required property var modelData
              text: root.tr(modelData.key, modelData.label)
              foreground: root.foreground
              fontSize: Style.font.caption
              horizontalPadding: Style.space(6)
              selected: root.intervalMinutes === modelData.minutes
              onClicked: root.setSetting("intervalMinutes", modelData.minutes)
            }
          }
        }
      }

      Repeater {
        // Artists (fame), Fit and Language, laid out like the interval row above.
        model: [
          { key: "fame", configKey: "fame", titleKey: "artists", title: "Artists", choices: root.fames, fallback: "famous" },
          { key: "fit", configKey: "fit", titleKey: "fit", title: "Fit", choices: root.fits, fallback: "blur" },
          { key: "language", configKey: "languageSetting", titleKey: "language", title: "Language",
            choices: root.languages, fallback: "auto" }
        ]

        Row {
          id: choiceRow
          required property var modelData
          readonly property string current: root.service && root.service.config[modelData.configKey]
            ? root.service.config[modelData.configKey] : modelData.fallback
          width: parent.width
          spacing: Style.space(2)

          Text {
            id: choiceLabel
            anchors.top: parent.top
            topPadding: Style.space(4)
            width: intervalLabel.implicitWidth
            textFormat: Text.PlainText
            text: root.tr(choiceRow.modelData.titleKey, choiceRow.modelData.title)
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // Wraps: eleven language choices do not fit on one line.
          Flow {
            width: choiceRow.width - choiceLabel.width - choiceRow.spacing
            spacing: Style.space(2)

            Repeater {
              model: choiceRow.modelData.choices

              Button {
                required property var modelData
                text: modelData.key ? root.tr(modelData.key, modelData.label) : modelData.label
                tooltipText: modelData.tip || ""
                foreground: root.foreground
                fontSize: Style.font.caption
                horizontalPadding: Style.space(6)
                selected: choiceRow.current === modelData.value
                onClicked: root.setSetting(choiceRow.modelData.key, modelData.value)
              }
            }
          }
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(6)
        visible: !!root.activeFilter

        Text {
          anchors.verticalCenter: parent.verticalCenter
          width: parent.width - clearButton.width - Style.space(6)
          textFormat: Text.PlainText
          text: root.tr("filter", "Filter: {name}", root.filterText(root.activeFilter))
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }

        Button {
          id: clearButton
          iconText: "󰅖"
          tooltipText: root.tr("clear_filter", "Clear filter")
          foreground: root.foreground
          onClicked: if (root.service) root.service.clearFilter()
        }
      }
    }
  }
}
