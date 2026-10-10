// visionkit.swift — the macOS Vision helper the Python side shells out to.
//
// Two questions the reel pipeline keeps asking and must never answer with a guess:
//   faces  — where is a person's face in this frame? (caption cards must never land on one)
//   ocr    — is there readable writing baked into this frame? (the film must carry no captions
//            of its own; the only words on screen are ours, added after the cut)
//
// It is Apple's own on-device model: offline, deterministic, no API, no key, no pip dependency —
// and it actually sees a face on a warm amber frame, which a colour-based skin rule does not
// (that rule fired on 78% of a photoreal reel's pixels and printed a title across the man's face).
//
//   swiftc -O visionkit.swift -o .cache/visionkit
//   .cache/visionkit faces a.png b.png
//   .cache/visionkit ocr   a.png b.png
//
// One line per input:  <path>\t<BOX> <BOX> …   or  <path>\tNONE
// A BOX is  x,y,w,h  in PIXELS, origin top-left (Vision's origin is bottom-left; converted here
// so the Python side can compare against the ffmpeg/PIL coordinate space directly).

import Foundation
import Vision
import CoreImage

func box(_ b: CGRect, _ W: CGFloat, _ H: CGFloat) -> String {
    let x = b.minX * W, y = (1 - b.maxY) * H
    return String(format: "%.0f,%.0f,%.0f,%.0f", x, y, b.width * W, b.height * H)
}

let argv = Array(CommandLine.arguments.dropFirst())
guard let mode = argv.first, mode == "faces" || mode == "ocr" else {
    FileHandle.standardError.write("usage: visionkit faces|ocr <png>…\n".data(using: .utf8)!)
    exit(2)
}

for path in argv.dropFirst() {
    guard let img = CIImage(contentsOf: URL(fileURLWithPath: path)) else {
        print("\(path)\tERR"); continue
    }
    let W = img.extent.width, H = img.extent.height
    let handler = VNImageRequestHandler(ciImage: img, options: [:])
    var out: [String] = []

    if mode == "faces" {
        let req = VNDetectFaceRectanglesRequest()
        do { try handler.perform([req]) } catch { print("\(path)\tERR"); continue }
        out = (req.results ?? []).map { box($0.boundingBox, W, H) }
    } else {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = false
        // Whatever scripts this machine's Vision actually ships. Asking for a language it does
        // not have throws, so the list is filtered against its own answer first.
        let want = ["hi-IN", "en-US"]
        let have = (try? req.supportedRecognitionLanguages()) ?? []
        let langs = want.filter { have.contains($0) }
        if !langs.isEmpty { req.recognitionLanguages = langs }
        do { try handler.perform([req]) } catch { print("\(path)\tERR"); continue }
        out = (req.results ?? []).compactMap { o in
            guard let c = o.topCandidates(1).first, c.string.trimmingCharacters(in: .whitespaces).count >= 2
            else { return nil }
            return "\(box(o.boundingBox, W, H)):\(String(format: "%.2f", c.confidence)):\(c.string)"
        }
    }
    print("\(path)\t\(out.isEmpty ? "NONE" : out.joined(separator: " | "))")
}
