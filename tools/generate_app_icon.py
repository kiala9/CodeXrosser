from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "CodexQuotaViewerWindows.Qt" / "codex_quota_viewer" / "assets"
SOURCE_SVG = ASSETS / "cqv-app-icon-source.svg"
SOURCE_ICON = ASSETS / "cqv-app-icon-source.png"


def scaled(value: float, size: int) -> float:
    return value * size / 1024.0


def draw_icon(size: int) -> QImage:
    if SOURCE_SVG.exists():
        renderer = QSvgRenderer(str(SOURCE_SVG))
        if not renderer.isValid():
            raise ValueError(f"Invalid app icon SVG source: {SOURCE_SVG}")

        image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        return image

    source = QImage(str(SOURCE_ICON))
    if source.isNull():
        raise FileNotFoundError(f"Missing app icon source: {SOURCE_ICON}")

    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    scaled_source = source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    x = round((size - scaled_source.width()) / 2)
    y = round((size - scaled_source.height()) / 2)
    painter.drawImage(x, y, scaled_source)

    painter.end()
    return image


def _font(size: int) -> QFont:
    font = QFont("Segoe UI")
    font.setWeight(QFont.Black)
    font.setPixelSize(round(scaled(236, size)))
    font.setStyleStrategy(QFont.PreferAntialias)
    return font


def image_to_png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_ico(path: Path, sizes: list[int]) -> None:
    entries: list[tuple[int, bytes]] = [(size, image_to_png_bytes(draw_icon(size))) for size in sizes]
    offset = 6 + 16 * len(entries)
    header = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    body = bytearray()
    for size, payload in entries:
        width = 0 if size >= 256 else size
        height = 0 if size >= 256 else size
        header.extend(struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(payload), offset))
        body.extend(payload)
        offset += len(payload)
    path.write_bytes(header + body)


def write_preview(path: Path) -> None:
    sizes = [16, 24, 32, 48, 64, 128]
    scale = 4
    padding = 28
    width = sum(size * scale for size in sizes) + padding * (len(sizes) + 1)
    height = 196
    preview = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    preview.fill(QColor("#07111c"))

    painter = QPainter(preview)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QColor("#d8e4ea"))
    painter.setFont(QFont("Segoe UI", 10))
    x = padding
    for size in sizes:
        image = draw_icon(size)
        pixmap = QPixmap.fromImage(image).scaled(size * scale, size * scale, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter.drawPixmap(x, 34, pixmap)
        painter.drawText(QRectF(x, 34 + size * scale + 14, size * scale, 24), Qt.AlignCenter, f"{size}px")
        x += size * scale + padding
    painter.end()
    preview.save(str(path))


def main() -> int:
    app = QGuiApplication(sys.argv)
    ASSETS.mkdir(parents=True, exist_ok=True)
    source = draw_icon(1024)
    source.save(str(SOURCE_ICON))
    source.save(str(ASSETS / "cqv-app-icon.png"))
    write_ico(ASSETS / "cqv-app-icon.ico", [16, 24, 32, 48, 64, 128, 256])
    write_preview(ASSETS / "cqv-app-icon-preview.png")
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
