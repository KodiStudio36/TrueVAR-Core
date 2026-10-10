"""AI cameraman core — the tkd_autocam.py logic as a library (no GUI, no CLI).

Shared by both disciplines:
  kyorugi : AthleteSelector (2 athletes by kit, referee ignored) + VirtualCamera
  poomsae : PoomsaeSelector (everyone on the mat) + PoomsaeCamera
  both    : MatRegion — colour-agnostic mat detection (any colour, square or octagon)

Changes from the proof of concept, all behaviour-preserving:
  * Detection runs at a lower rate (e.g. 10 Hz) than rendering (30 fps), so
    VirtualCamera.update() is split into observe() (new detections, ~10 Hz)
    and step() (spring integration, every rendered frame).
  * Per-step constants that were tuned for 30 fps (EMAs, hold/forget frame
    counts, median length, mat refresh) are rescaled to the detection rate by
    make_selector() / make_camera(), so the *time* behaviour is unchanged.
  * Everything works in "analysis" coordinates (the downscaled detector frame);
    the engine scales the crop rectangle up to the 1080p source when rendering.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

_BASE_FPS = 30.0      # rate the PoC constants were tuned for


# --------------------------------------------------------------------------
# Mat / competition area
# --------------------------------------------------------------------------
# Forced colours (mat_color setting): OpenCV hue centre (0-179) and half-width.
MAT_COLORS = {"blue": (108, 16), "red": (0, 10), "yellow": (26, 8), "green": (65, 20)}


def hue_name(h: float) -> str:
    h = float(h) % 180
    for limit, name in ((8, "red"), (18, "orange"), (35, "yellow"), (85, "green"),
                        (130, "blue"), (165, "purple")):
        if h < limit:
            return name
    return "red"


class MatRegion:
    """The competition area: where people must stand to count.

    Colour-agnostic (blue, yellow, red, green ... mats):
      1. LEARN — a hue histogram of strongly coloured pixels gives candidate
         colours. For each, the largest solid area is scored by
         size x solidity^2 x floor-position. The inner tatami is big and solid;
         the outer border is a ring (low solidity), walls/wood sit high in the
         frame, stand seats are fragments. The winner's colour is locked in.
      2. TRACK — every update only that colour is segmented (cheap), averaged
         over time, cleaned of thin things (legs, cables, blue trousers), and
         its outline fitted with the fewest corners (4..8) that match it:
         a square in perspective, the same cut by the frame edge, or an octagon.
         A coloured area that has another solid floor area INSIDE it is the
         border (e.g. a fully visible octagon inside its square), so the inner
         one wins even when the border is bigger.
      3. FOLLOW slow white-balance drift by re-centring on the mat's own hue;
         RE-LEARN every `relearn_every` updates, or at once if the mat is lost
         or suddenly shrinks (lighting change, camera moved). The current colour
         is kept unless another is clearly better, so it never flip-flops.
    A fixed polygon (mat_polygon setting) disables all of this.
    """

    WORK_W = 320                  # analysis width for the mat (cheap)
    S_MIN, V_MIN = 60, 40         # "strongly coloured" pixel

    def __init__(self, polygon=None, update_every=5, min_area_frac=0.03, color="auto",
                 max_vertices=8, relearn_every=40):
        self.poly = np.array(polygon, np.int32) if polygon else None
        self.fixed = self.poly is not None
        self.update_every = max(1, int(update_every))
        self.min_area_frac = min_area_frac
        self.color = color if color in MAT_COLORS else "auto"
        self.max_vertices = max_vertices
        self.relearn_every = max(1, int(relearn_every))
        self.hue, self.hue_hw = MAT_COLORS.get(self.color, (None, None))
        self._acc = None              # temporal average of the mat-colour mask
        self._area_ema = None         # typical mat size (to notice a sudden shrink)
        self._updates = 0
        self.info = {"source": "fixed" if self.fixed else "auto", "color": None, "corners": None}

    # ------------------------------------------------------------------ public

    def update(self, frame, frame_idx):
        if self.fixed or (self.poly is not None and frame_idx % self.update_every):
            return
        H, W = frame.shape[:2]
        s = self.WORK_W / W
        small = cv2.resize(frame, (self.WORK_W, max(1, int(H * s))), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        self._updates += 1
        learned = False
        if self.color == "auto" and (self.hue is None or self._updates % self.relearn_every == 0):
            self._learn(hsv)
            learned = True
        if self.hue is None:
            return
        found = self._track(hsv)
        if found in (None, "relearn") and self.color == "auto" and not learned:
            self._learn(hsv)                                   # mat lost / shrank: look again now
            found = self._track(hsv)
        if found in (None, "relearn"):
            return
        hull, area_frac = found
        poly = self._fit_polygon(hull)
        new = (poly.reshape(-1, 2) / s).astype(np.int32)
        if self.poly is None or self._iou(new, self.poly, frame.shape) < 0.96:
            self.poly = new                                    # ignore sub-percent jitter
        self.info = {"source": "auto", "color": hue_name(self.hue), "hue": int(round(self.hue)) % 180,
                     "corners": int(len(self.poly)), "area": round(float(area_frac), 3)}

    def contains(self, pt, tol):
        if self.poly is None:
            return True
        return cv2.pointPolygonTest(self.poly, (float(pt[0]), float(pt[1])), True) >= -tol

    def bbox(self):
        if self.poly is None:
            return None
        x, y, w, h = cv2.boundingRect(self.poly)
        return [x, y, x + w, y + h]

    def centre(self, W, H):
        if self.poly is None:
            return W / 2, H * 0.6
        m = cv2.moments(self.poly)
        if m["m00"] == 0:
            return W / 2, H * 0.6
        return m["m10"] / m["m00"], m["m01"] / m["m00"]

    # ------------------------------------------------------------------ internals

    def _hue_mask(self, hsv, hue, hw):
        d = np.abs(hsv[..., 0].astype(np.int16) - int(hue))
        d = np.minimum(d, 180 - d)                             # hue is circular (red wraps)
        return (d <= hw) & (hsv[..., 1] >= self.S_MIN) & (hsv[..., 2] >= self.V_MIN)

    def _areas(self, mask_u8, shape):
        """Mat-like components of a colour mask, each measured and scored."""
        h, w = shape
        k = max(5, int(round(w * 0.035)) | 1)                   # removes legs, cables, lines
        m = cv2.morphologyEx(mask_u8, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in cnts:
            area = cv2.contourArea(c)                          # outer outline: holes (people) ignored
            if area < self.min_area_frac * w * h:
                continue
            hull = cv2.convexHull(c)
            hull_area = max(cv2.contourArea(hull), 1.0)
            solidity = area / hull_area                        # open ring / U-shaped border -> low
            mom = cv2.moments(c)
            cx, cy = (mom["m10"] / mom["m00"], mom["m01"] / mom["m00"]) if mom["m00"] else (0.0, 0.0)
            bottom = c[:, 0, 1].max() / h
            # floor prior: walls / stands sit high in the picture, the mat lower down
            floor = min(max((cy / h - 0.3) / 0.2, 0.05), 1.0) * (0.5 + 0.5 * min(max((bottom - 0.4) / 0.3, 0.0), 1.0))
            out.append({"score": (area / (w * h)) * solidity ** 2 * floor, "hull": hull,
                        "hull_area": hull_area, "area": area / (w * h), "solidity": solidity,
                        "floor": floor, "c": (cx, cy)})
        return out

    def _best_area(self, mask_u8, shape):
        areas = self._areas(mask_u8, shape)
        return max(areas, key=lambda a: a["score"]) if areas else None

    def _learn(self, hsv):
        """Find the mat colour: the hue whose biggest solid area looks most like a mat."""
        h, w = hsv.shape[:2]
        col = (hsv[..., 1] >= self.S_MIN) & (hsv[..., 2] >= self.V_MIN)
        hist = np.bincount(hsv[..., 0][col].ravel(), minlength=180).astype(np.float64)[:180]
        if hist.sum() < self.min_area_frac * w * h:
            return
        g = np.exp(-0.5 * (np.arange(-6, 7) / 2.0) ** 2)
        hs = np.convolve(np.r_[hist[-6:], hist, hist[:6]], g / g.sum(), "valid")   # circular smooth
        peaks = [i for i in range(180) if hs[i] >= hs[i - 1] and hs[i] >= hs[(i + 1) % 180]
                 and hs[i] >= 0.08 * hs.max()]
        peaks = sorted(peaks, key=lambda i: -hs[i])[:6]
        cands = []
        for p in peaks:                                        # half-width: down to 25% or a valley
            hw = 4
            for side in (-1, 1):
                j, prev = p, hs[p]
                for _ in range(22):
                    j2 = (j + side) % 180
                    if hs[j2] < 0.25 * hs[p] or hs[j2] > prev * 1.05:
                        break
                    j, prev = j2, hs[j2]
                    hw = max(hw, min(abs(j - p), 180 - abs(j - p)))
            cands.append((p, min(max(hw + 2, 6), 22)))
        if self.hue is not None:                               # current colour competes too
            cands.append((self.hue, self.hue_hw))
        found = []
        for hue, hw in cands:
            best = self._best_area(self._hue_mask(hsv, hue, hw).astype(np.uint8) * 255, (h, w))
            if best is not None:
                found.append((hue, hw, best))
        # Nesting rule: an area with another solid floor area of a different colour
        # INSIDE it is the border around the competition area, not the area itself
        # (e.g. a fully visible octagon inside its square, a mat inside a coloured floor).
        scored = []
        for hue, hw, a in found:
            border = any(
                abs(((hue2 - hue) + 90) % 180 - 90) > 6 and b["solidity"] >= 0.75 and b["floor"] >= 0.3
                and b["hull_area"] >= 0.04 * a["hull_area"] and b["area"] < a["area"]
                and cv2.pointPolygonTest(a["hull"], b["c"], False) > 0
                for hue2, _, b in found)
            scored.append((a["score"] * (0.02 if border else 1.0), hue, hw))
        if not scored:
            return
        score, hue, hw = max(scored)
        if self.hue is not None:                               # hysteresis: keep the current colour
            cur = [sc for sc, hu, _ in scored if hu == self.hue]
            if cur and cur[0] >= 0.7 * score:
                return
        if self.hue is None or abs(((hue - self.hue) + 90) % 180 - 90) > 6:
            self._acc = None                                   # different colour: restart averaging
            self._area_ema = None
        self.hue, self.hue_hw = int(hue), int(hw)

    def _track(self, hsv):
        raw = self._hue_mask(hsv, self.hue, self.hue_hw)
        mask = raw.astype(np.float32)
        self._acc = mask if self._acc is None or self._acc.shape != mask.shape \
            else 0.7 * self._acc + 0.3 * mask
        best = self._best_area((self._acc > 0.5).astype(np.uint8) * 255, hsv.shape[:2])
        if best is None:
            return None
        if self.color == "auto":
            # follow slow white-balance / lighting drift: re-centre on the mat's own hue
            inside = np.zeros(raw.shape, np.uint8)
            cv2.fillPoly(inside, [best["hull"].reshape(-1, 2)], 1)
            hues = hsv[..., 0][raw & (inside > 0)].astype(np.float64)
            if hues.size > 50:
                ang = np.deg2rad(hues * 2.0)
                mean = (np.rad2deg(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) / 2.0) % 180
                diff = ((mean - self.hue) + 90) % 180 - 90
                self.hue = (self.hue + 0.3 * diff) % 180
            # sudden shrink (light change, camera moved, wrong colour): look again
            if self._area_ema is not None and best["area"] < 0.6 * self._area_ema:
                self._area_ema = None
                return "relearn"
            self._area_ema = best["area"] if self._area_ema is None else 0.8 * self._area_ema + 0.2 * best["area"]
        return best["hull"], best["area"]

    def _fit_polygon(self, hull, tol=0.03):
        """Fewest corners (4..max_vertices) whose area matches the hull within tol."""
        hull_area = max(cv2.contourArea(hull), 1.0)
        peri = cv2.arcLength(hull, True)
        best = hull
        for f in np.linspace(0.12, 0.004, 60):                 # coarse -> fine
            poly = cv2.approxPolyDP(hull, f * peri, True)
            if len(poly) < 4:
                continue
            if len(poly) > self.max_vertices:
                break
            best = poly
            if abs(cv2.contourArea(poly) - hull_area) / hull_area <= tol:
                return poly
        return best

    @staticmethod
    def _iou(a, b, shape, scale=0.25):
        h, w = int(shape[0] * scale) + 1, int(shape[1] * scale) + 1
        ma, mb = np.zeros((h, w), np.uint8), np.zeros((h, w), np.uint8)
        cv2.fillPoly(ma, [np.round(a * scale).astype(np.int32)], 1)
        cv2.fillPoly(mb, [np.round(b * scale).astype(np.int32)], 1)
        union = np.count_nonzero(ma | mb)
        return np.count_nonzero(ma & mb) / union if union else 0.0


# --------------------------------------------------------------------------
# Athlete selection
# --------------------------------------------------------------------------
def _region_hsv(frame, box, top, bottom, inset=0.15):
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    rx0, rx1 = x0 + int(inset * w), x1 - int(inset * w)
    ry0, ry1 = y0 + int(top * h), y0 + int(bottom * h)
    crop = frame[max(ry0, 0):max(ry1, 0), max(rx0, 0):max(rx1, 0)]
    if crop.size == 0:
        return None
    crop = cv2.resize(crop, (24, 24), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)


def kit_colour_score(frame: np.ndarray, box) -> float:
    """0..1 'looks like a taekwondo athlete' score (white dobok, red/blue hogu)."""
    torso = _region_hsv(frame, box, 0.2, 0.55)
    legs = _region_hsv(frame, box, 0.6, 0.92)
    if torso is None or legs is None:
        return 0.0

    def white(hsv):
        return (hsv[..., 1] < 50) & (hsv[..., 2] > 150)

    hh, ss, vv = torso[..., 0], torso[..., 1], torso[..., 2]
    red = ((hh < 10) | (hh > 170)) & (ss > 90) & (vv > 60)
    blue = (hh > 95) & (hh < 130) & (ss > 90) & (vv > 50)
    torso_kit = float(np.mean(white(torso) | red | blue))
    legs_white = float(np.mean(white(legs)))
    dark = float(np.mean(legs[..., 2] < 80))
    return max(0.0, min(1.0, 0.45 * torso_kit + 0.55 * legs_white - 0.5 * dark))


@dataclass
class AthleteSelector:
    max_athletes: int = 2
    kit_min: float = 0.2
    ema: float = 0.1
    kit_ema: float = 0.2
    hold_frames: int = 20
    forget_after: int = 90
    switch_margin: float = 1.4
    foot_tol: float = 0.0
    scores: dict = field(default_factory=dict)
    last_seen: dict = field(default_factory=dict)
    last_box: dict = field(default_factory=dict)
    kit: dict = field(default_factory=dict)
    chosen: list = field(default_factory=list)

    def select(self, frame, boxes, ids, frame_idx, mat: MatRegion):
        H, W = frame.shape[:2]
        mcx, mcy = mat.centre(W, H)
        current, on_mat = {}, {}
        for box, tid in zip(boxes, ids):
            x0, y0, x1, y1 = box
            if not mat.contains(((x0 + x1) / 2, y1), self.foot_tol * W):
                continue
            area = (x1 - x0) * (y1 - y0) / (W * H)
            d = math.hypot(((x0 + x1) / 2 - mcx) / W, (y1 - mcy) / H)
            centrality = 1.0 - min(1.0, d / 0.7)
            k = kit_colour_score(frame, box)
            self.kit[tid] = (1 - self.kit_ema) * self.kit.get(tid, k) + self.kit_ema * k
            raw = math.sqrt(area) * (0.5 + centrality) * (0.1 + self.kit[tid])
            old = self.scores.get(tid, raw)
            self.scores[tid] = (1 - self.ema) * old + self.ema * raw
            self.last_seen[tid] = frame_idx
            self.last_box[tid] = box
            on_mat[tid] = box
            if self.kit[tid] >= self.kit_min:
                current[tid] = box

        for tid in [t for t, f in self.last_seen.items()
                    if frame_idx - f > self.forget_after]:
            for d in (self.scores, self.last_seen, self.last_box, self.kit):
                d.pop(tid, None)

        score = lambda t: self.scores.get(t, 0.0)
        visible = [t for t in self.chosen if t in current]
        ghosts = [t for t in self.chosen if t not in current
                  and frame_idx - self.last_seen.get(t, -10**9) <= self.hold_frames]
        others = sorted((t for t in current if t not in visible), key=score, reverse=True)

        kept = list(visible)
        free = self.max_athletes - len(kept)
        kept += others[:free]
        others = others[free:]
        kept += ghosts[: self.max_athletes - len(kept)]

        if others and visible:
            weakest = min((t for t in kept if t in current), key=score)
            if score(others[0]) > self.switch_margin * score(weakest):
                kept[kept.index(weakest)] = others[0]

        self.chosen = kept
        held = [t for t in kept if t not in current]
        return [current.get(t, self.last_box.get(t)) for t in kept], kept, held, set(on_mat)


@dataclass
class PoomsaeSelector:
    """Everyone whose feet are on the competition square — no kit test, no referee.

    * enter_frames: a person must stand on the mat this long before the shot
      widens for them (someone crossing a corner doesn't yank the camera);
    * hold_frames: a person briefly hidden (overlap in pairs/teams) or detected
      just off the edge for a moment is held at their last position;
    * max_people: safety cap (team poomsae); the most central ones win.
    Same select() contract as AthleteSelector.
    """
    max_people: int = 8
    enter_frames: int = 15
    hold_frames: int = 30
    forget_after: int = 90
    foot_tol: float = 0.0            # feet may be this far outside the mat edge, x the person's
                                     # own height (perspective-correct: same distance near & far)
    on_count: dict = field(default_factory=dict)
    last_seen: dict = field(default_factory=dict)
    last_box: dict = field(default_factory=dict)
    chosen: list = field(default_factory=list)
    kit: dict = field(default_factory=dict)      # unused, kept for a uniform interface

    def select(self, frame, boxes, ids, frame_idx, mat: MatRegion):
        H, W = frame.shape[:2]
        mcx, mcy = mat.centre(W, H)
        on_mat = {}
        for box, tid in zip(boxes, ids):
            x0, y0, x1, y1 = box
            if not mat.contains(((x0 + x1) / 2, y1), self.foot_tol * (y1 - y0)):
                self.on_count.pop(tid, None)
                continue
            on_mat[tid] = box
            self.on_count[tid] = self.on_count.get(tid, 0) + 1
            self.last_seen[tid] = frame_idx
            self.last_box[tid] = box

        for tid in [t for t, f in self.last_seen.items() if frame_idx - f > self.forget_after]:
            for d in (self.on_count, self.last_seen, self.last_box):
                d.pop(tid, None)

        confirmed = {t: b for t, b in on_mat.items()
                     if self.on_count.get(t, 0) >= self.enter_frames or t in self.chosen}
        ghosts = [t for t in self.chosen if t not in confirmed
                  and frame_idx - self.last_seen.get(t, -10**9) <= self.hold_frames]
        kept = list(confirmed) + ghosts
        if len(kept) > self.max_people:
            def dist(t):
                x0, y0, x1, y1 = self.last_box[t]
                return math.hypot(((x0 + x1) / 2 - mcx) / W, (y1 - mcy) / H)
            kept = sorted(kept, key=dist)[: self.max_people]
        self.chosen = kept
        held = [t for t in kept if t not in confirmed]
        return [self.last_box[t] for t in kept], kept, held, set(on_mat)


# --------------------------------------------------------------------------
# Virtual camera
# --------------------------------------------------------------------------
def smooth_damp(current, target, vel, smooth_time, dt):
    """Critically damped spring (same idea as Unity's SmoothDamp)."""
    omega = 2.0 / max(smooth_time, 1e-4)
    x = omega * dt
    exp = 1.0 / (1.0 + x + 0.48 * x * x + 0.235 * x * x * x)
    change = current - target
    temp = (vel + omega * change) * dt
    vel = (vel - omega * temp) * exp
    return target + (change + temp) * exp, vel


@dataclass
class VirtualCamera:
    frame_w: int
    frame_h: int
    out_w: int = 1280
    out_h: int = 720
    max_zoom: float = 1.6
    athlete_size: float = 0.45
    min_width_athletes: float = 3.5
    zoom_out_time: float = 0.35
    pad_x: float = 1.6
    pad_y: float = 1.4
    headroom: float = 0.05
    pan_time: float = 0.8
    zoom_time: float = 1.6
    dead_zone_x: float = 0.07
    dead_zone_y: float = 0.12
    zoom_dead_zone: float = 0.10
    median_len: int = 9
    lost_timeout: float = 1.5

    def __post_init__(self):
        self.aspect = self.out_w / self.out_h
        self.cx, self.cy = self.frame_w / 2, self.frame_h / 2
        self.w = self.full_w()
        self.vx = self.vy = self.vw = 0.0
        self.tx, self.ty, self.tw = self.cx, self.cy, self.w
        self.lost_for = 0.0
        self.too_big = False
        self.hist = deque(maxlen=self.median_len)

    def full_w(self):
        return min(self.frame_w, self.frame_h * self.aspect)

    def _fit(self, boxes, pad_x, pad_y, headroom):
        b = np.array(boxes, dtype=float)
        x0, y0 = b[:, 0].min(), b[:, 1].min()
        x1, y1 = b[:, 2].max(), b[:, 3].max()
        w = max((x1 - x0) * pad_x, (y1 - y0) * pad_y * self.aspect)
        person_h = float((b[:, 3] - b[:, 1]).max())
        w = max(w, person_h / self.athlete_size * self.aspect,
                person_h * self.min_width_athletes)
        w = min(max(w, self.full_w() / self.max_zoom), self.full_w())
        return (x0 + x1) / 2, (y0 + y1) / 2 - headroom * (w / self.aspect), w

    def observe(self, boxes, dt, wide_box=None):
        """New detection result. `dt` = seconds since the previous detection."""
        if boxes:
            self.lost_for = 0.0
            now = self._fit(boxes, self.pad_x, self.pad_y, self.headroom)
            self.hist.append(now)
            tx, ty, tw = np.median(np.array(self.hist), axis=0)
            self.too_big = now[2] > self.w * 1.02
            if self.too_big:
                self.tx, self.ty, self.tw = now[0], now[1], now[2]
            else:
                if abs(tx - self.tx) > self.dead_zone_x * self.w:
                    self.tx = tx
                if abs(ty - self.ty) > self.dead_zone_y * self.w / self.aspect:
                    self.ty = ty
                if abs(tw - self.tw) > self.zoom_dead_zone * self.w:
                    self.tw = tw
        else:
            self.too_big = False
            self.lost_for += dt
            if self.lost_for > self.lost_timeout:
                self.hist.clear()
                if wide_box:
                    self.tx, self.ty, self.tw = self._fit([wide_box], 1.1, 1.3, 0.1)
                else:
                    self.tx, self.ty, self.tw = self.frame_w / 2, self.frame_h / 2, self.full_w()

    def step(self, dt):
        """Advance the springs by `dt` seconds (call once per rendered frame)."""
        if self.too_big:
            zt = pt = self.zoom_out_time
        else:
            zt = self.zoom_time * (0.5 if self.tw > self.w else 1.0)
            pt = self.pan_time
        self.cx, self.vx = smooth_damp(self.cx, self.tx, self.vx, pt, dt)
        self.cy, self.vy = smooth_damp(self.cy, self.ty, self.vy, pt, dt)
        self.w, self.vw = smooth_damp(self.w, self.tw, self.vw, zt, dt)
        self._clamp()

    def _clamp(self):
        self.w = min(max(self.w, self.full_w() / self.max_zoom), self.full_w())
        h = self.w / self.aspect
        self.cx = min(max(self.cx, self.w / 2), self.frame_w - self.w / 2)
        self.cy = min(max(self.cy, h / 2), self.frame_h - h / 2)

    def crop_rect_f(self):
        """(x0, y0, w, h) as floats, in analysis coordinates."""
        h = self.w / self.aspect
        return self.cx - self.w / 2, self.cy - h / 2, self.w, h

    @property
    def zoom(self):
        return self.full_w() / self.w


class PoomsaeCamera(VirtualCamera):
    """Detail-first 'hold, then re-frame' camera for poomsae.

      * ZOOM: always at max_zoom (1.5 = 1:1 pixels for 1080p -> 720p). It zooms
        out only when someone's body would leave the frame (just enough), and
        back in only after everyone has fitted comfortably for zoom_in_delay_s.
      * HOLD: while the athletes move around inside the shot, the camera does
        not move at all.
      * RE-FRAME: when the group has drifted more than move_threshold (fraction
        of the shot) from where the camera points, or is about to touch an edge
        (edge_margin), the camera moves decisively — fast (pan_time) and
        re-centred — so the next move only comes after another big drift.
      * lookahead_s keeps room for where a moving group is heading (stepping
        toward the camera makes athletes bigger AND lower), so the hold never
        lets a fast step get cut.
    """

    def __init__(self, *a, edge_margin=0.05, headroom_margin=0.08, foot_margin=0.04,
                 zoom_in_delay_s=2.5, move_threshold=0.25, lookahead_s=0.6, **k):
        super().__init__(*a, **k)
        self.edge_margin = edge_margin
        self.headroom_margin = headroom_margin
        self.foot_margin = foot_margin
        self.zoom_in_delay_s = zoom_in_delay_s
        self.move_threshold = move_threshold
        self.lookahead_s = lookahead_s
        self._prev_box = None
        self._vel = np.zeros(4)
        self._fits_for = 0.0
        self.moves = 0                                          # re-frames so far (stats)
        self.w = self.tw = self.full_w() / self.max_zoom        # start zoomed in

    def min_w(self):
        return self.full_w() / self.max_zoom

    def observe(self, boxes, dt, wide_box=None):
        if not boxes:
            self.too_big = False
            self.lost_for += dt
            if self.lost_for > self.lost_timeout:            # mat empty: calm wide shot of it
                self.hist.clear()
                self._fits_for = 0.0
                self._prev_box, self._vel = None, np.zeros(4)
                if wide_box:
                    self.tx, self.ty, self.tw = self._fit([wide_box], 1.05, 1.15, 0.05)
                else:
                    self.tx, self.ty, self.tw = self.frame_w / 2, self.frame_h / 2, self.full_w()
            return
        self.lost_for = 0.0
        b = np.array(boxes, dtype=float)
        self.hist.append((b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()))
        box = np.median(np.array(self.hist), axis=0)        # glitch-free group box
        if self._prev_box is not None and dt > 0:
            self._vel = 0.6 * self._vel + 0.4 * (box - self._prev_box) / dt
        self._prev_box = box
        pred = box + self._vel * self.lookahead_s           # where it is heading
        x0, y0 = min(box[0], pred[0]), min(box[1], pred[1])
        x1, y1 = max(box[2], pred[2]), max(box[3], pred[3])
        gw, gh = x1 - x0, y1 - y0

        # ---- zoom: smallest window that still contains everyone (with slack)
        old_tw = self.tw
        need = max(gw / (1 - 2 * self.edge_margin),
                   gh / (1 - self.headroom_margin - self.foot_margin) * self.aspect)
        need = min(max(need * 1.04, self.min_w()), self.full_w())
        if need > self.tw:                                   # someone would be cut: widen now
            self.tw, self._fits_for = need, 0.0
        elif need < self.tw * 0.85:                          # fits comfortably for a while: back in
            self._fits_for += dt
            if self._fits_for >= self.zoom_in_delay_s:
                self.tw, self._fits_for = need, 0.0
        else:
            self._fits_for = 0.0
        self.too_big = self.tw > self.w * 1.02

        # ---- pan: hold still; re-frame (re-centred) only on a big drift or an edge
        w, h = self.tw, self.tw / self.aspect
        mx = self.edge_margin * w
        lo_x, hi_x = x1 + mx - w / 2, x0 - mx + w / 2        # window centres that keep all inside
        lo_y = y1 + self.foot_margin * h - h / 2
        hi_y = y0 - self.headroom_margin * h + h / 2
        want_x = (lo_x + hi_x) / 2                           # well-composed (centred) position
        want_y = (lo_y + hi_y) / 2
        must = not (lo_x <= self.tx <= hi_x and lo_y <= self.ty <= hi_y)
        drift = (abs(want_x - self.tx) > self.move_threshold * w or
                 abs(want_y - self.ty) > self.move_threshold * h)
        if must or drift or self.tw != old_tw:
            if must or drift:
                self.moves += 1
            self.tx, self.ty = want_x, want_y

    def step(self, dt):
        zt = self.zoom_out_time if self.tw > self.w else self.zoom_time
        self.cx, self.vx = smooth_damp(self.cx, self.tx, self.vx, self.pan_time, dt)
        self.cy, self.vy = smooth_damp(self.cy, self.ty, self.vy, self.pan_time, dt)
        self.w, self.vw = smooth_damp(self.w, self.tw, self.vw, zt, dt)
        self._clamp()


# --------------------------------------------------------------------------
# Factories — rescale the 30 fps-tuned constants to the detection rate
# --------------------------------------------------------------------------
def _per_step(alpha_at_base: float, fps: float) -> float:
    """Convert an EMA weight tuned at 30 fps to the same time constant at `fps`."""
    return 1.0 - (1.0 - alpha_at_base) ** (_BASE_FPS / fps)


def _frames(n_at_base: int, fps: float, minimum: int = 1) -> int:
    return max(minimum, int(round(n_at_base * fps / _BASE_FPS)))


def make_selector(cfg, det_fps: float):
    if getattr(cfg, "discipline", "kyorugi") == "poomsae":
        return PoomsaeSelector(
            max_people=cfg.max_people,
            enter_frames=max(1, int(round(cfg.enter_s * det_fps))),
            hold_frames=max(2, int(round(cfg.hold_s * det_fps))),
            forget_after=max(3, int(round(max(3.0, cfg.hold_s * 3) * det_fps))),
            foot_tol=cfg.foot_tolerance,
        )
    if cfg.include_referee:
        n, kit_min = 3, 0.0
    else:
        n, kit_min = cfg.athletes, cfg.kit_min
    return AthleteSelector(
        max_athletes=n, kit_min=kit_min,
        ema=_per_step(0.1, det_fps), kit_ema=_per_step(0.2, det_fps),
        hold_frames=_frames(20, det_fps, 2), forget_after=_frames(90, det_fps, 3),
    )


def make_camera(cfg, frame_w: int, frame_h: int, det_fps: float) -> VirtualCamera:
    median = _frames(9, det_fps, 3) | 1         # odd, >= 3
    if getattr(cfg, "discipline", "kyorugi") == "poomsae":
        cam = PoomsaeCamera(
            frame_w, frame_h, cfg.output_width, cfg.output_height,
            max_zoom=cfg.max_zoom, zoom_out_time=cfg.zoom_out_time, median_len=median,
            pan_time=cfg.move_time, zoom_time=cfg.move_time * 2, lost_timeout=3.0,
            edge_margin=cfg.edge_margin, zoom_in_delay_s=cfg.zoom_in_delay_s,
            move_threshold=cfg.move_threshold, lookahead_s=cfg.lookahead_s,
        )
    else:
        cam = VirtualCamera(
            frame_w, frame_h, cfg.output_width, cfg.output_height,
            max_zoom=cfg.max_zoom, athlete_size=cfg.athlete_size,
            min_width_athletes=cfg.min_width_athletes, zoom_out_time=cfg.zoom_out_time,
            median_len=median,
        )
        cam.pan_time *= cfg.smooth
        cam.zoom_time *= cfg.smooth
    return cam


def make_mat(cfg, scale: float, det_fps: float) -> MatRegion:
    poly = None
    if cfg.mat_polygon:
        poly = [[int(round(x * scale)), int(round(y * scale))] for x, y in cfg.mat_polygon]
    return MatRegion(poly, update_every=_frames(15, det_fps),
                     color=getattr(cfg, "mat_color", "auto"),
                     relearn_every=max(1, int(round(20.0 * det_fps / _frames(15, det_fps)))))  # ~20 s