// Life Home: a background helper that serves Apple Home to the Life connector.
// Loopback-only HTTP API on 127.0.0.1:8767 (see LocalServer.swift). No Dock icon; the window
// closes itself after start-up. Endpoints:
//   GET  /health                      {ok, authorization, homes}
//   GET  /home                        every home, room, accessory (services → characteristics with
//                                     values and metadata), scene and automation, with ids
//   POST /read       {characteristic} fresh value
//   POST /write      {characteristic, value}
//   POST /scene      {id}              run a scene
//   POST /automation {id, enabled}
import HomeKit
import SwiftUI

@main
struct LifeHomeApp: App {
    @StateObject private var model = HomeModel()
    var body: some Scene {
        WindowGroup {
            Text(model.status).padding()
                .task {
                    try? await Task.sleep(for: .seconds(5))
                    for scene in UIApplication.shared.connectedScenes {
                        UIApplication.shared.requestSceneSessionDestruction(scene.session, options: nil)
                    }
                }
        }
    }
}

@MainActor
final class HomeModel: NSObject, ObservableObject, HMHomeManagerDelegate {
    @Published var status = "Waiting for Apple Home…"
    private let manager = HMHomeManager()
    private var server: LocalServer?

    override init() {
        super.init()
        manager.delegate = self
        server = try? LocalServer(port: 8767) { [weak self] request in
            guard let self else { return .json(["error": "gone"], status: 503) }
            return await self.respond(request)
        }
        server?.start()
    }

    nonisolated func homeManagerDidUpdateHomes(_ manager: HMHomeManager) {
        Task { @MainActor in self.refreshStatus() }
    }

    nonisolated func homeManager(_ manager: HMHomeManager, didUpdate status: HMHomeManagerAuthorizationStatus) {
        Task { @MainActor in self.refreshStatus() }
    }

    private func refreshStatus() {
        let count = manager.homes.reduce(0) { $0 + $1.accessories.count }
        status = "Life Home · authorization \(manager.authorizationStatus.rawValue) · \(manager.homes.count) home(s) · \(count) accessories"
    }

    // MARK: - API

    func respond(_ request: HTTPRequest) async -> HTTPResponse {
        let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any] ?? [:]
        switch (request.method, request.path) {
        case ("GET", "/health"):
            return .json(["ok": true, "authorization": manager.authorizationStatus.rawValue, "homes": manager.homes.count])
        case ("GET", "/home"):
            await refreshValues()
            return .json(["homes": manager.homes.map(describe)])
        case ("POST", "/read"):
            guard let c = characteristic(body["characteristic"]) else { return notFound("characteristic") }
            do {
                try await c.readValue()
                return .json(["value": jsonValue(c.value)])
            } catch { return failure(error, c.service?.accessory) }
        case ("POST", "/write"):
            guard let c = characteristic(body["characteristic"]) else { return notFound("characteristic") }
            guard c.properties.contains(HMCharacteristicPropertyWritable) else {
                return .json(["error": "\(c.localizedDescription) can't be changed"], status: 400)
            }
            do {
                try await c.writeValue(body["value"])
                return .json(["value": jsonValue(c.value)])
            } catch { return failure(error, c.service?.accessory) }
        case ("POST", "/scene"):
            guard let (home, set) = actionSet(body["id"]) else { return notFound("scene") }
            do {
                try await home.executeActionSet(set)
                return .json(["ran": set.name])
            } catch { return failure(error, nil) }
        case ("POST", "/automation"):
            guard let trigger = trigger(body["id"]), let enabled = body["enabled"] as? Bool else { return notFound("automation") }
            do {
                try await trigger.enable(enabled)
                return .json(["name": trigger.name, "enabled": trigger.isEnabled])
            } catch { return failure(error, nil) }
        default:
            return .json(["error": "no such endpoint"], status: 404)
        }
    }

    /// HomeKit's cached values start empty; read every readable value of reachable accessories
    /// (concurrently, at most ~4 s in total) so /home reports real state.
    private func refreshValues() async {
        let targets = manager.homes.flatMap(\.accessories).filter(\.isReachable)
            .flatMap(\.services).flatMap(\.characteristics)
            .filter { $0.properties.contains(HMCharacteristicPropertyReadable)
                && ![HMCharacteristicMetadataFormatTLV8, HMCharacteristicMetadataFormatData].contains($0.metadata?.format ?? "") }
        // Reads run on their own; we answer when they're all done or after 4 s, whichever is first.
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            let once = Once(done)
            Task { @MainActor in
                await withTaskGroup(of: Void.self) { group in
                    for c in targets { group.addTask { try? await c.readValue() } }
                }
                once.fire()
            }
            Task { try? await Task.sleep(for: .seconds(4)); once.fire() }
        }
    }

    private func notFound(_ what: String) -> HTTPResponse { .json(["error": "no such \(what)"], status: 404) }

    private func failure(_ error: Error, _ accessory: HMAccessory?) -> HTTPResponse {
        let who = accessory.map { " (\($0.name)\($0.isReachable ? "" : " isn't reachable"))" } ?? ""
        return .json(["error": error.localizedDescription + who], status: 500)
    }

    // MARK: - Lookup by id

    private func uuid(_ any: Any?) -> UUID? { (any as? String).flatMap(UUID.init(uuidString:)) }

    private func characteristic(_ id: Any?) -> HMCharacteristic? {
        guard let id = uuid(id) else { return nil }
        for home in manager.homes {
            for accessory in home.accessories {
                for service in accessory.services {
                    if let c = service.characteristics.first(where: { $0.uniqueIdentifier == id }) { return c }
                }
            }
        }
        return nil
    }

    private func actionSet(_ id: Any?) -> (HMHome, HMActionSet)? {
        guard let id = uuid(id) else { return nil }
        for home in manager.homes {
            if let set = home.actionSets.first(where: { $0.uniqueIdentifier == id }) { return (home, set) }
        }
        return nil
    }

    private func trigger(_ id: Any?) -> HMTrigger? {
        guard let id = uuid(id) else { return nil }
        return manager.homes.flatMap(\.triggers).first(where: { $0.uniqueIdentifier == id })
    }

    // MARK: - Describing the home

    private func jsonValue(_ value: Any?) -> Any {
        switch value {
        case let n as NSNumber: return n
        case let s as String: return s
        case let d as Data: return d.base64EncodedString()
        case nil: return NSNull()
        default: return String(describing: value!)
        }
    }

    private func describe(_ home: HMHome) -> [String: Any] {
        [
            "id": home.uniqueIdentifier.uuidString,
            "name": home.name,
            "primary": home.isPrimary,
            "rooms": home.rooms.map { ["id": $0.uniqueIdentifier.uuidString, "name": $0.name] },
            "accessories": home.accessories.map(describe),
            "scenes": home.actionSets.map {
                ["id": $0.uniqueIdentifier.uuidString, "name": $0.name, "type": $0.actionSetType, "actions": actions($0)]
            },
            "automations": home.triggers.map { trigger in
                [
                    "id": trigger.uniqueIdentifier.uuidString, "name": trigger.name, "enabled": trigger.isEnabled,
                    "scenes": trigger.actionSets.filter { $0.actionSetType != HMActionSetTypeTriggerOwned }.map(\.name),
                    "actions": trigger.actionSets.filter { $0.actionSetType == HMActionSetTypeTriggerOwned }.flatMap(actions),
                ] as [String: Any]
            },
        ]
    }

    /// What a scene or automation does, e.g. "Front Camera: Active → 0".
    private func actions(_ set: HMActionSet) -> [String] {
        set.actions.compactMap { action in
            guard let write = action as? HMCharacteristicWriteAction<NSCopying> else { return nil }
            let c = write.characteristic
            return "\(c.service?.accessory?.name ?? "?"): \(c.localizedDescription) → \(jsonValue(write.targetValue))"
        }.sorted()
    }

    private func describe(_ accessory: HMAccessory) -> [String: Any] {
        [
            "id": accessory.uniqueIdentifier.uuidString,
            "name": accessory.name,
            "room": accessory.room?.name ?? "",
            "category": accessory.category.localizedDescription,
            "reachable": accessory.isReachable,
            "services": accessory.services.map { service in
                [
                    "id": service.uniqueIdentifier.uuidString,
                    "name": service.name,
                    "type": service.localizedDescription,
                    "primary": service.isPrimaryService,
                    "characteristics": service.characteristics.map(describe),
                ] as [String: Any]
            },
        ]
    }

    private func describe(_ c: HMCharacteristic) -> [String: Any] {
        var out: [String: Any] = [
            "id": c.uniqueIdentifier.uuidString,
            "name": c.localizedDescription,
            "type": c.characteristicType,
            "value": jsonValue(c.value),
            "readable": c.properties.contains(HMCharacteristicPropertyReadable),
            "writable": c.properties.contains(HMCharacteristicPropertyWritable),
        ]
        if let m = c.metadata {
            out["format"] = m.format ?? ""
            if let v = m.minimumValue { out["min"] = v }
            if let v = m.maximumValue { out["max"] = v }
            if let v = m.stepValue { out["step"] = v }
            if let v = m.units { out["unit"] = v }
            if let v = m.validValues { out["valid"] = v }
        }
        return out
    }
}

/// Resumes a continuation exactly once, from whichever caller gets there first.
final class Once: @unchecked Sendable {
    private var continuation: CheckedContinuation<Void, Never>?
    private let lock = NSLock()
    init(_ continuation: CheckedContinuation<Void, Never>) { self.continuation = continuation }
    func fire() {
        lock.lock(); let c = continuation; continuation = nil; lock.unlock()
        c?.resume()
    }
}
