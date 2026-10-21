from datetime import datetime, timezone

from app.config import get_settings
from app.errors import AppError
from app.providers.discord import split_content
from app.repositories.briefings import BriefingRepository
from app.services.briefing_content import build_content
from app.services.delivery import drain_outbox
from app.services.research import ResearchService


class BriefingService:
    def __init__(self, db, settings=None, *, research_factory=None, discord=None, clock=None):
        self.db = db
        self.settings = settings or get_settings()
        self.repository = BriefingRepository(db)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.research_factory = research_factory or (lambda: ResearchService(db, self.settings))
        self.discord = discord

    def preview(self, uid, selection):
        content = build_content(self.research_factory(), selection)
        return self.repository.save_preview(uid, selection, content)

    def queue(self, uid, briefing_id, key):
        briefing = self.repository.get(uid, briefing_id)
        if not self.settings.discord_webhook_url:
            raise AppError(
                503, "discord_not_configured", "서버의 DISCORD_WEBHOOK_URL을 설정하세요."
            )
        return self.repository.queue(
            uid, briefing_id, key, split_content(briefing["text"], briefing_id), self.clock()
        )

    def _generate(self, claim):
        try:
            content = build_content(
                self.research_factory(),
                claim,
                since=claim["_schedule"].get("last_successful_briefing_at"),
            )
            parts = split_content(content["text"], claim["id"])
        except Exception:
            content, parts = None, []
        return self.repository.finish_build(claim, content, parts, self.clock())

    def tick(self, uid, *, schedule_limit=10, part_limit=30):
        now = self.clock()
        counts = {"generated": 0, "parts_sent": 0, "delivery_errors": 0}
        for row in self.repository.recover_builds(uid, now, schedule_limit):
            claim = self.repository.reclaim(uid, row.id, self.clock())
            if claim and self._generate(claim):
                counts["generated"] += 1
        for row in self.repository.due(uid, now, schedule_limit):
            claim = self.repository.reserve(uid, row.id, self.clock())
            if claim and self._generate(claim):
                counts["generated"] += 1
        counts.update(
            drain_outbox(
                self.repository,
                uid,
                self.settings,
                self.clock,
                discord=self.discord,
                part_limit=part_limit,
            )
        )
        return counts
