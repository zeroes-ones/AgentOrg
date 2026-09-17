//
//  LineDecoderTests.swift
//  AgentOrgKitTests
//
//  The decoder is where a protocol violation becomes invisible if it is wrong.
//
//  A pipe delivers arbitrary byte chunks, so a line arrives across several reads, and a multi-byte
//  character can be split between two of them. Decoding naively at each read corrupts text and drops
//  events — silently, which is why this has its own tests rather than a couple of assertions.
//

import XCTest
@testable import AgentOrgKit

final class LineDecoderTests: XCTestCase {

    private func decode(_ chunks: [String]) -> [DecodedFrame] {
        let decoder = LineDecoder()
        var frames: [DecodedFrame] = []
        for chunk in chunks {
            frames.append(contentsOf: decoder.append(Data(chunk.utf8)))
        }
        return frames
    }

    private func events(_ frames: [DecodedFrame]) -> [EngineEvent] {
        frames.compactMap { if case .event(let event) = $0 { return event } else { return nil } }
    }

    func testDecodesAWholeLine() {
        let frames = decode(["{\"v\":1,\"seq\":1,\"type\":\"run.start\",\"payload\":{}}\n"])
        XCTAssertEqual(events(frames).count, 1)
        XCTAssertEqual(events(frames).first?.type, "run.start")
    }

    func testDecodesSeveralLinesInOneChunk() {
        let chunk = "{\"v\":1,\"seq\":1,\"type\":\"a\",\"payload\":{}}\n"
            + "{\"v\":1,\"seq\":2,\"type\":\"b\",\"payload\":{}}\n"
        let frames = decode([chunk])
        XCTAssertEqual(events(frames).map(\.type), ["a", "b"])
    }

    func testHoldsAPartialLineUntilItCompletes() {
        // The realistic case: a pipe hands over half a line.
        let decoder = LineDecoder()
        let first = decoder.append(Data("{\"v\":1,\"seq\":1,\"type\":\"run.".utf8))
        XCTAssertTrue(first.isEmpty, "an incomplete line must not be decoded")
        XCTAssertGreaterThan(decoder.pendingBytes, 0)

        let second = decoder.append(Data("start\",\"payload\":{}}\n".utf8))
        XCTAssertEqual(events(second).map(\.type), ["run.start"])
        XCTAssertEqual(decoder.pendingBytes, 0, "the buffer must drain")
    }

    func testSurvivesAMultiByteCharacterSplitAcrossChunks() {
        // A UTF-8 sequence split between reads: decoding per chunk would corrupt it.
        let text = "{\"v\":1,\"seq\":1,\"type\":\"agent.log\",\"payload\":{\"text\":\"caf\u{e9} \u{1f600}\"}}\n"
        let data = Data(text.utf8)
        let split = data.count / 2
        let decoder = LineDecoder()
        var frames = decoder.append(data.prefix(split))
        frames.append(contentsOf: decoder.append(data.suffix(from: split)))
        let decoded = events(frames)
        XCTAssertEqual(decoded.count, 1, "a split character must not lose the event")
        XCTAssertEqual(decoded.first?.payload["text"]?.stringValue, "caf\u{e9} \u{1f600}")
    }

    func testReportsAnUnparsableLineRatherThanDroppingIt() {
        // Silently dropping a bad line would make a contract drift invisible.
        let frames = decode(["this is not json\n"])
        guard case .unparsable(let text) = frames.first else {
            return XCTFail("an unparsable line must be reported")
        }
        XCTAssertTrue(text.contains("not json"))
    }

    func testSkipsBlankLines() {
        XCTAssertTrue(decode(["\n\n\n"]).isEmpty)
    }

    func testBoundsAnOversizedPartialLine() {
        // A runaway line must not grow the app's memory without bound.
        let decoder = LineDecoder(maxLineBytes: 64)
        let frames = decoder.append(Data(String(repeating: "x", count: 200).utf8))
        guard case .unparsable(let text) = frames.first else {
            return XCTFail("an oversized partial line must be reported and discarded")
        }
        XCTAssertTrue(text.contains("exceeded"))
        XCTAssertEqual(decoder.pendingBytes, 0)
    }

    func testPreservesUnknownEventTypes() {
        // A newer engine may emit a type this build has never seen; the app must keep running.
        let frames = decode(["{\"v\":1,\"seq\":1,\"type\":\"future.event\",\"payload\":{}}\n"])
        let decoded = events(frames)
        XCTAssertEqual(decoded.count, 1)
        XCTAssertEqual(decoded.first?.type, "future.event")
        XCTAssertFalse(decoded.first?.isKnown ?? true, "an unknown type must be reported as unknown")
    }

    func testDecodesCorrelationFields() {
        let line = """
        {"v":1,"seq":7,"type":"node.enter","payload":{},"run_id":"run_1","agent_id":"ag_1",\
        "node_id":"fixer","session_id":"ses_1","phase":"BUILD","ts":"2026-01-01T00:00:00.000Z"}

        """
        let decoded = events(decode([line]))[0]
        XCTAssertEqual(decoded.runId, "run_1")
        XCTAssertEqual(decoded.agentId, "ag_1")
        XCTAssertEqual(decoded.nodeId, "fixer")
        XCTAssertEqual(decoded.sessionId, "ses_1")
        XCTAssertEqual(decoded.phase, "BUILD")
    }

    func testPayloadValuesDecodeToTheRightJSONTypes() {
        let line = """
        {"v":1,"seq":1,"type":"llm.response","payload":{"usage":{"prompt_tokens":100,\
        "completion_tokens":20,"cost_usd":0.004,"measured":true},"model":"m","findings":[]}}

        """
        let payload = events(decode([line]))[0].payload
        XCTAssertEqual(payload["usage"]?["prompt_tokens"]?.intValue, 100)
        XCTAssertEqual(payload["usage"]?["cost_usd"]?.doubleValue ?? 0, 0.004, accuracy: 1e-9)
        XCTAssertEqual(payload["usage"]?["measured"]?.boolValue, true)
        XCTAssertEqual(payload["findings"]?.arrayValue?.count, 0)
    }

    func testDottedPathSubscriptWalksNestedPayloads() {
        let line = "{\"v\":1,\"seq\":1,\"type\":\"x\",\"payload\":{\"usage\":{\"total\":42}}}\n"
        let payload = events(decode([line]))[0].payload
        XCTAssertEqual(payload[path: "usage.total"]?.intValue, 42)
        XCTAssertNil(payload[path: "usage.missing"])
    }
}
