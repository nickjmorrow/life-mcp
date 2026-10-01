// A tiny HTTP/1.1 server on 127.0.0.1 only (Network framework). One request per connection.
// Every request must carry the secret shared with the connector (see SharedToken).
import Darwin
import Foundation
import Network

/// The secret the Life connector puts in X-Life-Home-Token. The connector makes the file
/// (~/.config/life-mcp/home-token, mode 600); the app may read exactly that file through the
/// read-only sandbox exception in LifeHome.entitlements. It's read on every request, so the
/// connector can make or replace it at any time.
enum SharedToken {
    static let header = "x-life-home-token"
    static let relativePath = ".config/life-mcp/home-token"  // must match the entitlement

    /// The real home folder: in the sandbox, NSHomeDirectory() is the app's container.
    static var path: String {
        let home = getpwuid(getuid()).flatMap { String(validatingCString: $0.pointee.pw_dir) } ?? NSHomeDirectory()
        return (home as NSString).appendingPathComponent(relativePath)
    }

    static func current() -> String? {
        let text = (try? String(contentsOfFile: path, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        return text?.isEmpty == false ? text : nil
    }

    /// Compares in constant time, so the secret can't be guessed byte by byte from response times.
    static func matches(_ given: String?) -> Bool {
        guard let expected = current(), let given else { return false }
        let a = Array(expected.utf8), b = Array(given.utf8)
        guard a.count == b.count else { return false }
        return zip(a, b).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }
}

struct HTTPRequest {
    let method: String
    let path: String
    let query: [String: String]
    let headers: [String: String]  // lowercased names
    let body: Data
}

struct HTTPResponse {
    var status: Int = 200
    var contentType = "application/json"
    var body = Data()

    static func json(_ object: Any, status: Int = 200) -> HTTPResponse {
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
        return HTTPResponse(status: status, body: data)
    }
}

final class LocalServer: @unchecked Sendable {
    private let listener: NWListener
    private let allowedHosts: Set<String>
    private let handle: @Sendable (HTTPRequest) async -> HTTPResponse

    init(port: UInt16, handle: @escaping @Sendable (HTTPRequest) async -> HTTPResponse) throws {
        let params = NWParameters.tcp
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!)
        params.allowLocalEndpointReuse = true
        listener = try NWListener(using: params)
        allowedHosts = ["127.0.0.1:\(port)", "localhost:\(port)"]
        self.handle = handle
    }

    func start() {
        listener.newConnectionHandler = { [weak self] connection in self?.serve(connection) }
        listener.start(queue: .global())
    }

    private func serve(_ connection: NWConnection) {
        connection.start(queue: .global())
        receive(connection, buffer: Data())
    }

    private func receive(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { [weak self] data, _, done, error in
            guard let self else { return }
            var buffer = buffer
            if let data { buffer.append(data) }
            if let request = Self.parse(buffer) {
                Task {
                    // Only local programs, never a web page: a DNS-rebinding page sends its own Host,
                    // and browsers add Origin to cross-site requests. Neither reaches the handler.
                    let local = self.allowedHosts.contains(request.headers["host"] ?? "") && request.headers["origin"] == nil
                    // And only the connector: other programs on the Mac don't have the shared secret.
                    let response: HTTPResponse
                    if !local {
                        response = .json(["error": "forbidden"], status: 403)
                    } else if SharedToken.current() == nil {
                        response = .json(["error": "no shared token yet: the Life connector makes ~/\(SharedToken.relativePath) on its first call"], status: 503)
                    } else if !SharedToken.matches(request.headers[SharedToken.header]) {
                        response = .json(["error": "forbidden: missing or wrong X-Life-Home-Token"], status: 403)
                    } else {
                        response = await self.handle(request)
                    }
                    self.send(response, on: connection)
                }
            } else if done || error != nil || buffer.count > 1 << 20 {
                connection.cancel()
            } else {
                self.receive(connection, buffer: buffer)
            }
        }
    }

    private static func parse(_ data: Data) -> HTTPRequest? {
        guard let end = data.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let head = String(decoding: data[..<end.lowerBound], as: UTF8.self).components(separatedBy: "\r\n")
        let parts = (head.first ?? "").split(separator: " ")
        guard parts.count >= 2 else { return nil }
        var headers: [String: String] = [:]
        for line in head.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            headers[line[..<colon].lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        let length = Int(headers["content-length"] ?? "") ?? 0
        let body = data[end.upperBound...]
        guard body.count >= length else { return nil }
        let target = URLComponents(string: String(parts[1]))
        var query: [String: String] = [:]
        for item in target?.queryItems ?? [] { query[item.name] = item.value ?? "" }
        return HTTPRequest(method: String(parts[0]), path: target?.path ?? "/", query: query, headers: headers, body: Data(body.prefix(length)))
    }

    private func send(_ response: HTTPResponse, on connection: NWConnection) {
        let reason = [200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 500: "Error", 503: "Unavailable"][response.status] ?? "OK"
        var out = Data("HTTP/1.1 \(response.status) \(reason)\r\nContent-Type: \(response.contentType)\r\nContent-Length: \(response.body.count)\r\nConnection: close\r\n\r\n".utf8)
        out.append(response.body)
        connection.send(content: out, completion: .contentProcessed { _ in connection.cancel() })
    }
}
