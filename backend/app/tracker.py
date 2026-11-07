from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional
import time

def iou_xyxy(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0, ax2-ax1) * max(0, ay2-ay1)
    area_b = max(0, bx2-bx1) * max(0, by2-by1)
    union = area_a + area_b - inter + 1e-9
    return inter / union

@dataclass
class Track:
    track_id: int
    bbox: Tuple[float, float, float, float]
    last_seen: float
    participant_id: Optional[int] = None
    participant_name: Optional[str] = None
    score: Optional[float] = None

class SimpleTracker:
    def __init__(self, iou_threshold=0.3, max_age_sec=1.0):
        self.iou_threshold = iou_threshold
        self.max_age_sec = max_age_sec
        self._next_id = 1
        self.tracks: Dict[int, Track] = {}

    def update(self, detections_xyxy: List[Tuple[float,float,float,float]]) -> List[Track]:
        now = time.time()
        # purge old
        for tid in list(self.tracks.keys()):
            if now - self.tracks[tid].last_seen > self.max_age_sec:
                del self.tracks[tid]

        assigned = set()
        # match detections to existing tracks
        for det in detections_xyxy:
            best_tid = None
            best_iou = 0.0
            for tid, tr in self.tracks.items():
                if tid in assigned:
                    continue
                v = iou_xyxy(det, tr.bbox)
                if v > best_iou:
                    best_iou = v
                    best_tid = tid
            if best_tid is not None and best_iou >= self.iou_threshold:
                tr = self.tracks[best_tid]
                tr.bbox = det
                tr.last_seen = now
                assigned.add(best_tid)
            else:
                tid = self._next_id
                self._next_id += 1
                self.tracks[tid] = Track(track_id=tid, bbox=det, last_seen=now)
                assigned.add(tid)

        return list(self.tracks.values())