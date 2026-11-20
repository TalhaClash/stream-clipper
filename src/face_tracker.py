import os

import cv2
import numpy as np
from typing import Optional, Tuple, List, Dict

# YuNet: OpenCV's own tiny CNN face detector (face_detection_yunet_2023mar,
# ~233 KB, MIT, in opencv/opencv_zoo). It is already available inside the
# installed opencv-python-headless 4.10.0.84 via cv2.FaceDetectorYN — no new
# dependency. Measured on WIDER FACE it scores 0.884/0.866/0.750
# (Easy/Medium/Hard) vs Haar's 0.373/0.382/0.211, ~5x faster, and it is
# strong on side faces, occlusions and poor lighting — the exact ways Haar
# misses a real facecam. It also returns 5 facial landmarks per face, which
# replace the hard 0.35 top-rejection: a detection is accepted only if its
# landmarks form a plausible face (both eyes above the mouth, inter-eye
# distance > ~25% of box width). That kills the GTA altitude-gauge false
# positive (a round UI element has no face landmarks) WITHOUT rejecting a
# genuine facecam at the top of the screen — the Phoenix video's cam sits
# top-right at y≈0.22, which the old 0.35 rule rejected, so the job fell
# back to the bottom-left minimap. YuNet is tried first; Haar is the
# fallback when the model file is missing or YuNet finds nothing.
YUNET_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models", "face_detection_yunet_2023mar.onnx")
YUNET_SCORE_THRESHOLD = 0.6
YUNET_NMS_THRESHOLD = 0.3


class FaceTracker:
    """
    Detects the streamer's facecam coordinates from gaming stream video
    frames using YuNet, OpenCV's tiny CNN face detector (100% free, zero
    cloud API costs).

    The key idea: the streamer's facecam PiP stays in ONE fixed spot for
    the whole stream, while in-game characters move around. So we sample
    frames across the clip, detect every face, cluster them by position,
    and the cluster that shows up in the most frames — in a corner, small
    — is the streamer. The old code took the median of ALL detected faces,
    which averaged in-game characters in and put the box in the middle of
    the gameplay.
    """

    def __init__(self):
        # YuNet detector, created lazily on first use (the model file may
        # be absent on an older image — then no faces are found and the
        # caller degrades to full gameplay). The old Haar cascade is
        # GONE (deploy #73): it was the source of every fake cam on
        # no-cam videos (16px game texture, 214px NPC face, GTA gauge).
        self._yunet = None
        self._yunet_size = None

    def _yunet_detector(self, width: int, height: int):
        """Lazy singleton YuNet detector, re-sized when the frame size
        changes. Returns None when the model file is missing."""
        if not os.path.exists(YUNET_MODEL_PATH):
            return None
        if self._yunet is None:
            try:
                self._yunet = cv2.FaceDetectorYN.create(
                    YUNET_MODEL_PATH, "",
                    (width, height),
                    YUNET_SCORE_THRESHOLD, YUNET_NMS_THRESHOLD, 5000)
                self._yunet_size = (width, height)
            except cv2.error:
                self._yunet = None
                return None
        elif self._yunet_size != (width, height):
            self._yunet.setInputSize((width, height))
            self._yunet_size = (width, height)
        return self._yunet

    @staticmethod
    def _landmarks_plausible(det_row, box_w: float) -> bool:
        """A YuNet detection is a real face only if its 5 landmarks form
        one: both eyes above the mouth, and the eyes far enough apart.
        A round game-HUD element (the GTA altitude gauge that Haar kept
        flagging) has no such landmark geometry, so it is rejected here
        instead of by a blanket y-position rule."""
        re_x, re_y, le_x, le_y = float(det_row[4]), float(det_row[5]), float(det_row[6]), float(det_row[7])
        rmc_y, lmc_y = float(det_row[11]), float(det_row[13])
        eye_d = abs(le_x - re_x)
        eyes_above_mouth = max(re_y, le_y) < min(rmc_y, lmc_y)
        return eyes_above_mouth and eye_d > 0.25 * box_w

    def _detect_faces_yunet(self, frame) -> List[Dict]:
        """YuNet faces in one frame, landmark-validated. Returns dicts
        {cx, cy, x, y, w, h} in the frame's own coordinates. Empty when
        the model is missing or nothing plausible is found."""
        height, width = frame.shape[:2]
        det = self._yunet_detector(width, height)
        if det is None:
            return []
        try:
            _, faces = det.detect(frame)
        except cv2.error:
            return []
        if faces is None:
            return []
        out: List[Dict] = []
        for row in faces:
            x, y, w, h = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            if w < 20 or h < 20:
                continue
            if not self._landmarks_plausible(row, w):
                continue
            out.append({
                "cx": x + w / 2.0,
                "cy": y + h / 2.0,
                "x": int(x), "y": int(y),
                "w": int(w), "h": int(h),
            })
        return out

    def detect_pip_box(
        self,
        video_path: str,
        sample_frame_count: int = 4
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Finds the streamer's whole CAMERA window (the PiP rectangle), not
        just his face. The PiP is a fixed rectangle with a visible border
        in a corner of the source; his face sits inside it. We detect his
        face to anchor the search, then look for the rectangular contour
        that contains that face. Returns (x, y, w, h) of the PiP, or None
        when no rectangle is found (the caller falls back to the expanded
        face box). A face-only crop is wrong — the user wants the full
        cam screen (head, shoulders, room), as in his reference frames.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return None
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_area = float(width * height)

        # Anchor: his face centre, from the same upscale detection.
        face_box = self.detect_facecam_box(
            video_path, sample_frame_count=sample_frame_count, margin=0.0)
        if face_box is None:
            cap.release()
            return None
        fx, fy, fw, fh = face_box
        # margin=0.0 gives the raw face box; its centre is the anchor.
        anchor_x = fx + fw / 2.0
        anchor_y = fy + fh / 2.0

        # Collect candidate rectangles across sampled frames.
        candidates: List[Tuple[int, int, int, int]] = []
        step = max(1, total_frames // (sample_frame_count + 1))
        for i in range(1, sample_frame_count + 1):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i * step)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.Canny(gray, 50, 150)
            contours, _ = cv2.findContours(
                edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for cnt in contours:
                area = cv2.contourArea(cnt)
                # The PiP is a small-to-medium rectangle, not the whole
                # frame and not a speck.
                if area < frame_area * 0.01 or area > frame_area * 0.35:
                    continue
                peri = cv2.arcLength(cnt, True)
                approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
                if len(approx) != 4:
                    continue
                x, y, w, h = cv2.boundingRect(approx)
                if w < 60 or h < 40:
                    continue
                # The rectangle must contain his face.
                if not (x <= anchor_x <= x + w and y <= anchor_y <= y + h):
                    continue
                # It must be roughly 16:9 / 4:3, not a thin strip.
                ratio = w / float(h)
                if ratio < 1.0 or ratio > 2.4:
                    continue
                candidates.append((int(x), int(y), int(w), int(h)))
        cap.release()

        if not candidates:
            return None
        # The PiP never moves: take the median rectangle.
        mx = int(np.median([c[0] for c in candidates]))
        my = int(np.median([c[1] for c in candidates]))
        mw = int(np.median([c[2] for c in candidates]))
        mh = int(np.median([c[3] for c in candidates]))
        return (mx, my, mw, mh)

    def detect_facecam_box(
        self,
        video_path: str,
        sample_frame_count: int = 8,
        margin: float = 1.5
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Samples frames across the video, finds the face position that is
        most consistent across frames (the streamer's PiP), and returns an
        expanded box (x, y, w, h) covering head + shoulders + room. The
        margin is 1.5 (box = 4x the face) so the streamer appears zoomed
        OUT by 50% instead of a giant cropped face — the old 0.5 margin
        (box = 2x the face) filled the whole top and cut his head off
        whenever he leaned. Returns None when no stable face is found, so
        the caller degrades to full gameplay instead of a fake cam.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return None

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # Collect every detected face with its centre, so we can cluster.
        # YuNet ONLY (CNN, landmark-validated). The Haar fallback is
        # REMOVED (deploy #73): it was the source of every fake cam on
        # no-cam videos. On the no-commentary GTA video YuNet correctly
        # finds nothing, but Haar "found" a 16px game texture AND a
        # 214px in-game NPC face at (1497, 808) that sat stable in 2
        # frames — both blew up into a blurry smear / blurry face-like
        # blob on top. YuNet's landmarks already prove a detection is
        # a real face, so a top-of-screen facecam is accepted and no
        # y-position rejection is needed. If YuNet finds nothing, this
        # returns None and the caller degrades to full gameplay — a
        # SAFE degradation, never a fake cam. (WIDER FACE: YuNet 0.884
        # vs Haar 0.373, so a face YuNet misses but Haar catches is
        # unlikely; and a missed face now costs full gameplay, not a
        # broken top section.)
        faces: List[Dict] = []
        step = max(1, total_frames // (sample_frame_count + 1))

        for i in range(1, sample_frame_count + 1):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i * step)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            faces.extend(self._detect_faces_yunet(frame))

        cap.release()

        if not faces:
            return None

        # Cluster faces whose centres are close together. The streamer's
        # PiP never moves, so its cluster collects a hit in nearly every
        # sampled frame; in-game faces drift and split into small clusters.
        clusters: List[List[Dict]] = []
        for f in faces:
            placed = False
            for cluster in clusters:
                cx = sum(c["cx"] for c in cluster) / len(cluster)
                cy = sum(c["cy"] for c in cluster) / len(cluster)
                if abs(f["cx"] - cx) < 60 and abs(f["cy"] - cy) < 60:
                    cluster.append(f)
                    placed = True
                    break
            if not placed:
                clusters.append([f])

        def cluster_score(cluster: List[Dict]) -> float:
            # Most frames wins; prefer a small face at an EDGE (a PiP),
            # not a big central in-game character. The user's PiP is
            # bottom-left in his own streams, but Speed's is middle-left
            # — so the edge bonus covers left/right edges too, not just
            # the bottom corners. A face in the TOP THIRD is a game
            # HUD/character, never the streamer — it is rejected after
            # the vote (see below), not here.
            med_w = float(np.median([c["w"] for c in cluster]))
            med_h = float(np.median([c["h"] for c in cluster]))
            cx = float(np.median([c["cx"] for c in cluster]))
            cy = float(np.median([c["cy"] for c in cluster]))
            in_bottom_corner = (
                (cx < width * 0.30 or cx > width * 0.70)
                and cy > height * 0.65
            )
            at_side_edge = cx < width * 0.30 or cx > width * 0.70
            small = med_w < width * 0.30 and med_h < height * 0.30
            score = float(len(cluster))
            if in_bottom_corner:
                score += 2.0
            elif at_side_edge:
                score += 1.0
            if small:
                score += 1.0
            return score

        best = max(clusters, key=cluster_score)
        # The streamer must appear in at least 2 sampled frames, otherwise
        # it is a passing in-game face, not a fixed PiP.
        if len(best) < 2:
            return None

        # A real facecam face is never tiny. Speed's is 75px wide,
        # Phoenix's 81–111px (in 1920x1080 coords). Reject clusters
        # whose median face is below 40px (scaled to the frame width)
        # so a small in-game face can never become a fake cam.
        med_face_w = float(np.median([b["w"] for b in best]))
        if med_face_w < 40.0 * (width / 1920.0):
            return None

        # NO top-rejection any more: every face here is YuNet +
        # landmark-validated, so a genuine facecam at the top of the
        # screen (the Phoenix video's cam sits top-right at y≈0.22)
        # is accepted. The old 0.35 rule existed only to kill Haar's
        # GTA-gauge false positive — Haar is gone, and YuNet does not
        # see the gauge as a face at all (verified on Speed's frame).

        avg_x = int(np.median([b["x"] for b in best]))
        avg_y = int(np.median([b["y"] for b in best]))
        avg_w = int(np.median([b["w"] for b in best]))
        avg_h = int(np.median([b["h"] for b in best]))

        # Expand box by margin to capture head + shoulders / webcam frame
        pad_w = int(avg_w * margin)
        pad_h = int(avg_h * margin)

        x1 = max(0, avg_x - pad_w)
        y1 = max(0, avg_y - pad_h)
        x2 = min(width, avg_x + avg_w + pad_w)
        y2 = min(height, avg_y + avg_h + pad_h)

        # Size guard: a PiP is a small corner window. If the expanded box
        # covers most of the frame, it is the main content (a talking-head
        # stream, or a big in-game face) — not a cam. Instead of returning
        # None (which would make the caller fall back to a corner that may
        # be a blank wall), return a centred box around the face, clamped
        # to half the frame, so the top section still shows the streamer.
        if (x2 - x1) > width * 0.60 or (y2 - y1) > height * 0.60:
            bw = int(width * 0.50)
            bh = int(height * 0.50)
            cx = int(np.median([b["cx"] for b in best]))
            cy = int(np.median([b["cy"] for b in best]))
            x1 = max(0, min(cx - bw // 2, width - bw))
            y1 = max(0, min(cy - bh // 2, height - bh))
            return (x1, y1, bw, bh)

        return (x1, y1, x2 - x1, y2 - y1)
