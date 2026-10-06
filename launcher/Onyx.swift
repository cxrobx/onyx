// Onyx — resilient macOS launcher.
//
// Production builds ship the FastAPI service as a bundled executable. A
// checkout-local runtime remains as a development fallback, but the installed
// app never depends on ~/Projects/ask-widget or its .venv.
import Cocoa
import Sparkle
@preconcurrency import WebKit
import UniformTypeIdentifiers

private let port = 8899
private let baseURL = "http://127.0.0.1:\(port)"
private let expectedService = "onyx"
private let expectedProtocol = 3
/// The LaunchAgent scripts/install-daemon.sh installs, and the one app `scripts/onyx-daemon.sh` runs the service from.
/// An update replaces the app but not a service already running: see `staleBackgroundServiceVersion`.
private let daemonLabel = "com.cx.onyx.server"
private let daemonAppPath = "/Applications/Onyx.app"

private enum HealthResult {
    case healthy(providerAvailable: Bool, version: String?)
    case unavailable(String)
    case incompatible(String)
}

private struct ServerLaunch {
    let executable: URL
    let arguments: [String]
    let workingDirectory: URL
    let label: String
}

private struct DockRecentItem: Decodable {
    let title: String
    let path: String
    let folder: String
}

private struct DockRecentResponse: Decodable {
    let artifacts: [DockRecentItem]
    let notes: [DockRecentItem]
}

/// Each theme's opaque window colour, as sRGB 0–255. Must track `--bg-primary`
/// in `launcher_ui.py`: this paints the window before any page exists, and the
/// page sends the same triple whenever glass goes off.
private let baseRGB: (light: [Double], dark: [Double]) = ([247, 247, 247], [24, 24, 24])
private let appearanceDefaultsKey = "appearanceTheme"
/// The ground the shell last gave the glass: the first paint at launch, before any page has spoken.
private let launchBaseDefaultsKey = "launchBaseRGB"
/// `defaults write com.cx.onyx customIcon -bool false` keeps the shipped icon (and removes the custom one).
private let customIconDefaultsKey = "customIcon"

/// Gives the app the transparent gem as its custom icon, the one Finder's Get Info paste sets.
///
/// The shipped icon (Assets.car) is the gem on a dark square, because macOS 26 draws every
/// bundle icon as a rounded square and puts a free-standing one in a grey square of its own.
/// A custom icon overrides that on every macOS, so the gem stands alone again. It is set on the
/// installed app after Gatekeeper has opened it, never in the download: it writes `Icon\r` and a
/// Finder-info attribute into the bundle, which `codesign --verify` then reports, as it does for any
/// custom app icon. When the bundle isn't writable (a shared /Applications, App Translocation), the
/// call fails and the dark square stays: that is the fallback. An update replaces the bundle and
/// its first launch sets the icon again. Sparkle installs over a bundle that carries the icon without
/// complaint: tested 2026-09-30 with a signed copy that had set it.
private func adoptCustomIcon() -> String {
    let bundle = Bundle.main.bundleURL
    guard bundle.pathExtension == "app" else { return "not an app bundle" }
    let marker = bundle.appendingPathComponent("Icon\r")
    let present = FileManager.default.fileExists(atPath: marker.path)
    if UserDefaults.standard.object(forKey: customIconDefaultsKey) as? Bool == false {
        if present { NSWorkspace.shared.setIcon(nil, forFile: bundle.path, options: []) }
        return present ? "removed" : "off"
    }
    guard let icns = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
          let gem = NSImage(contentsOf: icns) else { return "no AppIcon.icns" }
    NSApp.applicationIconImage = gem
    if present { return "already set" }
    return NSWorkspace.shared.setIcon(gem, forFile: bundle.path, options: []) ? "set" : "couldn't set; keeping the shipped icon"
}

private func srgb(_ components: [Double]) -> NSColor {
    NSColor(
        srgbRed: components[0] / 255, green: components[1] / 255,
        blue: components[2] / 255, alpha: 1
    )
}

private func appearance(forTheme theme: String?) -> NSAppearance? {
    switch theme {
    case "dark": return NSAppearance(named: .darkAqua)
    case "light": return NSAppearance(named: .aqua)
    default: return nil
    }
}

/// WebKit's stock right-click menu — "Reload" and "Inspect Element", or "Open
/// Link in New Window" on a sidebar row — can't be styled, so Onyx draws its own
/// (static/app-menu.js) and this suppresses the stock one wherever a page did
/// not claim the event: the cxtasks rule (its gotcha #9). Two places keep it:
/// editable fields, where cut/copy/paste and spelling live, and a document in
/// the reader, whose links, media and selected text carry the document's own
/// actions (Copy Link, Copy Image, Copy). It listens on `window` in the bubble
/// phase, so every page handler has had its turn first. tests/browser_smoke.py
/// runs this exact source.
private let stockMenuGuard = """
window.addEventListener('contextmenu', (event) => {
  if (event.defaultPrevented) return;
  const node = event.target;
  const el = node instanceof Element ? node : node && node.parentElement;
  if (el && el.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return;
  const reading = location.pathname === '/view' || location.pathname.startsWith('/_fs/');
  const selection = getSelection();
  if (reading && ((el && el.closest('a[href], img, video, audio')) || (selection && !selection.isCollapsed))) return;
  event.preventDefault();
});
"""

// MARK: - Window glass

/// Blur the desktop behind the window with a radius we choose.
///
/// `NSVisualEffectView` is not a blur. It is a material — Apple's radius, tint
/// and saturation boost welded into one enum case with no dial on any of it —
/// and in dark mode `.underWindowBackground` reads as flat milky grey that eats
/// the colour and the shape of whatever is behind the window. The transparency
/// slider only ever moved the tint painted on top; the smear underneath was
/// fixed. `CGSSetWindowBackgroundBlurRadius` is a plain Gaussian at a radius we
/// pass in, with no material, so the wallpaper stays itself and the page's
/// pane tints (`--pane-alpha` …) are the only thing colouring it. Ported from
/// cxtasks 767a8fb / cxmail 70087f5.
///
/// It is a private WindowServer symbol, so it is resolved with `dlsym` and never
/// linked: a macOS that stops exporting it gives `nil`, not a dyld abort, and
/// the window falls back to the material this app shipped before. Mac App Store
/// distribution was never on the table for a launcher that spawns CLIs.
///
/// Switching blur source moved three jobs onto us that the material did for free:
/// - **The launch gap.** A clear window around an empty WebView is bare
///   wallpaper and three floating traffic lights. The window boots OPAQUE and the
///   page arms glass after its first paint (`glass_script` in launcher_ui.py).
/// - **The window's edges.** Clear to alpha 0.01, not 0, or AppKit chamfers the
///   corners against the shadow; invalidate the shadow on every opacity flip; drop
///   the radius to 0 BEFORE going opaque, or one frame paints a grey halo.
/// - **Reduce Transparency.** A raw CGS blur reads no accessibility setting. It is
///   read live, observed, and pins the window opaque whatever the slider says;
///   the page pins its CSS alphas the same way.
private final class WindowGlass {
    private typealias ConnectionFn = @convention(c) () -> Int32
    private typealias SetBlurFn = @convention(c) (Int32, UInt32, Int32) -> Int32

    /// A sanity bound, not the design range (the page's curve sweeps 10–48).
    private static let radiusBounds = 4...64

    weak var window: NSWindow?
    var onReduceTransparencyChange: ((Bool) -> Void)?
    private let setBlur: SetBlurFn?
    private let connection: ConnectionFn?
    /// What the page last asked for. Kept apart from what is applied, so a
    /// Reduce Transparency flip in either direction restores the user's glass.
    private var desired: (enabled: Bool, radius: Int, base: NSColor)?
    private var fallback: NSVisualEffectView?
    private var observer: NSObjectProtocol?

    init() {
        let everywhere = UnsafeMutableRawPointer(bitPattern: -2)  // RTLD_DEFAULT
        setBlur = dlsym(everywhere, "CGSSetWindowBackgroundBlurRadius")
            .map { unsafeBitCast($0, to: SetBlurFn.self) }
        // Renamed across OS versions; both still ship on some. The function is
        // resolved once but called every time — the per-thread variant must be.
        connection = (dlsym(everywhere, "CGSDefaultConnectionForThread")
            ?? dlsym(everywhere, "CGSMainConnectionID"))
            .map { unsafeBitCast($0, to: ConnectionFn.self) }
    }

    var isAvailable: Bool { setBlur != nil && connection != nil }

    /// Read fresh every time: caching it is how the setting ends up needing a relaunch.
    var reduceTransparency: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency
    }

    func setState(enabled: Bool, radius: Int, base: NSColor) {
        desired = (enabled, radius.clamped(to: Self.radiusBounds), base)
        applyDesired()
    }

    /// The slider's continuous path: no window setup, just the radius.
    func setRadius(_ radius: Int) {
        guard var current = desired else { return }
        current.radius = radius.clamped(to: Self.radiusBounds)
        desired = current
        if current.enabled && !reduceTransparency && isAvailable {
            applyRadius(current.radius)
        }
    }

    /// The opaque state: at launch, and whenever glass is off.
    func paintOpaque(_ base: NSColor) {
        guard let window else { return }
        applyRadius(0)
        window.isOpaque = true
        window.backgroundColor = base
        window.invalidateShadow()
    }

    func installObserver() {
        guard observer == nil else { return }
        // ⚠ `defaults write com.apple.universalaccess reduceTransparency` edits the
        // plist WITHOUT posting this, so only the real System Settings toggle fires it.
        observer = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            self.applyDesired()
            self.onReduceTransparencyChange?(self.reduceTransparency)
        }
    }

    private func applyDesired() {
        guard let desired else { return }
        if desired.enabled && !reduceTransparency {
            enable(radius: desired.radius)
        } else {
            paintOpaque(desired.base)
        }
    }

    private func enable(radius: Int) {
        guard let window else { return }
        window.isOpaque = false
        window.backgroundColor = NSColor.clear.withAlphaComponent(0.01)
        window.hasShadow = true
        window.invalidateShadow()
        if isAvailable {
            applyRadius(radius)
        } else {
            installFallback(in: window)
        }
    }

    /// The material, once, behind everything — only when the CGS symbol is gone.
    private func installFallback(in window: NSWindow) {
        guard fallback == nil, let content = window.contentView else { return }
        let material = NSVisualEffectView(frame: content.bounds)
        material.material = .underWindowBackground
        material.blendingMode = .behindWindow
        material.state = .active
        material.autoresizingMask = [.width, .height]
        content.addSubview(material, positioned: .below, relativeTo: nil)
        fallback = material
        NSLog("Onyx glass: CGS blur unavailable; using NSVisualEffectView")
    }

    private func applyRadius(_ radius: Int) {
        guard let setBlur, let connection, let window else { return }
        // Assigned only once the window is ordered in; 0 or less would blur
        // some other window or nothing at all.
        let number = window.windowNumber
        guard number > 0 else { return }
        let status = setBlur(connection(), UInt32(truncatingIfNeeded: number), Int32(radius))
        if status != 0 {
            NSLog("Onyx glass: CGSSetWindowBackgroundBlurRadius(%ld) returned %d", radius, status)
        }
    }
}

private extension Int {
    func clamped(to range: ClosedRange<Int>) -> Int {
        Swift.min(Swift.max(self, range.lowerBound), range.upperBound)
    }
}

// MARK: - Swipe cover

/// A picture of the page a back/forward swipe landed on, held over the WebView
/// until the reader has painted it.
///
/// WebKit's swipe slides in a snapshot of the page it is going to and lifts the
/// snapshot once the MAIN frame has painted. Every Back and Forward in Onyx moves
/// the reader frame, so WebKit lifted it while the reader still held the page
/// just left: after every swipe that page showed for a frame, 70–600 ms after
/// the slide ended, and read as a reload. Measured on the real window at 45 fps.
/// It never takes a click: the page underneath keeps the mouse, as it keeps the
/// scroll, and is only transparent while covered.
private final class SwipeCover: NSView {
    init(image: CGImage, frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.contents = image
        layer?.contentsGravity = .resize
        autoresizingMask = [.width, .height]
    }

    required init?(coder: NSCoder) { nil }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

/// Whether `url` is somewhere other than the local service — the test for "hand this to the browser".
///
/// Onyx is a reader, not a browser: a page from another site loaded in here arrives with no address bar, no tab of its
/// own and the shell's chrome around it, and a Markdown page's link used to take the whole window with it. A page's own
/// machinery (`javascript:`, `about:`, `data:`, `blob:`) never travels; everything that is not the service's own origin
/// does, `mailto:`, `file:` and another app's scheme included — Launch Services knows what to do with each, and the
/// reader does not.
private func leavesOnyx(_ url: URL) -> Bool {
    let scheme = (url.scheme ?? "").lowercased()
    if ["javascript", "about", "data", "blob"].contains(scheme) { return false }
    guard scheme == "http" || scheme == "https", let base = URL(string: baseURL) else { return true }
    return !(scheme == base.scheme && url.host == base.host && url.port == base.port)
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
    WKUIDelegate, WKScriptMessageHandlerWithReply
{
    private var window: NSWindow!
    private var webView: WKWebView?
    private var statusLabel: NSTextField?
    private var server: Process?
    private var didSpawn = false
    private var startupID: UUID?
    private var showingFailure = false
    private var logHandle: FileHandle?
    private var pendingDocumentURL: URL?
    private var pendingQuickText: String?
    private var keyDownMonitor: Any?
    private let glass = WindowGlass()
    private var swipeCover: SwipeCover?
    private var swipeCoverDeadline: DispatchWorkItem?
    private var pageOnlyItem: NSMenuItem?
    private var dockArtifacts: [DockRecentItem] = []
    private var dockNotes: [DockRecentItem] = []
    private var dockRefreshTimer: Timer?
    private var dockRefreshInFlight = false
    /// Sparkle: checks the appcast named by SUFeedURL in Info.plist each day and on the menu item, verifies the
    /// download's EdDSA signature (SUPublicEDKey) before extracting it, and asks before installing anything. The
    /// controller starts its updater when it is created, so it is made once the app has finished launching.
    private var updaterController: SPUStandardUpdaterController!
    private let zoomLevels: [CGFloat] = [
        0.50, 0.67, 0.80, 0.90, 1.00, 1.10, 1.25, 1.50, 1.75, 2.00, 2.50, 3.00,
    ]

    private lazy var logURL: URL = {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Onyx", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
        return directory.appendingPathComponent("onyx.log")
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.servicesProvider = self
        updaterController = SPUStandardUpdaterController(
            startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil
        )
        // The last theme a page chose, so the launch window and its title bar
        // come up in it rather than flashing the system appearance first.
        NSApp.appearance = appearance(
            forTheme: UserDefaults.standard.string(forKey: appearanceDefaultsKey)
        )
        glass.onReduceTransparencyChange = { [weak self] on in
            // The window half has already applied; this is the CSS half.
            self?.webView?.evaluateJavaScript(
                "window.askwReduceTransparency && window.askwReduceTransparency(\(on))"
            )
        }
        glass.installObserver()
        buildMenu()
        installKeyboardShortcuts()
        buildLoadingWindow()
        beginStartup()
        DispatchQueue.main.async { [weak self] in
            // The system log too: onyx.log is open only when this app started the service itself.
            let outcome = adoptCustomIcon()
            NSLog("Onyx custom icon: %@", outcome)
            self?.writeLog("Custom icon: \(outcome)\n")
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        startupID = nil
        dockRefreshTimer?.invalidate()
        if let keyDownMonitor { NSEvent.removeMonitor(keyDownMonitor) }
        keyDownMonitor = nil
        stopOwnedServer()
        closeLog()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows: Bool
    ) -> Bool {
        if !hasVisibleWindows { window?.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        refreshDockRecents()
        let menu = NSMenu(title: "Onyx")
        menu.autoenablesItems = false
        menu.addItem(dockAction("Open Onyx", symbol: "rectangle.on.rectangle", action: #selector(showOnyx)))
        menu.addItem(.separator())
        addDockSection("Recent Artifacts", symbol: "curlybraces.square", items: dockArtifacts, to: menu)
        menu.addItem(.separator())
        addDockSection("Recent Notes", symbol: "note.text", items: dockNotes, to: menu)
        menu.addItem(.separator())
        menu.addItem(dockAction("Search…", symbol: "magnifyingglass", action: #selector(openSearch)))
        menu.addItem(dockAction("Open Document…", symbol: "doc.badge.plus", action: #selector(openDocument)))
        return menu
    }

    private func dockAction(_ title: String, symbol: String, action: Selector) -> NSMenuItem {
        let item = menuItem(title, action, "")
        item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        return item
    }

    private func addDockSection(_ title: String, symbol: String, items: [DockRecentItem], to menu: NSMenu) {
        let heading = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        heading.isEnabled = false
        menu.addItem(heading)
        if items.isEmpty {
            let empty = NSMenuItem(title: "No recent items", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            menu.addItem(empty)
            return
        }
        for recent in items {
            let item = dockAction(recent.title, symbol: symbol, action: #selector(openDockDocument(_:)))
            item.representedObject = recent.path
            item.toolTip = recent.folder.isEmpty ? recent.path : "\(recent.folder) · \(recent.path)"
            menu.addItem(item)
        }
    }

    private func refreshDockRecents() {
        guard webView != nil, !dockRefreshInFlight,
              let url = URL(string: "\(baseURL)/api/dock/recent") else { return }
        dockRefreshInFlight = true
        var request = URLRequest(url: url)
        request.timeoutInterval = 3
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            let recent = (response as? HTTPURLResponse)?.statusCode == 200
                ? data.flatMap { try? JSONDecoder().decode(DockRecentResponse.self, from: $0) }
                : nil
            DispatchQueue.main.async {
                guard let self else { return }
                self.dockRefreshInFlight = false
                if let recent {
                    self.dockArtifacts = recent.artifacts
                    self.dockNotes = recent.notes
                }
            }
        }.resume()
    }

    @objc private func showOnyx() {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func openDockDocument(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        openDocumentURL(URL(fileURLWithPath: path))
        refreshDockRecents()
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.first else { return }
        if webView == nil {
            pendingDocumentURL = url
        } else {
            openDocumentURL(url)
        }
    }

    @objc func askSelection(
        _ pasteboard: NSPasteboard,
        userData: String,
        error: AutoreleasingUnsafeMutablePointer<NSString?>
    ) {
        guard let text = pasteboard.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else {
            error.pointee = "Onyx did not receive any selected text."
            return
        }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if self.webView == nil {
                self.pendingQuickText = String(text.prefix(20_000))
                self.window?.makeKeyAndOrderFront(nil)
            } else {
                self.openQuickSelection(String(text.prefix(20_000)))
            }
        }
    }

    @objc func openDocumentWithOnyx(
        _ pasteboard: NSPasteboard,
        userData: String,
        error: AutoreleasingUnsafeMutablePointer<NSString?>
    ) {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [
            .urlReadingFileURLsOnly: true,
        ]
        guard let urls = pasteboard.readObjects(
            forClasses: [NSURL.self], options: options
        ) as? [URL], let url = urls.first else {
            error.pointee = "Onyx did not receive a document."
            return
        }
        guard isSupportedDocument(url) else {
            error.pointee = "Onyx supports HTML, Markdown, text, and PDF documents."
            return
        }

        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if self.webView == nil {
                self.pendingDocumentURL = url
                self.window?.makeKeyAndOrderFront(nil)
            } else {
                self.openDocumentURL(url)
            }
        }
    }

    // MARK: - Server lifecycle

    private func beginStartup() {
        let id = UUID()
        startupID = id
        showingFailure = false
        updateStatus("Checking local service…")

        checkHealth { [weak self] result in
            DispatchQueue.main.async {
                guard let self, self.startupID == id else { return }
                switch result {
                case .healthy(let providerAvailable, let version):
                    if let stale = self.staleBackgroundServiceVersion(reported: version) {
                        self.restartBackgroundService(
                            from: stale, providerAvailable: providerAvailable, id: id
                        )
                    } else {
                        self.openApp(providerAvailable: providerAvailable)
                    }
                case .incompatible(let detail):
                    self.failStartup(
                        "Port \(port) is already in use by another service.",
                        detail: detail,
                        id: id
                    )
                case .unavailable:
                    self.startServer(id: id)
                }
            }
        }
    }

    private func startServer(id: UUID) {
        guard let launch = resolveServerLaunch() else {
            failStartup(
                "The Onyx service is missing.",
                detail: "Rebuild the app with launcher/build-app.sh. No bundled service or usable development checkout was found.",
                id: id
            )
            return
        }

        do {
            try prepareLog()
        } catch {
            failStartup(
                "Onyx could not create its log file.",
                detail: error.localizedDescription,
                id: id
            )
            return
        }

        let process = Process()
        process.executableURL = launch.executable
        process.arguments = launch.arguments
        process.currentDirectoryURL = launch.workingDirectory
        process.environment = launcherEnvironment()
        process.standardOutput = logHandle
        process.standardError = logHandle
        process.terminationHandler = { [weak self, weak process] _ in
            guard let self, let process else { return }
            let status = process.terminationStatus
            DispatchQueue.main.async {
                guard self.startupID == id, !self.showingFailure else { return }
                self.failStartup(
                    "The Onyx service exited before it was ready.",
                    detail: "\(launch.label) exited with status \(status).",
                    id: id
                )
            }
        }

        writeLog("\n=== Launcher \(Date()) — \(launch.label) ===\n")
        updateStatus("Starting \(launch.label)…")
        do {
            try process.run()
            server = process
            didSpawn = true
            waitForServer(id: id, deadline: Date().addingTimeInterval(20))
        } catch {
            failStartup(
                "The Onyx service could not be launched.",
                detail: error.localizedDescription,
                id: id
            )
        }
    }

    private func checkHealth(_ completion: @escaping (HealthResult) -> Void) {
        guard let url = URL(string: "\(baseURL)/health") else {
            completion(.unavailable("Invalid health URL."))
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.0
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        URLSession.shared.dataTask(with: request) { data, response, error in
            guard let http = response as? HTTPURLResponse else {
                completion(.unavailable(error?.localizedDescription ?? "No response."))
                return
            }
            guard http.statusCode == 200 else {
                completion(.incompatible("The process on port \(port) returned HTTP \(http.statusCode)."))
                return
            }
            guard
                let data,
                let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                json["service"] as? String == expectedService,
                json["protocol"] as? Int == expectedProtocol
            else {
                completion(.incompatible("Its health response does not identify a compatible Onyx service."))
                return
            }
            let providerAvailable =
                (json["claude_available"] as? Bool ?? false) ||
                (json["codex_available"] as? Bool ?? false)
            completion(.healthy(providerAvailable: providerAvailable, version: json["version"] as? String))
        }.resume()
    }

    /// Polls until a service answers. With `expecting`, an answer from any other version is the old service still
    /// going down, so it keeps waiting; at the deadline it takes what answers, or starts a service of its own.
    private func waitForServer(id: UUID, deadline: Date, expecting: String? = nil) {
        guard startupID == id else { return }
        checkHealth { [weak self] result in
            guard let self else { return }
            DispatchQueue.main.async {
                guard self.startupID == id, !self.showingFailure else { return }
                let again = {
                    self.updateStatus("Waiting for the local service…")
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.25) {
                        self.waitForServer(id: id, deadline: deadline, expecting: expecting)
                    }
                }
                switch result {
                case .healthy(let providerAvailable, let version):
                    if let expecting, version != expecting, Date() < deadline {
                        again()
                    } else {
                        if let expecting, version != expecting {
                            NSLog("Onyx: the service still reports %@, not %@; opening anyway", version ?? "no version", expecting)
                        }
                        self.openApp(providerAvailable: providerAvailable)
                    }
                case .incompatible(let detail):
                    self.failStartup(
                        "The local service returned an incompatible response.",
                        detail: detail,
                        id: id
                    )
                case .unavailable:
                    if Date() < deadline {
                        again()
                    } else if expecting != nil {
                        // The restart never brought a service back: start one, as if none had been running.
                        self.startServer(id: id)
                    } else {
                        self.failStartup(
                            "The Onyx service did not become ready.",
                            detail: "Startup exceeded 20 seconds.",
                            id: id
                        )
                    }
                }
            }
        }
    }

    /// The version a background service is running when it is not this app's, and this app is the one it is run from.
    ///
    /// `scripts/onyx-daemon.sh` runs the service from /Applications/Onyx.app and keeps it across the app quitting. An
    /// update replaces the app's files, not that process: it carries on running the old code and this app, which
    /// adopts any healthy service on the port, would show its pages. So a service reporting another version than the
    /// app's is restarted, once per launch. A copy of the app run from anywhere else (a build in launcher/build, a
    /// disk image) never touches it: that service was started from a different app.
    private func staleBackgroundServiceVersion(reported: String?) -> String? {
        // Both sides resolved the same way: resolvingSymlinksInPath rewrites /private/tmp to /tmp, so one side alone
        // would miss a bundle that sits under a symlinked folder.
        guard let reported, reported != currentVersion(),
              Bundle.main.bundleURL.resolvingSymlinksInPath().path
                == URL(fileURLWithPath: daemonAppPath).resolvingSymlinksInPath().path else { return nil }
        return reported
    }

    private func restartBackgroundService(from stale: String, providerAvailable: Bool, id: UUID) {
        let current = currentVersion()
        NSLog("Onyx: the background service runs %@ but this app is %@; restarting %@", stale, current, daemonLabel)
        updateStatus("Restarting the background service on \(current)…")
        DispatchQueue.global().async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            process.arguments = ["kickstart", "-k", "gui/\(getuid())/\(daemonLabel)"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            var restarted = false
            do {
                try process.run()
                process.waitUntilExit()
                restarted = process.terminationStatus == 0
            } catch {}
            DispatchQueue.main.async {
                guard let self, self.startupID == id else { return }
                if restarted {
                    self.waitForServer(id: id, deadline: Date().addingTimeInterval(20), expecting: current)
                } else {
                    // No such LaunchAgent: the service belongs to something else, so it stays as it was.
                    NSLog("Onyx: %@ is not loaded; keeping the service that answers", daemonLabel)
                    self.openApp(providerAvailable: providerAvailable)
                }
            }
        }
    }

    private func resolveServerLaunch() -> ServerLaunch? {
        let fileManager = FileManager.default
        let home = fileManager.homeDirectoryForCurrentUser
        let commonArguments = serverArguments(home: home)

        if let resources = Bundle.main.resourceURL {
            let bundled = resources.appendingPathComponent("Server/onyx-server")
            if fileManager.isExecutableFile(atPath: bundled.path) {
                return ServerLaunch(
                    executable: bundled,
                    arguments: commonArguments,
                    workingDirectory: bundled.deletingLastPathComponent(),
                    label: "bundled service"
                )
            }
        }

        // Development fallbacks. ONYX_REPO is useful when the checkout is
        // elsewhere; the bundle-relative candidate supports launcher/build.
        var candidates: [URL] = []
        if let override = ProcessInfo.processInfo.environment["ONYX_REPO"],
           !override.isEmpty {
            candidates.append(URL(fileURLWithPath: (override as NSString).expandingTildeInPath))
        }
        let bundleCheckout = Bundle.main.bundleURL
            .deletingLastPathComponent() // build/
            .deletingLastPathComponent() // launcher/
            .deletingLastPathComponent() // checkout/
        candidates.append(bundleCheckout)
        // Both spellings of the checkout: the project was renamed to Onyx while the
        // folder on disk stayed `ask-widget`, and either may be what a developer has.
        candidates.append(home.appendingPathComponent("Projects/onyx", isDirectory: true))
        candidates.append(home.appendingPathComponent("Projects/ask-widget", isDirectory: true))

        var seen = Set<String>()
        for repo in candidates where seen.insert(repo.standardizedFileURL.path).inserted {
            let python = repo.appendingPathComponent(".venv/bin/python")
            if fileManager.isExecutableFile(atPath: python.path) {
                return ServerLaunch(
                    executable: python,
                    arguments: ["-m", "onyx"] + commonArguments,
                    workingDirectory: repo,
                    label: "development service"
                )
            }
            let runScript = repo.appendingPathComponent("run.sh")
            if fileManager.isExecutableFile(atPath: runScript.path) {
                return ServerLaunch(
                    executable: URL(fileURLWithPath: "/bin/bash"),
                    arguments: [runScript.path] + commonArguments,
                    workingDirectory: repo,
                    label: "development bootstrap"
                )
            }
        }
        return nil
    }

    private func serverArguments(home: URL) -> [String] {
        var arguments = [
            "--folder", home.appendingPathComponent("Projects").path,
            "--port", String(port),
            "--parent-pid", String(ProcessInfo.processInfo.processIdentifier),
        ]
        let rootsFile = home.appendingPathComponent(".config/onyx/allow-roots")
        if let raw = try? String(contentsOf: rootsFile, encoding: .utf8) {
            // A closure, not `\Character.isNewline`: Swift 5.10 (CI's macos-14) cannot
            // pass a key path where a rethrowing closure is expected.
            for line in raw.split(whereSeparator: { $0.isNewline }) {
                let value = line.trimmingCharacters(in: .whitespaces)
                guard !value.isEmpty, !value.hasPrefix("#") else { continue }
                let expanded = (value as NSString).expandingTildeInPath
                arguments += ["--allow-root", expanded]
            }
        }
        return arguments
    }

    private func launcherEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let preferred = [
            "\(home)/.local/bin",
            "\(home)/.claude/local",
            "\(home)/.bun/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        let inherited = environment["PATH"]?.split(separator: ":").map(String.init) ?? []
        environment["PATH"] = Array(NSOrderedSet(array: preferred + inherited))
            .compactMap { $0 as? String }
            .joined(separator: ":")
        environment["PYTHONUNBUFFERED"] = "1"
        return environment
    }

    private func stopOwnedServer() {
        if didSpawn, let server, server.isRunning {
            server.terminate()
        }
        server = nil
        didSpawn = false
    }

    // MARK: - Diagnostics and recovery

    private func prepareLog() throws {
        closeLog()
        let fileManager = FileManager.default
        try fileManager.createDirectory(
            at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        if let size = try? logURL.resourceValues(forKeys: [.fileSizeKey]).fileSize,
           size > 2_000_000 {
            let previous = logURL.deletingLastPathComponent()
                .appendingPathComponent("onyx.previous.log")
            try? fileManager.removeItem(at: previous)
            try fileManager.moveItem(at: logURL, to: previous)
        }
        if !fileManager.fileExists(atPath: logURL.path) {
            guard fileManager.createFile(atPath: logURL.path, contents: nil) else {
                throw NSError(
                    domain: "OnyxLauncher",
                    code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "Could not create \(logURL.path)."]
                )
            }
        }
        logHandle = try FileHandle(forWritingTo: logURL)
        try logHandle?.seekToEnd()
    }

    private func writeLog(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }
        try? logHandle?.write(contentsOf: data)
        try? logHandle?.synchronize()
    }

    private func closeLog() {
        try? logHandle?.close()
        logHandle = nil
    }

    private func logTail(maxLines: Int = 18) -> String {
        guard let data = try? Data(contentsOf: logURL),
              let text = String(data: data.suffix(24_000), encoding: .utf8) else {
            return "No service output was captured."
        }
        return text.split(separator: "\n", omittingEmptySubsequences: false)
            .suffix(maxLines)
            .joined(separator: "\n")
    }

    private func failStartup(_ summary: String, detail: String, id: UUID) {
        guard startupID == id, !showingFailure else { return }
        showingFailure = true
        stopOwnedServer()
        updateStatus("Startup failed")

        let diagnostic = """
        \(detail)

        Log: \(logURL.path)

        Recent output:
        \(logTail())
        """

        while true {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = summary
            alert.informativeText = diagnostic
            alert.addButton(withTitle: "Retry")
            alert.addButton(withTitle: "Open Log")
            alert.addButton(withTitle: "Quit")
            let response = alert.runModal()
            if response == .alertFirstButtonReturn {
                startupID = nil
                showingFailure = false
                beginStartup()
                return
            }
            if response == .alertSecondButtonReturn {
                ensureLogExists()
                NSWorkspace.shared.open(logURL)
                continue
            }
            NSApp.terminate(nil)
            return
        }
    }

    private func ensureLogExists() {
        let directory = logURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
    }

    // MARK: - UI

    private func openApp(providerAvailable: Bool) {
        startupID = nil
        statusLabel = nil

        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.userContentController.addScriptMessageHandler(
            self, contentWorld: .page, name: "askwPick"
        )
        configuration.userContentController.addScriptMessageHandler(
            self, contentWorld: .page, name: "askwAppearance"
        )
        configuration.userContentController.addScriptMessageHandler(
            self, contentWorld: .page, name: "askwClipboard"
        )
        configuration.userContentController.addScriptMessageHandler(
            self, contentWorld: .page, name: "askwGlass"
        )
        configuration.userContentController.addScriptMessageHandler(
            self, contentWorld: .page, name: "askwDrag"
        )
        configuration.userContentController.addScriptMessageHandler(
            self, contentWorld: .page, name: "askwPainted"
        )
        configuration.userContentController.addScriptMessageHandler(
            self, contentWorld: .page, name: "askwChrome"
        )
        // WebKit does not consistently expose the Clipboard API to localhost
        // pages. Give interactive local HTML a browser-compatible writeText()
        // backed by the native pasteboard. The message handler replies with a
        // Promise, matching the standard Clipboard API contract.
        configuration.userContentController.addUserScript(WKUserScript(
            source: """
            (() => {
              const writeText = (text) =>
                window.webkit.messageHandlers.askwClipboard.postMessage({text: String(text)});
              try {
                if (typeof Clipboard !== 'undefined' && Clipboard.prototype) {
                  Object.defineProperty(Clipboard.prototype, 'writeText', {
                    configurable: true,
                    value: writeText,
                    writable: true
                  });
                }
                const nativeClipboard = navigator.clipboard;
                const bridgedClipboard = nativeClipboard
                  ? new Proxy(nativeClipboard, {
                      get(target, property) {
                        if (property === 'writeText') return writeText;
                        const value = target[property];
                        return typeof value === 'function' ? value.bind(target) : value;
                      }
                    })
                  : {writeText};
                Object.defineProperty(navigator, 'clipboard', {
                  configurable: true,
                  value: bridgedClipboard
                });
              } catch (_) {
                window.askWidgetClipboard = {writeText};
              }
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        ))
        configuration.userContentController.addUserScript(WKUserScript(
            source: stockMenuGuard, injectionTime: .atDocumentStart, forMainFrameOnly: false
        ))
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = self
        view.uiDelegate = self
        view.allowsBackForwardNavigationGestures = true
        // The window supplies the desktop blur (WindowGlass) and the page only
        // alpha-aware pane tints, so the WebView must paint nothing of its own.
        // A CSS backdrop-filter cannot see past the WebView, so transparency
        // without the native blur would expose a sharp, unreadable desktop.
        view.underPageBackgroundColor = .clear
        view.setValue(false, forKey: "drawsBackground")
        view.load(URLRequest(url: URL(string: "\(baseURL)/")!))
        webView = view

        window.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.title = "Onyx"
        // Opaque until the page has painted and arms the glass itself.
        glass.window = window
        glass.paintOpaque(launchBaseColor())
        let container = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            view.topAnchor.constraint(equalTo: container.topAnchor),
            view.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        window.contentView = container
        window.setContentSize(NSSize(width: 1200, height: 860))
        window.minSize = NSSize(width: 720, height: 520)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refreshDockRecents()
        dockRefreshTimer?.invalidate()
        dockRefreshTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.refreshDockRecents()
        }

        if !providerAvailable {
            writeLog("WARNING: neither claude nor codex was found by the local service.\n")
        }
        if let url = pendingDocumentURL {
            pendingDocumentURL = nil
            openDocumentURL(url)
        } else if let text = pendingQuickText {
            pendingQuickText = nil
            openQuickSelection(text)
        }
    }

    private func buildLoadingWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 210),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.titlebarAppearsTransparent = true
        window.title = "Onyx"
        window.isMovableByWindowBackground = true
        window.center()
        // A plain opaque splash: there is no page yet to tint the glass, and a
        // blurred window with nothing painted on it reads as a rendering bug.
        glass.window = window
        glass.paintOpaque(launchBaseColor())

        let root = NSView()
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.startAnimation(nil)
        spinner.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Onyx")
        title.font = .boldSystemFont(ofSize: 16)
        title.translatesAutoresizingMaskIntoConstraints = false

        let status = NSTextField(labelWithString: "Starting…")
        status.font = .systemFont(ofSize: 13)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        statusLabel = status

        [spinner, title, status].forEach { root.addSubview($0) }
        window.contentView = root
        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: root.centerYAnchor, constant: -20),
            title.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            title.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 14),
            status.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            status.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 6),
            status.leadingAnchor.constraint(greaterThanOrEqualTo: root.leadingAnchor, constant: 24),
            status.trailingAnchor.constraint(lessThanOrEqualTo: root.trailingAnchor, constant: -24),
        ])
        window.makeKeyAndOrderFront(nil)
    }

    /// The window colour before any page has spoken: the ground the shell last gave the glass (the vault's own
    /// while the app wears the vault look), else the theme's `--bg-primary`.
    private func launchBaseColor() -> NSColor {
        if let saved = UserDefaults.standard.array(forKey: launchBaseDefaultsKey) as? [Double], saved.count == 3 {
            return srgb(saved.map { min(255, max(0, $0)) })
        }
        let dark = (window?.effectiveAppearance ?? NSApp.effectiveAppearance)
            .bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        return srgb(dark ? baseRGB.dark : baseRGB.light)
    }

    private func updateStatus(_ text: String) {
        DispatchQueue.main.async { [weak self] in self?.statusLabel?.stringValue = text }
    }

    // Native picker bridge → chosen absolute path, or null on cancellation.
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        let body = message.body as? [String: Any] ?? [:]
        if message.name == "askwClipboard" {
            guard let text = body["text"] as? String else {
                replyHandler(nil, "Clipboard text is missing.")
                return
            }
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            if pasteboard.setString(String(text.prefix(1_000_000)), forType: .string) {
                replyHandler(true, nil)
            } else {
                replyHandler(nil, "Could not write to the clipboard.")
            }
            return
        }
        if message.name == "askwPainted" {
            // The shell, a frame after a reader page has loaded: the page a swipe went to is on screen now.
            if message.frameInfo.isMainFrame { liftSwipeCover() }
            replyHandler(true, nil)
            return
        }
        if message.name == "askwChrome" {
            // The shell went in or out of Page Only (⌘B, vault_ui.py). A page in the reader frame has no say.
            if message.frameInfo.isMainFrame { showPageOnly(body["pageOnly"] as? Bool == true) }
            replyHandler(true, nil)
            return
        }
        if message.name == "askwAppearance" {
            // Only the shell page sets the window's appearance: a document in the reader frame, asking for the
            // app theme, would otherwise undo the vault look's mode.
            guard message.frameInfo.isMainFrame else {
                replyHandler(true, nil)
                return
            }
            let theme = body["theme"] as? String
            let chosen = appearance(forTheme: theme)
            // Native menus (including HTML <select> popups) resolve their
            // colors from NSApp, while the title bar resolves from the window.
            // Keep both halves on the same explicit appearance.
            NSApp.appearance = chosen
            window.appearance = chosen
            UserDefaults.standard.set(
                ["dark", "light"].contains(theme ?? "") ? theme : "system",
                forKey: appearanceDefaultsKey
            )
            replyHandler(true, nil)
            return
        }
        if message.name == "askwDrag" {
            // A WebView swallows the mouse, so the only thing AppKit will drag
            // the window by is the thin band of title bar over the page. The
            // shell hands the mouse-down straight back and the window drags by
            // its own chrome instead — what `data-tauri-drag-region` does for
            // cxtasks and cxmail, and the way their Tauri does it (tao's
            // drag_window): drag by NSApp's current event, whatever the page
            // took to report it. On a Force Touch trackpad that is a pressure
            // event, which keeps arriving while the finger is down and cannot
            // start a drag, so it stands in a mouse-down at the pointer. A check
            // for a mouse-down here refused every drag on the trackpad.
            guard message.frameInfo.isMainFrame, var event = NSApp.currentEvent else {
                replyHandler(false, nil)
                return
            }
            // A double-click in the title band, which the page covers too, sent
            // on its mouse-up as Tauri does on macOS: it does what a title bar's
            // does, as the user set it in Desktop & Dock.
            if body["double"] as? Bool == true {
                replyHandler(true, nil)
                titleBarDoubleClick()
                return
            }
            if event.type == .pressure, let down = NSEvent.mouseEvent(
                with: .leftMouseDown,
                location: window.convertPoint(fromScreen: NSEvent.mouseLocation),
                modifierFlags: event.modifierFlags, timestamp: event.timestamp,
                windowNumber: window.windowNumber, context: nil, eventNumber: 0,
                clickCount: 1, pressure: 1
            ) {
                event = down
            }
            // Reply first: performDrag(with:) runs its own event loop and does
            // not return until the mouse comes up.
            replyHandler(true, nil)
            window.performDrag(with: event)
            return
        }
        if message.name == "askwGlass" {
            let state: [String: Any] = [
                "reduceTransparency": glass.reduceTransparency,
                "available": glass.isAvailable,
            ]
            // Only the shell page drives the window; a document in the reader
            // iframe has no say over it.
            guard body["query"] as? Bool != true, message.frameInfo.isMainFrame else {
                replyHandler(state, nil)
                return
            }
            let radius = (body["radius"] as? NSNumber)?.intValue ?? 24
            if body["radiusOnly"] as? Bool == true {
                glass.setRadius(radius)
            } else {
                let rgb = (body["rgb"] as? [NSNumber])?.map { $0.doubleValue }
                let base = rgb?.count == 3 ? srgb(rgb!.map { min(255, max(0, $0)) }) : launchBaseColor()
                if let rgb, rgb.count == 3 { UserDefaults.standard.set(rgb, forKey: launchBaseDefaultsKey) }
                glass.setState(enabled: body["enabled"] as? Bool ?? false, radius: radius, base: base)
            }
            replyHandler(state, nil)
            return
        }
        let kind = body["kind"] as? String ?? "folder"
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        switch kind {
        case "file":
            panel.canChooseFiles = true
            panel.canChooseDirectories = false
            panel.allowedContentTypes = supportedDocumentTypes()
            panel.prompt = "Open"
            panel.message = "Choose an HTML, Markdown, text, or PDF document"
        case "html":
            panel.canChooseFiles = true
            panel.canChooseDirectories = false
            panel.allowsMultipleSelection = true
            panel.allowedContentTypes = htmlDocumentTypes()
            panel.prompt = "Link"
            panel.message = "Choose HTML pages to link into Artifacts"
        default:
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.canCreateDirectories = false
            panel.prompt = "Use Folder"
            panel.message = "Choose the context folder the selected provider should read"
        }
        if let prompt = body["prompt"] as? String, !prompt.isEmpty { panel.prompt = prompt }
        if let text = body["message"] as? String, !text.isEmpty { panel.message = text }
        if let initial = body["initial"] as? String, !initial.isEmpty,
           FileManager.default.fileExists(atPath: initial) {
            panel.directoryURL = URL(fileURLWithPath: initial)
        }
        panel.begin { response in
            guard response == .OK else {
                replyHandler(nil, nil)
                return
            }
            if kind == "html" {
                replyHandler(panel.urls.map { $0.path }, nil)
            } else {
                replyHandler(panel.url?.path, nil)
            }
        }
    }

    /// A link that leaves Onyx opens in the user's browser, and never in the app. This is the app's whole answer for
    /// a click: the shell's own handler (`extClick`, tabs_ui.py) steps aside here, because a navigation is cancelled
    /// whatever the page is — a note, a PDF, an artifact's frame, a page with no script of ours in it — while a window
    /// has to be allowed to open first. Only a link the reader actually clicked is taken: an embedded frame (a video,
    /// a map) navigates as `.other` and still loads in place, and a Back or Forward never re-opens the browser.
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard navigationAction.navigationType == .linkActivated,
              let url = navigationAction.request.url, leavesOnyx(url) else {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        NSWorkspace.shared.open(url)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        // A page of Onyx's own asking for a window (a ⌘-click or middle click the shell didn't catch) opens as a tab,
        // never over the shell. A window asked for somewhere else is the shell handing over a link that leaves Onyx, or
        // a page asking to: it goes to the browser. Anything else still loads here.
        if let base = URL(string: baseURL), url.host == base.host, url.port == base.port,
           ["/view", "/quick"].contains(url.path) {
            let href = url.path + (url.query.map { "?\($0)" } ?? "") + (url.fragment.map { "#\($0)" } ?? "")
            shellCall("onyxShell.openHref(\(jsString(href)))", fallback: href)
        } else if leavesOnyx(url) {
            NSWorkspace.shared.open(url)
        } else {
            webView.load(URLRequest(url: url))
        }
        return nil
    }

    /// A new page in the window starts with its chrome: Page Only is the shell's, and not remembered, so a shell that
    /// loads again (Reload, a view switch that loads) comes back with none of it, and the traffic lights must too.
    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        showPageOnly(false)
    }

    /// Page Only (⌘B): the page is the whole window, so the traffic lights go too and its top-left corner takes clicks.
    /// The shell hides everything else; the item's check mark follows whichever way it was toggled.
    private func showPageOnly(_ on: Bool) {
        for kind in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            window?.standardWindowButton(kind)?.isHidden = on
        }
        pageOnlyItem?.state = on ? .on : .off
    }

    /// WebKit's private navigation-delegate call, made as a swipe's slide ends and before it navigates; `item` is nil
    /// when the swipe was abandoned. What is on screen then is WebKit's snapshot of the destination, fully in.
    @objc(_webViewDidEndNavigationGesture:withNavigationToBackForwardListItem:)
    func webViewDidEndNavigationGesture(_ webView: WKWebView, navigatingTo item: WKBackForwardListItem?) {
        guard item != nil else { return }
        holdSwipeCover(over: webView)
    }

    private func holdSwipeCover(over webView: WKWebView) {
        guard swipeCover == nil, let window, let container = webView.superview,
              let screen = NSScreen.screens.first else { return }
        let rect = window.convertToScreen(webView.convert(webView.bounds, to: nil))
        // CoreGraphics counts from the top of the menu-bar screen; AppKit from its bottom.
        let captureRect = CGRect(x: rect.minX, y: screen.frame.maxY - rect.maxY, width: rect.width, height: rect.height)
        guard let image = CGWindowListCreateImage(
            captureRect, .optionIncludingWindow, CGWindowID(window.windowNumber),
            [.boundsIgnoreFraming, .bestResolution]
        ) else { return }
        let cover = SwipeCover(image: image, frame: webView.frame)
        container.addSubview(cover, positioned: .above, relativeTo: webView)
        // With glass on, the panes are translucent: the page left would show through the picture's.
        webView.alphaValue = 0
        swipeCover = cover
        // A back or forward that loads nothing (an anchor in the same page) sends no word; don't hold the page still.
        let deadline = DispatchWorkItem { [weak self] in self?.liftSwipeCover() }
        swipeCoverDeadline = deadline
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: deadline)
    }

    private func liftSwipeCover() {
        swipeCoverDeadline?.cancel()
        swipeCoverDeadline = nil
        guard let cover = swipeCover else { return }
        swipeCover = nil
        webView?.alphaValue = 1
        cover.removeFromSuperview()
    }

    // "Double-click a window's title bar to" Zoom, Minimize or Do Nothing.
    // Unset is Zoom; older systems kept Minimize as a Bool of its own. Fill
    // (macOS 15) has no public call, and zoom to the standard frame is the
    // screen's visible frame, which is what Fill gives a window like this one.
    private func titleBarDoubleClick() {
        let defaults = UserDefaults.standard
        switch defaults.string(forKey: "AppleActionOnDoubleClick") {
        case "None":
            return
        case "Minimize":
            window.performMiniaturize(nil)
        case nil where defaults.bool(forKey: "AppleMiniaturizeOnDoubleClick"):
            window.performMiniaturize(nil)
        default:
            window.performZoom(nil)
        }
    }

    // MARK: - Menu

    @objc private func reload() { webView?.reload() }
    @objc private func zoomIn() { stepZoom(direction: 1) }
    @objc private func zoomOut() { stepZoom(direction: -1) }
    @objc private func resetZoom() { webView?.pageZoom = 1.0 }

    private func stepZoom(direction: Int) {
        guard let webView else { return }
        let current = webView.pageZoom
        if direction > 0 {
            webView.pageZoom = zoomLevels.first(where: { $0 > current + 0.001 })
                ?? zoomLevels.last!
        } else {
            webView.pageZoom = zoomLevels.last(where: { $0 < current - 0.001 })
                ?? zoomLevels.first!
        }
    }

    private func installKeyboardShortcuts() {
        keyDownMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) {
            [weak self] event in
            guard let self else { return event }
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            guard flags.contains(.command),
                  !flags.contains(.control),
                  !flags.contains(.option) else { return event }
            switch event.charactersIgnoringModifiers {
            case "=", "+":
                self.zoomIn()
            case "-", "_":
                self.zoomOut()
            case "0":
                self.resetZoom()
            case "w":
                // ⌘W hides Onyx, like ⌘H. ⇧⌘W arrives as "W" and still closes the window.
                NSApp.hide(nil)
            default:
                return event
            }
            return nil
        }
    }

    @objc private func goLibrary() {
        switchVault("library", path: "/")
    }
    @objc private func goVault() {
        switchVault("notes", path: "/vault")
    }
    @objc private func goHTMLVault() {
        switchVault("html", path: "/vault?vault=html")
    }
    @objc private func openSettings() {
        shellCall("onyxShell.openSettings()", fallback: "/#settings")
    }
    @objc private func openRecentConversations() {
        shellCall("onyxShell.openHistory()", fallback: "/#history")
    }
    @objc private func openSearch() {
        shellCall("onyxShell.openSearch()", fallback: "/#search")
    }
    // The tabs (tabs_ui.py). New Tab loads the shell when it isn't showing; the others act only on the shell.
    @objc private func newTab() { shellCall("onyxShell.newTab()", fallback: "/") }
    @objc private func nextTab() { onShell("onyxShell.nextTab()") }
    @objc private func previousTab() { onShell("onyxShell.prevTab()") }
    /// File ▸ Close Tab (no shortcut: ⌘W hides Onyx, like ⌘H). The shell answers false for its lone home tab, and
    /// off the shell there are no tabs: then it closes the window. A panel or alert that is key closes itself instead.
    @objc private func closeTab() {
        guard let webView, NSApp.keyWindow == nil || NSApp.keyWindow === window,
              ["/", "/vault"].contains(webView.url?.path ?? "") else {
            (NSApp.keyWindow ?? window)?.performClose(nil)
            return
        }
        webView.evaluateJavaScript("!!(window.onyxShell && onyxShell.closeTab && onyxShell.closeTab())") {
            [weak self] result, _ in
            if (result as? Bool) != true { self?.window?.performClose(nil) }
        }
    }
    /// View ▸ Toggle Editing (⌘E): a Markdown page in the reader turns into its editor and back, as Obsidian's ⌘E does.
    /// ⌘E typed in the page or the sidebar is taken there first (ask.js, vault_ui.py); this serves a click.
    @objc private func toggleEditing() { onShell("onyxShell.edit()") }
    /// View ▸ Page Only (⌘B): the page alone in the window, and back. ⌘B typed in the page or the shell is taken there
    /// first (vault_ui.py), and in the editor it stays bold; this serves a click.
    @objc private func togglePageOnly() { onShell("onyxShell.togglePageOnly()") }
    @objc private func openFind() { findInPage("open") }
    @objc private func findNext() { findInPage("next") }
    @objc private func findPrevious() { findInPage("previous") }
    /// Edit ▸ Find: the shell's find bar, over the page in its reader. Off the shell there is no reader to search, so
    /// unlike `shellCall` this never loads anything in its place. ⌘F and ⌘G typed in the page reach the bar first
    /// (the page takes the key); these items serve a click, and any key the page leaves alone.
    private func findInPage(_ verb: String) {
        onShell("onyxShell.find('\(verb)')")
    }
    /// Runs one of the shell's entry points when the shell is showing, and nothing otherwise.
    private func onShell(_ call: String) {
        guard let webView, ["/", "/vault"].contains(webView.url?.path ?? "") else { return }
        let entry = call.prefix { $0 != "(" }
        webView.evaluateJavaScript("!!(window.onyxShell && \(entry) && \(call))", completionHandler: nil)
    }
    /// A string as a JavaScript literal, for handing a path or a URL to the shell.
    private func jsString(_ text: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: text, options: [.fragmentsAllowed, .withoutEscapingSlashes])
        return data.flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
    }
    /// On the shell (Library, Notes, Artifacts) a view comes in place, so the sidebar never reloads
    /// (a load blanks the glass window for a frame); from any other page, or if the shell can't, it loads.
    private func switchVault(_ kind: String, path: String) {
        shellCall("onyxVault.switchTo('\(kind)')", fallback: path)
    }
    /// Runs one of the shell's entry points (`window.onyxVault`, `window.onyxShell`) in place, or loads
    /// `fallback` when the page isn't the shell or the call didn't take.
    private func shellCall(_ call: String, fallback: String) {
        guard let webView else { return }
        let load = { _ = webView.load(URLRequest(url: URL(string: "\(baseURL)\(fallback)")!)) }
        guard ["/", "/vault"].contains(webView.url?.path ?? "") else { load(); return }
        let entry = call.prefix { $0 != "." }
        webView.evaluateJavaScript("!!(window.\(entry) && \(call))") { result, _ in
            if (result as? Bool) != true { load() }
        }
    }
    @objc private func openInBrowser() {
        NSWorkspace.shared.open(webView?.url ?? URL(string: "\(baseURL)/")!)
    }
    @objc private func openLogs() {
        ensureLogExists()
        NSWorkspace.shared.open(logURL)
    }
    private func currentVersion() -> String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }

    @objc private func openDocument() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = supportedDocumentTypes()
        panel.allowsMultipleSelection = false
        panel.begin { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            self?.openDocumentURL(url)
        }
    }

    private func htmlDocumentTypes() -> [UTType] {
        var types: [UTType] = [.html]
        if let htm = UTType(filenameExtension: "htm"), !types.contains(htm) { types.append(htm) }
        return types
    }

    private func supportedDocumentTypes() -> [UTType] {
        var types: [UTType] = [.html, .pdf, .plainText]
        for ext in ["htm", "md", "markdown"] {
            if let type = UTType(filenameExtension: ext), !types.contains(type) {
                types.append(type)
            }
        }
        return types
    }

    private func isSupportedDocument(_ url: URL) -> Bool {
        ["html", "htm", "md", "markdown", "txt", "pdf"]
            .contains(url.pathExtension.lowercased())
    }

    /// A document from Finder, File ▸ Open or Alfred comes forward in the tab already reading it, or opens in a new
    /// one; a page that lives in a vault opens as its row there (the service maps the real file back to it). Off the
    /// shell, or before it has loaded, the shell loads with the page in Library, as it always did.
    private func openDocumentURL(_ documentURL: URL) {
        var components = URLComponents()
        components.queryItems = [URLQueryItem(name: "src", value: documentURL.path)]
        guard let query = components.percentEncodedQuery else { return }
        shellCall("onyxShell.openInTab(\(jsString(documentURL.path)))", fallback: "/?\(query)")
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func openQuickSelection(_ text: String) {
        var components = URLComponents(string: "\(baseURL)/quick")!
        components.queryItems = [URLQueryItem(name: "text", value: text)]
        if let url = components.url {
            webView?.load(URLRequest(url: url))
            window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func menuItem(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(NSMenuItem(
            title: "About Onyx",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        ))
        // Sparkle's own item: it is the controller's target, so the controller also greys it out mid-check.
        let updatesItem = NSMenuItem(
            title: "Check for Updates…",
            action: #selector(SPUStandardUpdaterController.checkForUpdates(_:)),
            keyEquivalent: ""
        )
        updatesItem.target = updaterController
        appMenu.addItem(updatesItem)
        appMenu.addItem(.separator())
        appMenu.addItem(menuItem("Settings…", #selector(openSettings), ","))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(
            title: "Hide Onyx", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"
        ))
        appMenu.addItem(NSMenuItem(
            title: "Quit Onyx", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"
        ))

        let fileItem = NSMenuItem()
        main.addItem(fileItem)
        let fileMenu = NSMenu(title: "File")
        fileItem.submenu = fileMenu
        fileMenu.addItem(menuItem("New Tab", #selector(newTab), "t"))
        fileMenu.addItem(menuItem("Open Document…", #selector(openDocument), "o"))
        fileMenu.addItem(menuItem("Search…", #selector(openSearch), "p"))
        fileMenu.addItem(.separator())
        fileMenu.addItem(menuItem("Library", #selector(goLibrary), "n"))
        fileMenu.addItem(menuItem("Vault", #selector(goVault), "V"))
        fileMenu.addItem(menuItem("Artifacts", #selector(goHTMLVault), "H"))
        fileMenu.addItem(.separator())
        fileMenu.addItem(menuItem("Recent Conversations", #selector(openRecentConversations), "y"))
        fileMenu.addItem(.separator())
        fileMenu.addItem(menuItem("Close Tab", #selector(closeTab), ""))
        fileMenu.addItem(NSMenuItem(
            title: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "W"
        ))

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editItem.submenu = editMenu
        editMenu.addItem(NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z"))
        editMenu.addItem(NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "Z"))
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editMenu.addItem(.separator())
        // Find, where every Mac app files it: the reader's find bar (find_ui.py).
        let findItem = NSMenuItem(title: "Find", action: nil, keyEquivalent: "")
        let findMenu = NSMenu(title: "Find")
        findItem.submenu = findMenu
        findMenu.addItem(menuItem("Find…", #selector(openFind), "f"))
        findMenu.addItem(menuItem("Find Next", #selector(findNext), "g"))
        findMenu.addItem(menuItem("Find Previous", #selector(findPrevious), "G"))
        editMenu.addItem(findItem)

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewItem.submenu = viewMenu
        viewMenu.addItem(menuItem("Reload", #selector(reload), "r"))
        viewMenu.addItem(menuItem("Toggle Editing", #selector(toggleEditing), "e"))
        let pageOnly = menuItem("Page Only", #selector(togglePageOnly), "b")
        pageOnlyItem = pageOnly
        viewMenu.addItem(pageOnly)
        viewMenu.addItem(.separator())
        viewMenu.addItem(menuItem("Zoom In", #selector(zoomIn), "+"))
        viewMenu.addItem(menuItem("Zoom Out", #selector(zoomOut), "-"))
        viewMenu.addItem(menuItem("Actual Size", #selector(resetZoom), "0"))
        viewMenu.addItem(.separator())
        viewMenu.addItem(menuItem("Open in Default Browser", #selector(openInBrowser), "B"))
        viewMenu.addItem(.separator())
        viewMenu.addItem(menuItem("Open Service Log", #selector(openLogs), "L"))

        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: "Window")
        windowItem.submenu = windowMenu
        windowMenu.addItem(NSMenuItem(
            title: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m"
        ))
        windowMenu.addItem(.separator())
        // ⌃⇥ and ⌃⇧⇥, as Safari files them; ⌘⇧] and ⌘⇧[ are the page's own (tabs_ui.py).
        let previousTabItem = menuItem("Show Previous Tab", #selector(previousTab), "\u{19}")
        previousTabItem.keyEquivalentModifierMask = [.control, .shift]
        windowMenu.addItem(previousTabItem)
        let nextTabItem = menuItem("Show Next Tab", #selector(nextTab), "\t")
        nextTabItem.keyEquivalentModifierMask = [.control]
        windowMenu.addItem(nextTabItem)

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
