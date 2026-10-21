from datetime import datetime, timezone

from app.errors import AppError
from app.repositories.conversations import ConversationRepository


class ConversationDeletionService:
    """Bound each HTTP sweep; durable job and deleting parent make cleanup resumable."""

    def __init__(self, db):
        self.db = db
        self.repository = ConversationRepository(db)

    def start(self, uid, conversation_id, max_batches=10):
        job_ref = self.db.collection("job_runs").document(conversation_id)
        existing = job_ref.get()
        job = existing.to_dict() or {}
        if existing.exists and (
            job.get("owner_uid") != uid or job.get("kind") != "conversation_delete"
        ):
            raise AppError(404, "job_not_found", "작업을 찾을 수 없습니다.")
        parent = self.db.collection("conversations").document(conversation_id).get()
        if not parent.exists and existing.exists:
            job_ref.update({"status": "completed", "completed_at": datetime.now(timezone.utc)})
            return {"job_id": conversation_id, "status": "completed"}
        self.repository.mark_deleting(uid, conversation_id)
        now = datetime.now(timezone.utc)
        job_ref.set(
            {
                "kind": "conversation_delete",
                "owner_uid": uid,
                "target_id": conversation_id,
                "status": "pending",
                "created_at": job.get("created_at", now),
                "updated_at": now,
            },
            merge=True,
        )
        for _ in range(max_batches):
            result = self.repository.sweep(uid, conversation_id)
            if result["parent_deleted"]:
                job_ref.update({"status": "completed", "completed_at": datetime.now(timezone.utc)})
                return {"job_id": conversation_id, "status": "completed"}
        return {
            "job_id": conversation_id,
            "status": "pending",
            "status_url": f"/api/jobs/{conversation_id}",
        }

    def status(self, uid, job_id):
        snapshot = self.db.collection("job_runs").document(job_id).get()
        job = snapshot.to_dict() or {}
        if not snapshot.exists or job.get("owner_uid") != uid:
            raise AppError(404, "job_not_found", "작업을 찾을 수 없습니다.")
        return {"id": snapshot.id, **{k: v for k, v in job.items() if k != "owner_uid"}}
