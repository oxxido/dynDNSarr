-- Estado de entrega al Hub (slice 3a): `events` hace de outbox. Pendiente = ambos nulos.
-- Solo ADD COLUMN: los eventos ya guardados quedan pendientes.
ALTER TABLE events ADD COLUMN delivered_at TEXT;
ALTER TABLE events ADD COLUMN rejected_code TEXT;
CREATE INDEX events_pending ON events (sequence)
    WHERE delivered_at IS NULL AND rejected_code IS NULL;
