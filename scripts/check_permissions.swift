import ApplicationServices
import CoreGraphics
import Foundation

let accessibility = AXIsProcessTrusted()
let screenRecording = CGPreflightScreenCaptureAccess()
print("Accessibility: \(accessibility ? "granted" : "missing")")
print("Screen Recording: \(screenRecording ? "granted" : "missing")")
exit(accessibility && screenRecording ? 0 : 1)
