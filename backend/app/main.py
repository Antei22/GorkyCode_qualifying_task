"""FastAPI entrypoint and HTTP boundary for GorkyCode."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import OpenAIError
from psycopg import Error as DatabaseError
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from app.assistant.agent import Assistant
from app.assistant.operation_interpreter import LLMOperationInterpreter
from app.assistant.search import HybridSearch
from app.config import Settings
from app.domain.models import DomainError, Kind
from app.domain.time import Clock, period_bounds
from app.ingestion.csv_import import import_csv
from app.ingestion.kind_classifier import SemanticImportKindClassifier
from app.service import Service
from app.storage.repository import Repository

COOKIE = 'gorkycode_session'
DATA = Path(os.getenv('DATA_DIR', str(Path(__file__).resolve().parents[2] / 'data'))) / 'voice_assistant_events_1000_77e927b5d2.csv'
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: UUID
    message: str = Field(min_length=1, max_length=4000)


class RecordStatusRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['open', 'done']
    expected_version: int = Field(ge=1)


def create_app(settings=None, repository=None, assistant=None, operation_interpreter=None):
    settings = settings or Settings()
    repo = repository or Repository(settings.database_url)
    clock = Clock(settings.timezone, settings.fixed_now)
    interpreter = operation_interpreter
    if interpreter is None and assistant is None and settings.unified_operations:
        interpreter = LLMOperationInterpreter(settings)
    hybrid_search = HybridSearch(settings)
    import_kind_classifier = SemanticImportKindClassifier(settings)
    service = Service(repo, assistant or Assistant(settings), clock,
                      operation_interpreter=interpreter,
                      hybrid_search=hybrid_search)

    @asynccontextmanager
    async def lifespan(app):
        repo.initialize()
        yield

    app = FastAPI(title='GorkyCode tasks and meetings API', lifespan=lifespan)
    app.state.service = service
    app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin],
                       allow_credentials=True, allow_methods=['GET', 'POST', 'PATCH'], allow_headers=['Content-Type'])

    @app.exception_handler(DomainError)
    async def input_error(request, error):
        return JSONResponse(status_code=422, content={'detail': str(error)})

    @app.exception_handler(DatabaseError)
    async def database_error(request, error):
        logger.error('Database operation failed: %s', type(error).__name__)
        return JSONResponse(status_code=503, content={'detail': 'База данных недоступна. Действие не подтверждено.'})

    @app.exception_handler(OpenAIError)
    async def model_error(request, error):
        logger.error('Model operation failed: %s', type(error).__name__)
        return JSONResponse(status_code=503, content={'detail': 'Модель недоступна или истекло время ожидания. Изменения не сохранены.'})

    def owner(request, response):
        token = request.cookies.get(COOKIE, '')
        try:
            uid = UUID(token)
        except ValueError:
            uid = None
        if uid is None or not repo.exists(uid):
            uid = repo.new_session()
            response.set_cookie(COOKIE, str(uid), httponly=True, samesite='lax',
                                secure=settings.cookie_secure, max_age=60*60*24*30)
        return uid

    @app.get('/api/health')
    def health():
        with repo.connect() as con:
            con.execute('SELECT 1')
        return {'status': 'ok', 'llm_configured': bool(settings.llm_api_key)}

    @app.get('/api/session')
    def session(request: Request, response: Response):
        uid = owner(request, response)
        with repo.connect() as con:
            state = con.execute(
                'SELECT pending,operation_state FROM sessions WHERE id=%s', (uid,)
            ).fetchone()
            pending = state['operation_state'] or state['pending']
        return {'id': uid, 'now': clock.now(), 'timezone': settings.timezone,
                'fixed_clock': bool(settings.fixed_now), 'llm_configured': bool(settings.llm_api_key), 'pending': pending}

    @app.get('/api/history')
    def history(request: Request, response: Response):
        uid = owner(request, response)
        with repo.connect() as con:
            return repo.history(con, uid, 100)

    @app.post('/api/chat')
    def chat(payload: ChatRequest, request: Request, response: Response):
        return service.chat(owner(request, response), payload.request_id, payload.message)

    @app.patch('/api/records/{record_id}/status')
    def set_record_status(record_id: UUID, payload: RecordStatusRequest,
                           request: Request, response: Response):
        uid = owner(request, response)
        with repo.connect() as con:
            row = repo.set_status(con, uid, record_id, payload.expected_version, payload.status)
        if not row:
            raise HTTPException(409, 'Запись уже изменилась. Обновите список и повторите действие.')
        return {'record': row}

    @app.get('/api/records')
    def records(request: Request, response: Response, period: str = 'all',
                kind: Kind | None = None, query: str = '', date_from: str = '', date_to: str = ''):
        uid = owner(request, response)
        if len(query) > 300:
            raise HTTPException(422, 'Поисковый запрос слишком длинный.')
        if query and period != 'all':
            raise HTTPException(422, 'Поиск выполняется по всей истории; уберите фильтр периода.')
        first, last = period_bounds(period, clock.now(), date_from, date_to)
        with repo.connect() as con:
            if query.strip():
                rows = repo.search(con, uid, query, kind, now=clock.now())
                if not rows:
                    pool = repo.list_records(
                        con, uid, None, None, kind, now=clock.now(),
                    )
                    rows = hybrid_search.semantic_candidates(query, pool, limit=5)
                    if rows:
                        rows = hybrid_search.rerank(query, rows)
            else:
                rows = repo.list_records(con, uid, first, last, kind, now=clock.now())
        return {'records': rows, 'limit': 20 if query.strip() else 100}

    @app.post('/api/import/demo')
    def demo(request: Request, response: Response):
        uid = owner(request, response)
        return import_csv(
            repo, uid, DATA, settings.timezone,
            semantic_classifier=import_kind_classifier,
        )

    return app


app = create_app()
