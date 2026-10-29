"""PostgreSQL adapters for the unified operation application layer."""
from app.application.operations.ports import TurnContext
from app.domain.models import DomainError, RecordDraft
from app.domain.operations import (
    DurationValue, ExecutionResult, OperationIntent, RecordSnapshot,
    TargetResolution,
)
from app.domain.time import parse_day, parse_time
from app.observability import pipeline_event


def row_snapshot(row) -> RecordSnapshot:
    return RecordSnapshot(
        id=row['id'], kind=row['kind'], text=row['text'],
        scheduled_date=row['scheduled_date'], scheduled_time=row['scheduled_time'],
        timezone=row['timezone'],
        duration=DurationValue(
            minutes=row.get('duration_minutes'),
            source=row.get('duration_source') or 'unknown',
            description=row.get('duration_description'),
        ),
        status=row['status'], revision=str(row.get('version') or 1),
    )


class PostgresTargetResolver:
    def __init__(self, repository, connection):
        self.repository = repository
        self.connection = connection

    def resolve(self, context: TurnContext, intent: OperationIntent,
                selected_id=None) -> TargetResolution:
        if selected_id is not None:
            pipeline_event(
                'storage', 'record_lookup_started', context,
                mode='selected_candidate', selected_id=selected_id,
            )
            row = self.repository.get_record(
                self.connection, context.owner_id, selected_id,
            )
            pipeline_event(
                'storage', 'record_lookup_finished', context,
                mode='selected_candidate', rows_found=1 if row else 0,
            )
            return TargetResolution(candidates=(row_snapshot(row),) if row else ())
        target = intent.target
        day = parse_day(target.date_expression, context.now) if target.date_expression else None
        moment = parse_time(target.time_expression) if target.time_expression else None
        pipeline_event(
            'storage', 'record_search_started', context,
            query=target.query, kind=target.kind, temporal=target.temporal,
            day=day, time=moment, limit=21,
        )
        rows = self.repository.resolve_records(
            self.connection, context.owner_id, query=target.query,
            kind=target.kind, temporal=target.temporal, day=day, moment=moment,
            now=context.now, limit=21,
        )
        pipeline_event(
            'storage', 'record_search_finished', context,
            rows_found=len(rows), returned=min(len(rows), 20),
            truncated=len(rows) > 20,
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
        pipeline_event(
            'storage', 'mutation_started', context,
            action=plan.action, record_id=after.id,
            expected_revision=(plan.before.revision if plan.before else None),
            fingerprint=plan.fingerprint,
        )
        if plan.action in ('create', 'update'):
            draft = RecordDraft(
                kind=after.kind, text=after.text,
                scheduled_date=after.scheduled_date,
                scheduled_time=after.scheduled_time,
                duration_minutes=after.duration.minutes,
                duration_source=after.duration.source,
                duration_description=after.duration.description,
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
        pipeline_event(
            'storage', 'mutation_finished', context,
            action=plan.action, record_id=row['id'],
            new_revision=str(row.get('version') or 1), status=row['status'],
        )
        return ExecutionResult(action=plan.action, records=(row_snapshot(row),))
