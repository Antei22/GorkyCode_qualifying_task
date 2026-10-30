"""PostgreSQL adapters for the unified operation application layer."""
from datetime import date, time, timedelta
import re
from app.application.operations.ports import TurnContext
from app.domain.models import DomainError, RecordDraft
from app.domain.operations import (
    DurationInput, DurationValue, ExecutionResult, OperationIntent, RecordSnapshot,
    TargetResolution,
)
from app.domain.time import parse_day, parse_time, resolve_temporal
from app.domain.text import person_matches


def row_snapshot(row) -> RecordSnapshot:
    user_duration = row.get('user_duration')
    blocking_source = row.get('blocking_duration_source') or 'legacy_unknown'
    return RecordSnapshot(
        id=row['id'], kind=row['kind'],
        kind_source=row.get('kind_source') or 'user_input', text=row['text'],
        scheduled_date=row['scheduled_date'],
        scheduled_end_date=row.get('scheduled_end_date'),
        scheduled_time=row['scheduled_time'],
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
        if self._is_this_reference(context.message):
            last = self.repository.get_last_referenced(
                self.connection, context.owner_id, target.kind,
            )
            if last is not None:
                context.trace.update({
                    'search_stage': 'last_referenced_record',
                    'candidate_ids': [str(last['id'])],
                    'selected_id': str(last['id']),
                })
                return TargetResolution(candidates=(row_snapshot(last),))
        hard_kind = target.kind or (target.kinds[0] if len(target.kinds) == 1 else None)
        temporal_scope = target.temporal if isinstance(target.temporal, str) else 'unspecified'
        try:
            day = date.fromisoformat(target.date_expression) if target.date_expression else None
        except ValueError:
            day = parse_day(target.date_expression, context.now)
        if not isinstance(target.temporal, str):
            bounds = resolve_temporal(target.temporal, context.now)
            if bounds.start == bounds.end:
                day = bounds.start
        try:
            moment = time.fromisoformat(target.time_expression) if target.time_expression else None
        except ValueError:
            moment = parse_time(target.time_expression)
        rows = self.repository.resolve_records(
            self.connection, context.owner_id, query=target.query,
            kind=hard_kind, temporal=temporal_scope, day=day, moment=moment,
            now=context.now, limit=21,
        )
        stage = 'lexical'
        if target.person:
            rows = [row for row in rows if person_matches(target.person, row['text'])]
            stage = 'person_exact'
        if not rows and target.query and self.hybrid_search is not None:
            pool = self.repository.resolve_records(
                self.connection, context.owner_id, query=None,
                kind=hard_kind, temporal=temporal_scope, day=day, moment=moment,
                now=context.now, limit=101,
            )
            if target.person:
                pool = [row for row in pool if person_matches(target.person, row['text'])]
                # Semantic ranking is legal only after the person constraint.
                stage = 'person_semantic_empty' if not pool else 'person_semantic'
            rows = self.hybrid_search.semantic_candidates(target.query, pool, limit=5)
            if rows:
                rows = self.hybrid_search.rerank(target.query, rows)
                stage = 'semantic_rerank' if len(rows) == 1 else 'semantic'
            else:
                stage = 'semantic_empty'
        self.repository.trace_search(
            self.connection, context, intent, target.query, stage, rows,
        )
        context.trace.update({
            'search_stage': stage,
            'candidate_ids': [str(row['id']) for row in rows[:20]],
            'target_query': target.query,
            'person_filter': target.person,
            'person_label': target.person_label,
        })
        return TargetResolution(
            candidates=tuple(row_snapshot(row) for row in rows[:20]),
            truncated=len(rows) > 20,
        )

    def list(self, context: TurnContext, intent: OperationIntent) -> TargetResolution:
        target = intent.target
        first = last = None
        if not isinstance(target.temporal, str):
            bounds = resolve_temporal(target.temporal, context.now)
            first, last = bounds.start, bounds.end
        elif target.date_expression:
            try:
                first = last = date.fromisoformat(target.date_expression)
            except ValueError:
                first = last = parse_day(target.date_expression, context.now)
        time_scope = target.time_scope
        if time_scope == 'future' and (target.temporal == 'any' or
                                       target.temporal == 'unspecified'):
            temporal = 'future'
        elif time_scope == 'past' and (target.temporal == 'any' or
                                       target.temporal == 'unspecified'):
            temporal = 'past'
        elif isinstance(target.temporal, str) and target.temporal in {'future', 'past', 'all'}:
            temporal = target.temporal
        else:
            temporal = 'all'
        rows = self.repository.list_records(
            self.connection, context.owner_id, first, last,
            kind=target.kind, kinds=target.kinds, now=context.now,
            temporal=temporal,
            order_direction=target.ordering_direction,
            limit=100000,
        )
        if target.person:
            rows = [row for row in rows if person_matches(target.person, row['text'])]
        total_count = len(rows)
        explicit = target.result_limit is not None
        page_size = target.result_limit if explicit else 20
        rows = rows[:page_size]
        has_more = (not explicit and total_count > len(rows))
        next_cursor = (f'{rows[-1]["scheduled_date"]}T'
                       f'{rows[-1]["scheduled_time"] or "23:59:59"}|{rows[-1]["id"]}'
                       if has_more and rows else None)
        self.repository.trace_search(
            self.connection, context, intent, None,
            'person_exact' if target.person else 'list', rows,
        )
        context.trace.update({
            'search_stage': 'list',
            'candidate_ids': [str(row['id']) for row in rows[:100]],
            'target_kinds': list(target.kinds),
            'target_query': None,
            'person_filter': target.person,
            'person_label': target.person_label,
            'result_limit': target.result_limit,
            'page_size': page_size,
            'total_count': total_count,
            'returned_count': len(rows),
            'has_more': has_more,
            'next_cursor': next_cursor,
            'normalized_temporal': {
                'start': first.isoformat() if first else None,
                'end_exclusive': ((last + timedelta(days=1)).isoformat()
                                  if last else None),
            },
        })
        return TargetResolution(
            candidates=tuple(row_snapshot(row) for row in rows[:100]),
            truncated=has_more, total_count=(len(rows) if explicit else total_count),
            has_more=has_more, next_cursor=next_cursor,
        )

    @staticmethod
    def _is_this_reference(message):
        return bool(re.search(
            r'\b(?:эту|этот|это)\s+(?:встреч\w*|задач\w*|напоминани\w*)\b',
            message.lower().replace('ё', 'е'),
        ))


class PostgresOperationExecutor:
    def __init__(self, repository, connection):
        self.repository = repository
        self.connection = connection

    def execute(self, context: TurnContext, plan) -> ExecutionResult:
        after = plan.after
        if plan.action in ('create', 'update'):
            draft = RecordDraft(
                kind=after.kind, kind_source=after.kind_source, text=after.text,
                scheduled_date=after.scheduled_date,
                scheduled_end_date=after.scheduled_end_date,
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
