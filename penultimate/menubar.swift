// Penultimate in the menu bar: the live account value sits at the top of the screen; click it and the whole dashboard drops down.
// Built on the Mac itself by penultimate/menubar.py. It only talks to the bot on this computer.
import Cocoa
import WebKit

let args = CommandLine.arguments
let PORT = args.count > 1 ? args[1] : "8787"
let DATA = args.count > 2 ? args[2] : ""
let BASE = "http://127.0.0.1:\(PORT)/"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var item: NSStatusItem!
    let popover = NSPopover()
    var web: WKWebView!
    var timer: Timer?
    var pageOK = false
    var online = false

    func applicationDidFinishLaunching(_ note: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            b.title = "P …"
            b.target = self
            b.action = #selector(clicked(_:))
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
            b.toolTip = "Penultimate: click to open, right-click for more"
        }
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 470, height: 720), configuration: WKWebViewConfiguration())
        web.navigationDelegate = self
        let vc = NSViewController()
        vc.view = web
        popover.contentViewController = vc
        popover.contentSize = NSSize(width: 470, height: 720)
        popover.behavior = .transient
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refresh() }
    }

    @objc func clicked(_ sender: Any?) {
        if let e = NSApp.currentEvent, e.type == .rightMouseUp || e.modifierFlags.contains(.control) { showMenu(); return }
        toggle()
    }

    func toggle() {
        guard let b = item.button else { return }
        if popover.isShown { popover.performClose(nil); return }
        if !pageOK { loadPage() }
        popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
        NSApp.activate(ignoringOtherApps: true)
    }

    func loadPage() {
        if let u = URL(string: BASE) { web.load(URLRequest(url: u, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8)) }
    }

    func showMenu() {
        let m = NSMenu()
        m.addItem(withTitle: "Open the full dashboard in the browser", action: #selector(openBrowser), keyEquivalent: "").target = self
        m.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "").target = self
        m.addItem(NSMenuItem.separator())
        m.addItem(withTitle: "Hide from the menu bar until the bot restarts", action: #selector(hide), keyEquivalent: "").target = self
        item.menu = m
        item.button?.performClick(nil)
        item.menu = nil
    }

    @objc func openBrowser() { if let u = URL(string: BASE) { NSWorkspace.shared.open(u) } }
    @objc func reload() { pageOK = false; loadPage() }
    @objc func hide() {
        if !DATA.isEmpty { FileManager.default.createFile(atPath: DATA + "/menubar.off", contents: Data(), attributes: nil) }
        NSApp.terminate(nil)
    }

    // the page inside the drop-down
    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) { pageOK = (w.url?.absoluteString.hasPrefix(BASE) ?? false) }
    func webView(_ w: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { waiting() }
    func webView(_ w: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { waiting() }
    func waiting() {
        pageOK = false
        web.loadHTMLString("<body style='background:#04060a;color:#6f7f93;font:14px -apple-system;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>Penultimate is starting. This opens by itself in a few seconds.</body>", baseURL: nil)
    }

    // the number in the menu bar: one read of the bot's own state every few seconds
    func refresh() {
        guard let u = URL(string: BASE + "api/state") else { return }
        var req = URLRequest(url: u)
        req.timeoutInterval = 2.5
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            var title = "P off"
            var color = NSColor.secondaryLabelColor
            var ok = false
            if let d = data, let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
                ok = true
                if (j["ready"] as? Bool) == true, let eq = j["equity"] as? Double {
                    let risk = j["risk"] as? [String: Any] ?? [:]
                    let halted = (risk["halted"] as? Bool) ?? false
                    let paused = (risk["paused"] as? Bool) ?? false
                    let live = (j["mode"] as? String) == "live"
                    let day = (j["day_pnl"] as? Double) ?? 0
                    let f = NumberFormatter()
                    f.numberStyle = .currency
                    f.currencyCode = "USD"
                    f.maximumFractionDigits = 2
                    let money = f.string(from: NSNumber(value: eq)) ?? String(format: "$%.2f", eq)
                    let sign = day > 0.004 ? "+" : day < -0.004 ? "−" : "±"
                    title = (halted ? "■ " : paused ? "❙❙ " : live ? "● " : "○ ") + money + "  " + sign + String(format: "%.2f", abs(day))
                    color = halted ? NSColor.systemRed : paused ? NSColor.systemOrange : day > 0.004 ? NSColor.systemGreen : day < -0.004 ? NSColor.systemRed : NSColor.labelColor
                } else {
                    title = "P starting"
                }
            }
            DispatchQueue.main.async {
                guard let s = self, let b = s.item.button else { return }
                b.attributedTitle = NSAttributedString(string: title, attributes: [
                    .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium), .foregroundColor: color])
                if ok && !s.online && !s.pageOK && s.popover.isShown { s.loadPage() }     // the bot came back while the drop-down was open
                s.online = ok
            }
        }.resume()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
