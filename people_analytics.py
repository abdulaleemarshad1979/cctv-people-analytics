"""Track-level line counting and pinhole-camera calibration math."""

import math
import time


class LineCounter:
    def __init__(self, line, in_side="positive"):
        self.validate_line(line)
        self.line = line
        self.in_side = in_side
        self.in_count = 0
        self.out_count = 0
        self.tracks = {}

    @staticmethod
    def validate_line(line):
        try:
            if len(line) != 2 or any(len(point) != 2 for point in line):
                raise ValueError()
            numbers = [float(v) for point in line for v in point]
            if any(not math.isfinite(v) or v < 0 or v > 1 for v in numbers):
                raise ValueError()
            if math.dist(numbers[:2], numbers[2:]) < 0.05:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("Line needs two distinct points inside the image") from None

    def set_line(self, line, in_side):
        self.validate_line(line)
        self.line = line
        self.in_side = in_side
        self.forget_tracks()

    def forget_tracks(self):
        self.tracks.clear()

    def reset_counts(self):
        self.in_count = self.out_count = 0
        self.forget_tracks()

    def counts(self):
        return {"in": self.in_count, "out": self.out_count,
                "occupancy_change": self.in_count - self.out_count}

    def update(self, detections, width, height, now=None):
        """Count a stable track once when its foot crosses the finite line segment."""
        now = time.monotonic() if now is None else now
        (ax, ay), (bx, by) = self.line
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        for track_id, (x1, y1, x2, y2) in detections:
            x, y = (x1 + x2) / (2 * width), y2 / height
            along = ((x - ax) * dx + (y - ay) * dy) / (length * length)
            signed = (dx * (y - ay) - dy * (x - ax)) / length
            side = 1 if signed > 0.025 else -1 if signed < -0.025 else 0
            prior = self.tracks.get(track_id)
            if prior and side and prior["side"] and side != prior["side"]:
                # The segment bounds stop someone crossing its infinite extension.
                if -0.05 <= along <= 1.05 and -0.05 <= prior["along"] <= 1.05:
                    entered = (side == 1) == (self.in_side == "positive")
                    if entered:
                        self.in_count += 1
                    else:
                        self.out_count += 1
            if side:
                self.tracks[track_id] = {"side": side, "along": along, "seen": now}
            elif prior:
                prior["seen"] = now
        for track_id in list(self.tracks):
            if now - self.tracks[track_id]["seen"] > 5:
                del self.tracks[track_id]


def _ground_ratio(y_fraction, vfov_deg, tilt_deg):
    angle = math.radians(tilt_deg)
    image_y = (2 * y_fraction - 1) * math.tan(math.radians(vfov_deg) / 2)
    denominator = math.cos(angle) - image_y * math.sin(angle)
    return (math.sin(angle) + image_y * math.cos(angle)) / denominator


def estimate_height_from_person(head_y, foot_y, vfov_deg, tilt_deg, person_height_m):
    """Estimate mounting height from a known-height upright person on level ground."""
    if not (0 <= head_y < foot_y <= 1):
        raise ValueError("Mark the head above the feet inside the image")
    foot = _ground_ratio(foot_y, vfov_deg, tilt_deg)
    head = _ground_ratio(head_y, vfov_deg, tilt_deg)
    if foot <= 0 or foot <= head:
        raise ValueError("Person is above the horizon or camera angles are inconsistent")
    height = person_height_m * foot / (foot - head)
    if not math.isfinite(height) or not 0.2 <= height <= 100:
        raise ValueError("Estimated mounting height is outside 0.2–100 m")
    return height


def estimate_camera_geometry(config):
    """Return visible level-ground distances; horizon can make the far edge unbounded."""
    height = config.get("camera_height_m")
    if height is None:
        return None
    height = float(height)
    vfov = float(config["vertical_fov_deg"])
    hfov = float(config["horizontal_fov_deg"])
    tilt = float(config["tilt_deg"])
    bottom = _ground_ratio(1, vfov, tilt)
    top = _ground_ratio(0, vfov, tilt)
    if bottom <= 0:
        return {"height_m": height, "near_m": None, "far_m": None,
                "near_width_m": None, "far_width_m": None}
    near = height / bottom
    far = height / top if top > 0 else None
    spread = 2 * height * math.tan(math.radians(hfov) / 2)
    tilt_radians = math.radians(tilt)
    vfov_tangent = math.tan(math.radians(vfov) / 2)
    near_vertical = math.sin(tilt_radians) + vfov_tangent * math.cos(tilt_radians)
    far_vertical = math.sin(tilt_radians) - vfov_tangent * math.cos(tilt_radians)
    return {"height_m": round(height, 2), "near_m": round(near, 2),
            "far_m": round(far, 2) if far is not None else None,
            "near_width_m": round(spread / near_vertical, 2),
            "far_width_m": round(spread / far_vertical, 2) if far is not None else None}
