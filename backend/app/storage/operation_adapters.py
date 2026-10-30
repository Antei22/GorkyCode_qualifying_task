"""PostgreSQL adapters for the unified operation application layer."""
from datetime import date, time
from app.application.operations.ports import TurnContext
from app.domain.models import DomainError, RecordDraft
from app.domain.operations import (
    DurationInput, DurationValue, ExecutionResult, OperationIntent, RecordSnapshot,
    TargetResolution,
)
from app.domain.time import parse_day, parse_time


def row_snapshot(row) -> RecordSnapshot:
    user_duration = row.get('user_duration')
    blocking_source = row.get('blocking_duration_source') or 'legacy_unknown'
    return RecordSnapshot(
        id=row['id'], kind=row['kind'], text=row['text'],
        scheduled_date=row['scheduled_date'], scheduled_time=row['scheduled_time'],
        timezone=row['timezone'],
        user_duration=(DurationInput.model_validate(user_duration)
                       if user_duration else None),
        duration_status=row.get('duration_status') or 'defaulted',
        duration=DurationValue(
            minutes=row.get('duration_minutes'),
            source=row.get('duration_source') or 'unknown',
            description=row.get('duration_description'),
            blocking_source=blocking_source,
        ),
        status=row['status'], revision=str(row.get('version') or 1),
    )


class PostgresTargetResolver:
    def __init__(self, repository, connection, hybrid_search=None):
        self.repository = repository
        self.connection = connection
        self.hybrid_search = hybrid_search

    def resolve(self, context: TurnContext, intent: OperationIntent,
                selected_id=None) -> TargetResolution:
        if selected_id is not None:
            row = self.repository.get_record(
                self.connection, context.owner_id, selected_id,
            )
            return TargetResolution(candidates=(row_snapshot(row),) if row else ())
        target = intent.target
        try:
            day = date.fromisoformat(target.date_expression) if target.date_expression else None
        except ValueError:
            day = parse_day(target.date_expression, context.now)
        try:
            moment = time.fromisoformat(target.time_expression) if target.time_expression else None
        except ValueError:
            moment = parse_time(target.time_expression)
        rows = self.repository.resolve_records(
            self.connection, context.owner_id, query=target.query,
            kind=target.kind, temporal=target.temporal, day=day, moment=moment,
            now=context.now, limit=21,
        )
        stage = 'lexical'
        if not rows and target.query and self.hybrid_search is not None:
            pool = self.repository.resolve_records(
                self.connection, context.owner_id, query=None,
                kind=target.kind, temporal=target.temporal, day=day, moment=moment,
                now=context.now, limit=101,
            )
            rows = self.hybrid_search.semantic_candidates(target.query, pool, limit=5)
            if rows:
                rows = self.hybrid_search.rerank(target.query, rows)
                stage = 'semantic_rerank' if len(rows) == 1 else 'semantic'
            else:
                stage = 'semantic_empty'
        self.repository.trace_search(
            self.connection, context, intent, target.query, stage, rows,
        )
        return TargetResolution(
            candidates=tuple(row_snapshot(row) for row in rows[:20]),
            truncated=len(rows) > 20,
        )


class PostgresOperationExecutor:
    def __init__(self, repository, connection):
        self.repository = repository
        self.connection = connection

    def execute(self, context: TurnContext, plan) -> ExecutionResult:
        after = plan.after
        if plan.action in ('create', 'update'):
            draft = RecordDraft(
                kind=after.kind, text=after.text,
                scheduled_date=after.scheduled_date,
                scheduled_time=after.scheduled_time,
                duration_minutes=after.duration.minutes,
                duration_source=after.duration.source,
                duration_description=after.duration.description,
                user_duration=(after.user_duration.model_dump(mode='json', exclude_none=True)
                               if after.user_duration else None),
                duration_status=after.duration_status,
                blocking_duration_source=after.duration.blocking_source,
                timezone=after.timezone,
            )
        if plan.action == 'create':
            row = self.repository.create_with_id(
                self.connection, context.owner_id, after.id, draft,
            )
        elif plan.action == 'update':
            row = self.repository.update_record(
                self.connection, context.owner_id, after.id,
                int(plan.before.revision), draft,
            )
        else:
            row = self.repository.cancel_record_versioned(
                self.connection, context.owner_id, after.id,
                int(plan.before.revision), context.now,
            )
        if not row:
            raise DomainError(
                'Запись изменилась или стала недоступна. Повторите операцию заново.'
            )
        return ExecutionResult(action=plan.action, records=(row_snapshot(row),))
