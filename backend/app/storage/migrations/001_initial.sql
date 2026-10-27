CREATE TABLE sessions (
    id uuid PRIMARY KEY,
    pending jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE records (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES sessions(id),
    kind text CHECK (kind IN ('task','meeting','reminder')),
    text text NOT NULL CHECK (length(trim(text)) > 0),
    scheduled_date date,
    scheduled_time time,
    timezone text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
    source text NOT NULL,
    source_id text,
    raw_payload jsonb NOT NULL DEFAULT '{}',
    UNIQUE(owner_id,source,source_id)
);
CREATE INDEX records_by_date ON records(owner_id,scheduled_date);
CREATE INDEX records_search ON records USING gin(to_tsvector('russian',text));
CREATE TABLE chat_messages (
    id bigserial PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES sessions(id),
    role text NOT NULL CHECK (role IN ('user','assistant')),
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE chat_requests (
    owner_id uuid NOT NULL REFERENCES sessions(id),
    request_id uuid NOT NULL,
    message text NOT NULL,
    response jsonb NOT NULL,
    PRIMARY KEY(owner_id,request_id)
);

