-- Схема для Supabase.
-- Как применить: Supabase Dashboard -> SQL Editor -> New query -> вставить -> Run.
-- Скрипт идемпотентный, можно прогонять повторно.

-- ---------------------------------------------------------------- чаты
create table if not exists chats (
    id            bigint primary key,          -- marked peer id (-100xxxxxxxxxx для супергрупп)
    username      text,
    title         text,
    kind          text,                        -- group | supergroup | channel
    audience_hint text,                        -- b2b | b2c | mixed — проставляешь руками
    members_count int,
    is_active     boolean     not null default true,
    added_at      timestamptz not null default now()
);

-- ------------------------------------------------------------ сообщения
-- Телефоны/почты/карты маскируются ещё на этапе записи (см. pii.py),
-- реальные user_id не хранятся вообще — только солёный хэш.
create table if not exists messages (
    chat_id      bigint      not null references chats(id) on delete cascade,
    message_id   bigint      not null,
    ts           timestamptz not null,
    author_hash  text,
    author_label text,                         -- u:1a2b3c4d, стабильный псевдоним
    is_bot       boolean     not null default false,
    text         text,
    reply_to     bigint,
    fwd_from     text,
    media_type   text,
    reactions    int         not null default 0,
    edited_at    timestamptz,
    primary key (chat_id, message_id)
);
create index if not exists messages_chat_ts_idx on messages (chat_id, ts);
create index if not exists messages_ts_idx      on messages (ts);

-- ------------------------------------------------------ курсоры выгрузки
create table if not exists cursors (
    chat_id       bigint primary key references chats(id) on delete cascade,
    oldest_id     bigint,                      -- докуда ушли назад по истории
    newest_id     bigint,                      -- докуда дошли вперёд
    backfill_done boolean not null default false,
    last_run      timestamptz,
    retry_after   timestamptz,                 -- выставляется при FloodWait
    note          text
);

-- ------------------------------------------------------------------ треды
-- Восстановленные диалоги: единица анализа. root_id — минимальный message_id.
create table if not exists threads (
    chat_id      bigint      not null references chats(id) on delete cascade,
    root_id      bigint      not null,
    started_at   timestamptz not null,
    ended_at     timestamptz not null,
    message_ids  bigint[]    not null,
    participants int         not null default 0,
    text_hash    text        not null,         -- меняется, когда тред дорос
    status       text        not null default 'pending',  -- pending | extracted | skipped | failed
    built_at     timestamptz not null default now(),
    primary key (chat_id, root_id)
);
create index if not exists threads_status_idx on threads (status);

-- ---------------------------------------------------------------- сигналы
create table if not exists signals (
    id             bigserial primary key,
    chat_id        bigint not null,
    root_id        bigint not null,
    type           text   not null,   -- pain | need | jtbd | question | workaround
                                      -- | alternative | willingness_to_pay | feature_request
    audience       text   not null,   -- owner | staff | optometrist | supplier | customer | unknown
    summary        text   not null,
    evidence_quote text   not null,   -- дословная цитата, проверена на вхождение в исходник
    message_ids    bigint[],
    author_label   text,
    intensity      int,               -- 1..5
    confidence     real,
    entities       text[],            -- бренды линз/оправ, оборудование, ПО, поставщики
    context        text,
    ts             timestamptz,       -- время исходного обсуждения (для трендов)
    cluster_id     bigint,
    created_at     timestamptz not null default now(),
    foreign key (chat_id, root_id) references threads (chat_id, root_id) on delete cascade
);
create index if not exists signals_cluster_idx  on signals (cluster_id);
create index if not exists signals_ts_idx       on signals (ts);
create index if not exists signals_audience_idx on signals (audience);

-- --------------------------------------------------------------- кластеры
create table if not exists clusters (
    id         bigserial primary key,
    audience   text not null,
    label      text not null,
    statement  text,                  -- каноническая формулировка боли
    card       jsonb,                 -- развёрнутая карточка от синтеза
    score      real,
    n_signals  int  not null default 0,
    n_authors  int  not null default 0,
    n_chats    int  not null default 0,
    first_seen timestamptz,
    last_seen  timestamptz,
    updated_at timestamptz not null default now()
);
create index if not exists clusters_score_idx on clusters (score desc);

-- ------------------------------------------------------------ прогоны/лог
create table if not exists runs (
    id          bigserial primary key,
    kind        text not null,        -- ingest | threads | extract | cluster | report
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    stats       jsonb,
    error       text
);

-- ------------------------------------------------------------ расход модели
-- Одна строка на операцию (разбор, группировка, карточки). Пользователю бота
-- не показывается — только по скрытой команде /usage.
create table if not exists llm_usage (
    id                bigserial primary key,
    ts                timestamptz not null default now(),
    stage             text   not null,             -- extract | cluster | cards
    model             text,
    calls             int    not null default 0,
    prompt_tokens     bigint not null default 0,
    completion_tokens bigint not null default 0,
    reasoning_tokens  bigint not null default 0,   -- входят в completion_tokens
    threads           int    not null default 0,   -- для extract: обработано обсуждений
    seconds           int    not null default 0,
    cancelled         boolean not null default false
);
create index if not exists llm_usage_ts_idx on llm_usage (ts);

-- ------------------------------------------------------------ защита данных
-- Supabase публикует схему public через REST API (Data API). Без RLS любой,
-- у кого есть anon-ключ проекта (он считается публичным), мог бы читать
-- переписки. Включаем RLS без единой политики: ролям API (anon,
-- authenticated) доступ закрыт полностью, а пайплайн ходит напрямую под
-- владельцем таблиц, на которого RLS не распространяется.
-- Заодно это гасит предупреждения «RLS Disabled in Public» в Security Advisor.
alter table chats    enable row level security;
alter table messages enable row level security;
alter table cursors  enable row level security;
alter table threads  enable row level security;
alter table signals  enable row level security;
alter table clusters enable row level security;
alter table runs     enable row level security;
alter table llm_usage enable row level security;
