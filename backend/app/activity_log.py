"""Aggregate camera activity without exposing person-level fields."""
from datetime import timedelta
from sqlalchemy import func
from .db import Detection, utcnow


def read_activity(db, minutes=60, camera_id="", page=1, page_size=50):
    until = utcnow()
    since = until - timedelta(minutes=minutes)
    minute = func.strftime("%Y-%m-%dT%H:%M:00Z", Detection.timestamp)
    query = db.query(minute.label("minute"), Detection.camera_id,
                     func.count(Detection.id).label("observations")).filter(
        Detection.timestamp >= since, Detection.timestamp <= until)
    if camera_id:
        query = query.filter(Detection.camera_id == camera_id)
    query = query.group_by(minute, Detection.camera_id)
    total = query.count()
    rows = query.order_by(minute.desc(), Detection.camera_id).offset(
        (page - 1) * page_size).limit(page_size).all()
    return {"items": [{"minute": row.minute, "camera_id": row.camera_id,
                       "observations": row.observations} for row in rows],
            "total": total, "page": page, "page_size": page_size,
            "since": since.isoformat() + "Z", "until": until.isoformat() + "Z"}
