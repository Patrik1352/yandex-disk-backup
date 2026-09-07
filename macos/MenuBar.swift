import AppKit
import Foundation

// This app is a view/controller for the Python backup service. It never loads
// the credential file and quitting the app does not terminate a running backup.
private struct Snapshot {
    var state = "idle"
    var message = "Ожидание первого резервного копирования"
    var lastSuccess: String?
    var totalIsEstimate = false
    var nextRun: String?
    var path = ""
    var running = false
    var transferred: Double = 0
    var total: Double = 0
    var filesCompleted = 0
    var filesTotal = 0
    var percent: Double?
    var speed: Double?
    var eta: Double?

    init(_ object: [String: Any] = [:]) {
        state = object["state"] as? String ?? "idle"
        message = object["message"] as? String ?? ""
        lastSuccess = object["last_success"] as? String
        nextRun = object["next_run"] as? String
        running = object["running"] as? Bool ?? ["scanning", "uploading"].contains(state)
        if let progress = object["progress"] as? [String: Any] {
            path = progress["path"] as? String ?? ""
            totalIsEstimate = progress["total_is_estimate"] as? Bool ?? false
            transferred = max(0, (progress["transferred"] as? NSNumber)?.doubleValue ?? 0)
            total = max(0, (progress["total"] as? NSNumber)?.doubleValue ?? 0)
            filesCompleted = max(0, (progress["files_completed"] as? NSNumber)?.intValue ?? 0)
            filesTotal = max(0, (progress["files_total"] as? NSNumber)?.intValue ?? 0)
            if let value = (progress["percent"] as? NSNumber)?.doubleValue, value.isFinite {
                percent = min(100, max(0, value))
            } else if total > 0 && !totalIsEstimate {
                percent = min(100, 100 * transferred / total)
            }
            if let speed = (progress["speed_bytes_per_second"] as? NSNumber)?.doubleValue, speed.isFinite, speed >= 0 {
                self.speed = speed
            }
            if let eta = (progress["eta_seconds"] as? NSNumber)?.doubleValue, eta.isFinite, eta >= 0 {
                self.eta = eta
            }
        }
    }
}

@MainActor
private final class BackupMenuApp: NSObject, NSApplicationDelegate {
    private let files = FileManager.default
    private let configDirectory: URL
    private var item: NSStatusItem!
    private let menu = NSMenu()
    private var timer: Timer?
    private var snapshot = Snapshot()
    private var source: URL?
    private var intervalSeconds: Double = 900
    private var statusModified: Date?
    private var lastReadFailure: Date?
    private var settingsSource: URL?
    private var settingsSourceField: NSTextField?

    private let titleItem = NSMenuItem(title: "Яндекс Бэкап", action: nil, keyEquivalent: "")
    private let stateItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let detailItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let successItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let progressItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let runItem = NSMenuItem(title: "Скопировать сейчас", action: #selector(runNow), keyEquivalent: "")
    private let pauseItem = NSMenuItem(title: "Пауза", action: #selector(togglePause), keyEquivalent: "")
    private let sourceItem = NSMenuItem(title: "Открыть исходную папку", action: #selector(openSource), keyEquivalent: "")

    override init() {
        let arguments = CommandLine.arguments
        if let index = arguments.firstIndex(of: "--config-dir"), index + 1 < arguments.count {
            let path = (arguments[index + 1] as NSString).expandingTildeInPath
            configDirectory = URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL
        } else {
            configDirectory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/YandexBackup", isDirectory: true)
        }
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        menu.autoenablesItems = false
        titleItem.attributedTitle = NSAttributedString(
            string: "Яндекс Бэкап",
            attributes: [.font: NSFont.boldSystemFont(ofSize: NSFont.systemFontSize)]
        )
        for entry in [titleItem, stateItem, detailItem, successItem, progressItem] {
            entry.isEnabled = false
            menu.addItem(entry)
        }
        menu.addItem(.separator())
        for entry in [runItem, pauseItem] {
            entry.target = self
            menu.addItem(entry)
        }
        menu.addItem(.separator())
        sourceItem.target = self
        menu.addItem(sourceItem)
        addAction("Открыть Яндекс.Диск", #selector(openDisk))
        addAction("Открыть журнал", #selector(openLog))
        addAction("Настройки…", #selector(openSettings))
        menu.addItem(.separator())
        addAction("Закрыть значок (бэкап продолжится)", #selector(quit))
        item.menu = menu
        refresh()
        let refreshTimer = Timer(timeInterval: 2, target: self,
                                 selector: #selector(refresh), userInfo: nil, repeats: true)
        RunLoop.main.add(refreshTimer, forMode: .common)
        timer = refreshTimer
    }

    private func addAction(_ title: String, _ selector: Selector) {
        let entry = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        entry.target = self
        menu.addItem(entry)
    }

    private func file(_ name: String) -> URL {
        configDirectory.appendingPathComponent(name, isDirectory: false)
    }

    private func readObject(_ url: URL) -> [String: Any]? {
        guard let attributes = try? files.attributesOfItem(atPath: url.path),
              let size = attributes[.size] as? NSNumber,
              size.intValue <= 1_048_576,
              let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return object
    }

    @objc private func refresh() {
        if let config = readObject(file("config.json")) {
            if let path = config["source"] as? String, path.hasPrefix("/") {
                source = URL(fileURLWithPath: path, isDirectory: true)
            }
            intervalSeconds = max(60, (config["interval_seconds"] as? NSNumber)?.doubleValue ?? 900)
        }
        if let data = readObject(file("status.json")) {
            snapshot = Snapshot(data)
            statusModified = (try? files.attributesOfItem(atPath: file("status.json").path))?[.modificationDate] as? Date
            lastReadFailure = nil
        } else if lastReadFailure == nil {
            // Keep the previous value while an external writer replaces a file.
            lastReadFailure = Date()
        }

        let paused = files.fileExists(atPath: file("paused").path)
        let queued = files.fileExists(atPath: file("run-now").path)
        let stale = statusModified.map {
            Date().timeIntervalSince($0) >= 30
        } ?? false
        let unreadable = lastReadFailure.map { Date().timeIntervalSince($0) > 15 } ?? false
        var state = snapshot.state
        var detail = snapshot.message
        if stale {
            state = "offline"
            detail = "Нет связи с фоновым процессом. Проверьте журнал."
        } else if unreadable {
            state = "offline"
            detail = "Статус пока недоступен. Проверьте журнал."
        } else if paused {
            state = "paused"
            detail = snapshot.running ? "Текущие передачи завершатся перед паузой" : "Автоматическое копирование приостановлено"
        } else if queued, !snapshot.running {
            detail = "Резервное копирование запрошено"
        }

        let labels: [String: String] = [
            "idle": snapshot.lastSuccess == nil ? "Первая копия ещё не завершена" : "Копирование завершено ✓",
            "scanning": snapshot.lastSuccess == nil ? "Первая копия: подготовка…" : "Проверка изменений…",
            "uploading": snapshot.lastSuccess == nil ? "Первая копия: сохранение…" : "Сохранение изменений…",
            "paused": "Пауза",
            "error": "Не удалось завершить копирование",
            "offline": "Нет соединения с Яндекс.Диском"
        ]
        stateItem.title = stale || unreadable
            ? "Нет связи с фоновым процессом"
            : (labels[state] ?? "Состояние: \(state)")
        if !stale && !unreadable && !paused && !queued {
            if state == "scanning" {
                detail = "Проверяем следующую папку; готовые файлы уже сохранены"
            } else if state == "uploading" {
                detail = "Передача и проверка копий; дождитесь «Копирование завершено»"
            } else if state == "idle", snapshot.lastSuccess != nil {
                detail = "Следующая проверка: \(formatDate(snapshot.nextRun))"
            }
        }
        detailItem.title = shortened(detail, limit: 88)
        detailItem.toolTip = detail
        detailItem.isHidden = detail.isEmpty || detail == stateItem.title
        successItem.title = snapshot.lastSuccess == nil ? "Полной успешной копии пока нет" : "Последняя успешная копия: \(formatDate(snapshot.lastSuccess))"
        progressItem.title = state == "scanning"
            ? "Найдено файлов: \(snapshot.filesTotal) · \(shortened(snapshot.path, limit: 65))"
            : formatProgress()
        progressItem.toolTip = snapshot.path
        progressItem.isHidden = !snapshot.running || stale

        pauseItem.title = paused ? "Возобновить" : "Пауза"
        pauseItem.toolTip = paused ? "Разрешить автоматические копии" : "Приостановить после завершения текущих передач"
        runItem.isEnabled = !paused && (!snapshot.running || stale) && !queued
        runItem.title = queued ? "Копирование запрошено…" : "Скопировать сейчас"
        sourceItem.isEnabled = source != nil

        let symbols: [String: String] = [
            "idle": "externaldrive.badge.checkmark", "scanning": "arrow.triangle.2.circlepath",
            "uploading": "icloud.and.arrow.up", "paused": "pause.circle",
            "error": "exclamationmark.icloud", "offline": "icloud.slash"
        ]
        if let button = item.button {
            let image = NSImage(systemSymbolName: symbols[state] ?? "externaldrive", accessibilityDescription: "Яндекс Бэкап: \(stateItem.title)")
            image?.isTemplate = true
            button.image = image
            if state == "uploading", let percent = snapshot.percent {
                button.title = " \(min(99, Int(percent)))%"
            } else {
                let titles = ["idle": snapshot.lastSuccess == nil ? "Ожидание" : "Готово",
                              "scanning": "Проверка", "uploading": "Сохранение",
                              "paused": "Пауза", "error": "Ошибка", "offline": "Нет связи"]
                button.title = " " + (titles[state] ?? "ЯД")
            }
            button.toolTip = "Яндекс Бэкап — \(stateItem.title)\n\(successItem.title)"
        }
    }

    private func shortened(_ text: String, limit: Int) -> String {
        let singleLine = text.components(separatedBy: .newlines).joined(separator: " ")
        return singleLine.count > limit ? String(singleLine.prefix(limit - 1)) + "…" : singleLine
    }

    private func formatDate(_ text: String?) -> String {
        guard let text = text else { return "ещё не было" }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = parser.date(from: text)
        if date == nil {
            parser.formatOptions = [.withInternetDateTime]
            date = parser.date(from: text)
        }
        guard let date = date else { return "дата недоступна" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        formatter.doesRelativeDateFormatting = true
        return formatter.string(from: date)
    }

    private func formatProgress() -> String {
        var pieces: [String] = []
        if let percent = snapshot.percent { pieces.append("Передано \(Int(percent))% байтов") }
        if snapshot.filesTotal > 0 {
            pieces.append("файлов: \(snapshot.filesCompleted) / \(snapshot.filesTotal)")
        }
        if snapshot.total > 0, snapshot.total.isFinite, snapshot.transferred.isFinite {
            let formatter = ByteCountFormatter()
            formatter.countStyle = .file
            let transferred = formatter.string(fromByteCount: Int64(min(snapshot.transferred, Double(Int64.max / 2))))
            let total = formatter.string(fromByteCount: Int64(min(snapshot.total, Double(Int64.max / 2))))
            pieces.append(snapshot.totalIsEstimate ? "передано \(transferred)" : "\(transferred) из \(total)")
        }
        if let speed = snapshot.speed {
            pieces.append(ByteCountFormatter.string(fromByteCount: Int64(min(speed, Double(Int64.max / 2))), countStyle: .file) + "/с")
        }
        if let eta = snapshot.eta {
            let seconds = Int(min(eta, Double(Int.max / 2)))
            let remaining = seconds < 60 ? "меньше минуты" : (seconds < 3600 ? "\(seconds / 60) мин" : "\(seconds / 3600) ч \((seconds % 3600) / 60) мин")
            pieces.append("осталось \(remaining)")
        }
        return pieces.isEmpty ? "Подготовка копирования…" : pieces.joined(separator: " · ")
    }

    private func writeMarker(_ name: String) throws {
        try files.createDirectory(at: configDirectory, withIntermediateDirectories: true,
                                  attributes: [.posixPermissions: 0o700])
        let value = ISO8601DateFormatter().string(from: Date()) + "\n"
        try Data(value.utf8).write(to: file(name), options: .atomic)
        try files.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file(name).path)
    }

    private func showError(_ description: String, _ error: Error? = nil) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = description
        alert.informativeText = error?.localizedDescription ?? "Проверьте, что служба резервного копирования установлена."
        alert.addButton(withTitle: "Понятно")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    @objc private func runNow() {
        do { try writeMarker("run-now"); refresh() }
        catch { showError("Не удалось запросить резервную копию", error) }
    }

    @objc private func togglePause() {
        do {
            if files.fileExists(atPath: file("paused").path) {
                try files.removeItem(at: file("paused"))
            } else {
                try writeMarker("paused")
            }
            refresh()
        } catch { showError("Не удалось изменить режим копирования", error) }
    }

    @objc private func openSource() {
        guard let source = source else { return }
        NSWorkspace.shared.open(source)
    }

    @objc private func openDisk() {
        guard let application = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "ru.yandex.desktop.disk2") else {
            showError("Приложение Яндекс.Диск не найдено", NSError(domain: "YandexBackup", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Установите приложение Яндекс.Диск в папку «Программы». "]))
            return
        }
        NSWorkspace.shared.openApplication(at: application, configuration: NSWorkspace.OpenConfiguration()) { _, error in
            if let error = error {
                Task { @MainActor in self.showError("Не удалось открыть Яндекс.Диск", error) }
            }
        }
    }

    @objc private func openLog() { openDocument("backup.log") }

    @objc private func openSettings() {
        guard let config = readObject(file("config.json")) else {
            showError("Не удалось прочитать настройки")
            return
        }
        settingsSource = (config["source"] as? String).flatMap {
            $0.hasPrefix("/") ? URL(fileURLWithPath: $0, isDirectory: true) : nil
        }
        defer { settingsSourceField = nil; settingsSource = nil }

        let alert = NSAlert()
        alert.messageText = "Настройки резервного копирования"
        alert.informativeText = "Выберите папку и частоту копирования. Изменения применятся при следующем запуске."
        alert.addButton(withTitle: "Сохранить")
        alert.addButton(withTitle: "Отмена")
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 460, height: 158))

        let sourceLabel = NSTextField(labelWithString: "Папка для резервного копирования")
        sourceLabel.frame = NSRect(x: 0, y: 132, width: 440, height: 20)
        content.addSubview(sourceLabel)
        let pathField = NSTextField(labelWithString: settingsSource?.path ?? "Папка не выбрана")
        pathField.frame = NSRect(x: 0, y: 98, width: 340, height: 24)
        pathField.lineBreakMode = .byTruncatingMiddle
        pathField.isSelectable = true
        pathField.toolTip = settingsSource?.path
        pathField.setAccessibilityLabel("Выбранная папка")
        settingsSourceField = pathField
        content.addSubview(pathField)
        let chooseButton = NSButton(title: "Выбрать…", target: self, action: #selector(chooseSettingsSource))
        chooseButton.bezelStyle = .rounded
        chooseButton.frame = NSRect(x: 352, y: 94, width: 108, height: 30)
        content.addSubview(chooseButton)

        let intervalLabel = NSTextField(labelWithString: "Копировать каждые")
        intervalLabel.frame = NSRect(x: 0, y: 56, width: 175, height: 24)
        content.addSubview(intervalLabel)
        let seconds = (config["interval_seconds"] as? NSNumber)?.doubleValue ?? 900
        let minutes = seconds.isFinite ? Int(min(1440, max(1, seconds / 60))) : 15
        let intervalField = NSTextField(string: String(minutes))
        intervalField.frame = NSRect(x: 181, y: 54, width: 80, height: 26)
        intervalField.alignment = .right
        intervalField.setAccessibilityLabel("Интервал в минутах")
        content.addSubview(intervalField)
        let minutesLabel = NSTextField(labelWithString: "минут (от 1 до 1440)")
        minutesLabel.frame = NSRect(x: 271, y: 56, width: 189, height: 24)
        content.addSubview(minutesLabel)
        let validation = NSTextField(wrappingLabelWithString: "")
        validation.frame = NSRect(x: 0, y: 0, width: 460, height: 42)
        validation.textColor = .systemRed
        validation.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        content.addSubview(validation)
        alert.accessoryView = content
        alert.window.initialFirstResponder = intervalField

        NSApp.activate(ignoringOtherApps: true)
        while alert.runModal() == .alertFirstButtonReturn {
            guard let minutes = Int(intervalField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)),
                  (1...1440).contains(minutes) else {
                validation.stringValue = "Введите целое число минут от 1 до 1440."
                continue
            }
            guard let selected = settingsSource,
                  (try? selected.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true else {
                validation.stringValue = "Выберите существующую папку для резервного копирования."
                continue
            }
            // Re-read immediately before writing so settings edited elsewhere
            // while this dialog was open are preserved. Credentials are never read.
            guard var current = readObject(file("config.json")) else {
                showError("Не удалось сохранить: настройки сейчас недоступны")
                return
            }
            current["source"] = selected.path
            current["interval_seconds"] = minutes * 60
            do {
                var data = try JSONSerialization.data(withJSONObject: current, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
                data.append(0x0A)
                try data.write(to: file("config.json"), options: .atomic)
                try files.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file("config.json").path)
                refresh()
            } catch {
                showError("Не удалось сохранить настройки", error)
            }
            return
        }
    }

    @objc private func chooseSettingsSource() {
        let chooser = NSOpenPanel()
        chooser.title = "Папка для резервного копирования"
        chooser.prompt = "Выбрать папку"
        chooser.canChooseFiles = false
        chooser.canChooseDirectories = true
        chooser.allowsMultipleSelection = false
        chooser.canCreateDirectories = false
        chooser.directoryURL = settingsSource
        if chooser.runModal() == .OK, let selected = chooser.url {
            settingsSource = selected
            settingsSourceField?.stringValue = selected.path
            settingsSourceField?.toolTip = selected.path
        }
    }

    private func openDocument(_ name: String) {
        let url = file(name)
        guard files.fileExists(atPath: url.path) else {
            showError(name == "backup.log" ? "Журнал пока не создан" : "Настройки пока не созданы")
            return
        }
        // Use a readable text viewer regardless of the log's file association.
        if let textEdit = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.TextEdit") {
            NSWorkspace.shared.open([url], withApplicationAt: textEdit,
                                    configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func quit() {
        timer?.invalidate()
        NSApp.terminate(nil)
    }
}

@main
private enum MenuBarMain {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = BackupMenuApp()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
