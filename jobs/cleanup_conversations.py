"""Resume deletion-marked conversations for JOB_OWNER_UID; does not run a timer."""

import json

from google.cloud.firestore_v1.base_query import FieldFilter

from app.config import get_settings
from app.errors import AppError
from app.firebase import get_firestore
from app.services.conversation_deletion import ConversationDeletionService


def main():
    settings = get_settings()
    uid = settings.job_owner_uid
    if not uid or uid not in settings.allowed_uids:
        raise AppError(503, "configuration_missing", "허용 목록의 JOB_OWNER_UID를 설정하세요.")
    db = get_firestore()
    query = (
        db.collection("conversations")
        .where(filter=FieldFilter("owner_uid", "==", uid))
        .where(filter=FieldFilter("deleting", "==", True))
        .limit(100)
    )
    results = [
        ConversationDeletionService(db).start(uid, row.id, max_batches=100)
        for row in query.stream()
    ]
    print(json.dumps({"results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
