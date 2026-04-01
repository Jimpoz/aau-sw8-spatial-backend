import base64
from pathlib import Path
from xml.sax.saxutils import escape

import cv2
import numpy as np
import svgwrite


class RoomVectorizer:
    def __init__(
        self,
        vector_palette_size: int = 8,
        max_vector_width: int = 480,
    ) -> None:
        self.vector_palette_size = max(4, vector_palette_size)
        self.max_vector_width = max(240, max_vector_width)

    def vectorize_view(
        self,
        frame: np.ndarray,
        view_index: int,
        source_name: str,
        object_counts: dict[str, int],
    ) -> str:
        scaled = self._resize_for_vectorization(frame)
        height, width = scaled.shape[:2]
        footer_height = 88

        drawing = svgwrite.Drawing(size=(width, height + footer_height))
        drawing.add(
            drawing.rect(
                insert=(0, 0),
                size=(width, height + footer_height),
                fill="#f5f1e8",
            )
        )

        # Reduce the frame to a small palette before tracing shapes so the SVG stays lightweight.
        quantized, labels, centers = self._quantize_colors(scaled)
        total_area = width * height
        min_area = max(120, total_area // 450)

        for color_index, center in enumerate(centers):
            mask = np.where(labels == color_index, 255, 0).astype(np.uint8)
            mask = cv2.morphologyEx(
                mask,
                cv2.MORPH_OPEN,
                np.ones((3, 3), dtype=np.uint8),
            )
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            contours = sorted(contours, key=cv2.contourArea, reverse=True)[:48]

            fill = self._rgb_hex(tuple(int(channel) for channel in center))
            for contour in contours:
                area = cv2.contourArea(contour)
                if area < min_area:
                    continue
                epsilon = 0.012 * cv2.arcLength(contour, True)
                polygon = cv2.approxPolyDP(contour, epsilon, True)
                points = [
                    (int(point[0][0]), int(point[0][1]))
                    for point in polygon
                    if len(point[0]) == 2
                ]
                if len(points) < 3:
                    continue
                drawing.add(
                    drawing.polygon(
                        points=points,
                        fill=fill,
                        stroke=fill,
                        stroke_width=1,
                    )
                )

        # Add a subtle contour layer so large surfaces keep some structural detail.
        edges = cv2.Canny(cv2.cvtColor(quantized, cv2.COLOR_BGR2GRAY), 60, 140)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(
            contours,
            key=lambda contour_points: cv2.arcLength(contour_points, False),
            reverse=True,
        )[:120]:
            if cv2.arcLength(contour, False) < 30:
                continue
            points = [
                (int(point[0][0]), int(point[0][1]))
                for point in contour[::4]
            ]
            if len(points) < 2:
                continue
            drawing.add(
                drawing.polyline(
                    points=points,
                    fill="none",
                    stroke="#2b241d",
                    stroke_width=1,
                    opacity=0.18,
                )
            )

        drawing.add(
            drawing.rect(
                insert=(0, height),
                size=(width, footer_height),
                fill="#1f1c18",
                opacity=0.94,
            )
        )
        drawing.add(
            drawing.text(
                f"View {view_index}",
                insert=(18, height + 28),
                fill="#f8f3eb",
                font_size=20,
                font_family="Helvetica, Arial, sans-serif",
                font_weight="bold",
            )
        )
        drawing.add(
            drawing.text(
                self.display_source_name(source_name, view_index),
                insert=(18, height + 54),
                fill="#d6cbbb",
                font_size=14,
                font_family="Helvetica, Arial, sans-serif",
            )
        )

        summary_text = self._format_counts(object_counts)
        drawing.add(
            drawing.text(
                summary_text[0],
                insert=(140, height + 35),
                fill="#f8f3eb",
                font_size=14,
                font_family="Helvetica, Arial, sans-serif",
            )
        )
        if len(summary_text) > 1:
            drawing.add(
                drawing.text(
                    summary_text[1],
                    insert=(140, height + 55),
                    fill="#f8f3eb",
                    font_size=14,
                    font_family="Helvetica, Arial, sans-serif",
                )
            )
        drawing.add(
            drawing.text(
                "Room summary generated from the uploaded image set",
                insert=(140, height + 74),
                fill="#d6cbbb",
                font_size=12,
                font_family="Helvetica, Arial, sans-serif",
            )
        )
        return drawing.tostring()

    def embed_frame_svg(self, frame: np.ndarray, source_name: str) -> str:
        height, width = frame.shape[:2]
        # Store the original raster in an SVG wrapper so it can be persisted as plain text.
        success, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 92],
        )
        if not success:
            raise ValueError(f"Could not encode uploaded image {source_name!r}.")

        image_base64 = base64.b64encode(encoded.tobytes()).decode("ascii")
        safe_name = escape(Path(source_name).name or "image")
        return (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
            f"viewBox='0 0 {width} {height}'>"
            f"<title>{safe_name}</title>"
            f"<image width='{width}' height='{height}' preserveAspectRatio='none' "
            f"href='data:image/jpeg;base64,{image_base64}' />"
            "</svg>"
        )

    @staticmethod
    def display_source_name(source_name: str, view_index: int) -> str:
        display_name = Path(source_name).name.strip() or f"image_{view_index}"
        if len(display_name) <= 28:
            return display_name
        return f"{display_name[:25]}..."

    def _resize_for_vectorization(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        if width <= self.max_vector_width:
            return frame.copy()

        scale = self.max_vector_width / width
        resized_height = max(1, int(round(height * scale)))
        return cv2.resize(
            frame,
            (self.max_vector_width, resized_height),
            interpolation=cv2.INTER_AREA,
        )

    def _quantize_colors(
        self,
        frame: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        pixels = np.float32(frame.reshape((-1, 3)))
        criteria = (
            cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
            12,
            1.0,
        )
        _, labels, centers = cv2.kmeans(
            pixels,
            self.vector_palette_size,
            None,
            criteria,
            2,
            cv2.KMEANS_PP_CENTERS,
        )
        centers = np.uint8(centers)
        quantized = centers[labels.flatten()].reshape(frame.shape)
        return quantized, labels.reshape(frame.shape[:2]), centers

    @staticmethod
    def _format_counts(object_counts: dict[str, int]) -> list[str]:
        if not object_counts:
            return ["No objects detected in this view"]

        chunks: list[str] = []
        current_chunk = ""

        for token in (
            f"{label} x{count}"
            for label, count in sorted(object_counts.items())
        ):
            separator = " | " if current_chunk else ""
            candidate = f"{current_chunk}{separator}{token}"
            if len(candidate) > 48 and current_chunk:
                chunks.append(current_chunk)
                current_chunk = token
            else:
                current_chunk = candidate

        if current_chunk:
            chunks.append(current_chunk)

        return chunks[:2]

    @staticmethod
    def _rgb_hex(color: tuple[int, int, int]) -> str:
        blue, green, red = color
        return f"#{red:02x}{green:02x}{blue:02x}"
