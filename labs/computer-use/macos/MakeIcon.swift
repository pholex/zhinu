// 生成应用图标：一枚带渐变的圆角方块，上面是一支指向右上的羽毛（小羽）兼作光标。
// 只用 CoreGraphics，输出 AppIcon.iconset 的全部尺寸，再由 build.py 调 iconutil 合成 .icns。
// 用法：swift MakeIcon.swift <输出目录>

import AppKit
import Foundation

func drawIcon(size: CGFloat) -> CGImage {
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let context = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8, bytesPerRow: 0,
                            space: colorSpace, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    let s = size
    //  macOS 图标规范：图形占画布约 80%，四周留白
    let inset = s * 0.1
    let rect = CGRect(x: inset, y: inset, width: s - inset * 2, height: s - inset * 2)
    let radius = rect.width * 0.225

    //  底：蓝 → 紫的斜向渐变
    let path = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)
    context.saveGState()
    context.addPath(path)
    context.clip()
    let colors = [CGColor(red: 0.16, green: 0.49, blue: 0.98, alpha: 1), CGColor(red: 0.55, green: 0.30, blue: 0.95, alpha: 1)] as CFArray
    let gradient = CGGradient(colorsSpace: colorSpace, colors: colors, locations: [0, 1])!
    context.drawLinearGradient(gradient, start: CGPoint(x: rect.minX, y: rect.maxY), end: CGPoint(x: rect.maxX, y: rect.minY), options: [])
    //  一点高光，免得像平涂
    let glow = CGGradient(colorsSpace: colorSpace, colors: [CGColor(red: 1, green: 1, blue: 1, alpha: 0.28), CGColor(red: 1, green: 1, blue: 1, alpha: 0)] as CFArray, locations: [0, 1])!
    context.drawRadialGradient(glow, startCenter: CGPoint(x: rect.minX + rect.width * 0.3, y: rect.maxY - rect.height * 0.2), startRadius: 0,
                               endCenter: CGPoint(x: rect.minX + rect.width * 0.3, y: rect.maxY - rect.height * 0.2), endRadius: rect.width * 0.9, options: [])
    context.restoreGState()

    //  羽毛：一条从左下到右上的羽轴，两侧各几片羽片，整体像一枚指向右上的光标
    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -s * 0.02), blur: s * 0.05, color: CGColor(red: 0, green: 0, blue: 0, alpha: 0.35))
    let cx = rect.midX, cy = rect.midY, w = rect.width
    context.translateBy(x: cx, y: cy)
    context.rotate(by: .pi / 4)   // 羽轴转到指向右上
    let feather = CGMutablePath()
    let half = w * 0.33           // 羽毛半长
    feather.move(to: CGPoint(x: 0, y: half))                                     // 尖
    feather.addCurve(to: CGPoint(x: 0, y: -half * 0.55), control1: CGPoint(x: w * 0.30, y: half * 0.45), control2: CGPoint(x: w * 0.22, y: -half * 0.35))
    feather.addCurve(to: CGPoint(x: 0, y: half), control1: CGPoint(x: -w * 0.22, y: -half * 0.35), control2: CGPoint(x: -w * 0.30, y: half * 0.45))
    feather.closeSubpath()
    context.addPath(feather)
    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 0.96))
    context.fillPath()
    context.restoreGState()

    //  羽轴与羽片的细纹：用底色画在羽毛上，形成"羽"的辨识度
    context.saveGState()
    context.translateBy(x: cx, y: cy)
    context.rotate(by: .pi / 4)
    context.setStrokeColor(CGColor(red: 0.36, green: 0.40, blue: 0.96, alpha: 0.9))
    context.setLineCap(.round)
    context.setLineWidth(max(1, w * 0.035))
    context.move(to: CGPoint(x: 0, y: half * 0.9))
    context.addLine(to: CGPoint(x: 0, y: -half * 0.95))
    context.strokePath()
    context.setLineWidth(max(1, w * 0.022))
    for i in 0..<5 {
        let y = half * 0.65 - CGFloat(i) * half * 0.26
        let len = w * (0.10 + CGFloat(i) * 0.025)
        for sign in [CGFloat(1), CGFloat(-1)] {
            context.move(to: CGPoint(x: 0, y: y))
            context.addLine(to: CGPoint(x: sign * len, y: y - half * 0.16))
        }
    }
    context.strokePath()
    context.restoreGState()
    return context.makeImage()!
}

func writePNG(_ image: CGImage, to url: URL) {
    let rep = NSBitmapImageRep(cgImage: image)
    try! rep.representation(using: .png, properties: [:])!.write(to: url)
}

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
for (points, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let name = scale == 1 ? "icon_\(points)x\(points).png" : "icon_\(points)x\(points)@2x.png"
    writePNG(drawIcon(size: CGFloat(points * scale)), to: out.appendingPathComponent(name))
}
print(out.path)
