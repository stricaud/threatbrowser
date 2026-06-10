import SwiftUI
import WebKit

// Renders markdown as HTML in a WKWebView, injecting CSS to match the app's dark theme.
struct MarkdownWebView: UIViewRepresentable {
    let markdown: String
    let rule: ContentRule?
    var baseURL: URL?

    func makeUIView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.defaultWebpagePreferences.allowsContentJavaScript = false
        let wv = WKWebView(frame: .zero, configuration: cfg)
        wv.scrollView.showsHorizontalScrollIndicator = false
        wv.isOpaque = false
        wv.backgroundColor = .clear
        wv.scrollView.backgroundColor = .clear
        wv.navigationDelegate = context.coordinator
        return wv
    }

    func updateUIView(_ wv: WKWebView, context: Context) {
        let html = buildHTML()
        wv.loadHTMLString(html, baseURL: baseURL)
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    class Coordinator: NSObject, WKNavigationDelegate {
        func webView(_ wv: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let url = action.request.url {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.allow)
            }
        }
    }

    // MARK: - HTML generation

    private func buildHTML() -> String {
        var md = markdown
        // Apply content filter rule
        if let rs = rule?.rule_start, !rs.isEmpty, let r = md.range(of: rs) { md = String(md[r.lowerBound...]) }
        if let re = rule?.rule_end,   !re.isEmpty, let r = md.range(of: re) { md = String(md[..<r.lowerBound]) }
        let body = convertMarkdown(md)
        return """
        <!DOCTYPE html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <style>
        :root { color-scheme: dark; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            font-size: 15px; line-height: 1.65;
            color: #c9d1d9; background: transparent;
            margin: 0; padding: 16px;
            word-wrap: break-word; overflow-wrap: break-word;
        }
        h1 { font-size: 20px; } h2 { font-size: 17px; } h3 { font-size: 15px; }
        h1,h2,h3,h4 { color: #e6edf3; margin: 18px 0 8px; line-height: 1.3; }
        a { color: #58a6ff; text-decoration: none; }
        a:active { opacity: 0.7; }
        p { margin: 8px 0; }
        code {
            background: #21262d; border-radius: 4px;
            padding: 1px 5px; font-size: 13px;
            font-family: 'SF Mono', 'Fira Code', monospace;
        }
        pre {
            background: #161b22; border: 1px solid #30363d;
            border-radius: 6px; padding: 12px; overflow-x: auto;
            margin: 12px 0;
        }
        pre code { background: none; padding: 0; font-size: 12px; }
        blockquote {
            border-left: 3px solid #30363d; margin: 8px 0;
            padding-left: 12px; color: #8b949e;
        }
        img { max-width: 100%; border-radius: 6px; margin: 8px 0; display: block; }
        ul,ol { margin: 8px 0 8px 20px; }
        li { margin: 3px 0; }
        table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
        th,td { border: 1px solid #30363d; padding: 6px 10px; text-align: left; }
        th { background: #21262d; color: #e6edf3; }
        hr { border: none; border-top: 1px solid #30363d; margin: 16px 0; }
        strong { color: #e6edf3; }
        </style></head><body>\(body)</body></html>
        """
    }

    // MARK: - Minimal markdown → HTML

    private func convertMarkdown(_ md: String) -> String {
        let lines = md.components(separatedBy: "\n")
        var out = ""
        var inCode = false
        var codeLang = ""
        var codeBlock = ""
        var inList = false

        func flushList() {
            if inList { out += "</ul>\n"; inList = false }
        }

        for line in lines {
            // Fenced code block
            if line.hasPrefix("```") {
                if inCode {
                    out += "<pre><code class=\"\(codeLang)\">\(codeBlock.htmlEscaped)</code></pre>\n"
                    codeBlock = ""; inCode = false; codeLang = ""
                } else {
                    flushList()
                    codeLang = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                    inCode = true
                }
                continue
            }
            if inCode { codeBlock += line + "\n"; continue }

            // Headings
            if line.hasPrefix("# ")      { flushList(); out += "<h1>\(inline(String(line.dropFirst(2))))</h1>\n"; continue }
            if line.hasPrefix("## ")     { flushList(); out += "<h2>\(inline(String(line.dropFirst(3))))</h2>\n"; continue }
            if line.hasPrefix("### ")    { flushList(); out += "<h3>\(inline(String(line.dropFirst(4))))</h3>\n"; continue }
            if line.hasPrefix("#### ")   { flushList(); out += "<h4>\(inline(String(line.dropFirst(5))))</h4>\n"; continue }

            // Horizontal rule
            if line.hasPrefix("---") || line.hasPrefix("***") {
                flushList(); out += "<hr>\n"; continue
            }

            // Blockquote
            if line.hasPrefix("> ") {
                flushList()
                out += "<blockquote>\(inline(String(line.dropFirst(2))))</blockquote>\n"; continue
            }

            // List items
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ") {
                if !inList { out += "<ul>\n"; inList = true }
                out += "<li>\(inline(String(trimmed.dropFirst(2))))</li>\n"; continue
            }
            if trimmed.first?.isNumber == true, let m = trimmed.range(of: #"^\d+\. "#, options: .regularExpression) {
                if !inList { out += "<ul>\n"; inList = true }
                out += "<li>\(inline(String(trimmed[m.upperBound...])))</li>\n"; continue
            }

            flushList()

            // Blank line → paragraph break
            if trimmed.isEmpty { out += "\n"; continue }

            // Image (before link check)
            if trimmed.hasPrefix("![") {
                out += "<p>\(inline(trimmed))</p>\n"; continue
            }

            out += "<p>\(inline(trimmed))</p>\n"
        }
        flushList()
        if inCode { out += "<pre><code>\(codeBlock.htmlEscaped)</code></pre>\n" }
        return out
    }

    private func inline(_ s: String) -> String {
        var r = s.htmlEscaped
        // Images: ![alt](url)
        r = r.replacingOccurrences(of: #"!\[([^\]]*)\]\(([^)]+)\)"#,
            with: #"<img src="$2" alt="$1" loading="lazy">"#, options: .regularExpression)
        // Links: [text](url)
        r = r.replacingOccurrences(of: #"\[([^\]]+)\]\(([^)]+)\)"#,
            with: #"<a href="$2">$1</a>"#, options: .regularExpression)
        // Bold + italic: ***text***
        r = r.replacingOccurrences(of: #"\*\*\*([^*]+)\*\*\*"#,
            with: "<strong><em>$1</em></strong>", options: .regularExpression)
        // Bold: **text**
        r = r.replacingOccurrences(of: #"\*\*([^*]+)\*\*"#,
            with: "<strong>$1</strong>", options: .regularExpression)
        // Italic: *text* or _text_
        r = r.replacingOccurrences(of: #"\*([^*\n]+)\*"#, with: "<em>$1</em>", options: .regularExpression)
        // Inline code: `code`
        r = r.replacingOccurrences(of: #"`([^`\n]+)`"#,
            with: "<code>$1</code>", options: .regularExpression)
        return r
    }
}

private extension String {
    var htmlEscaped: String {
        self.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }
}
