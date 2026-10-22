-- Emparejamiento con el Hub (slice 8, ADR-0013). Instantes UTC ISO 8601 con sufijo Z.

-- CA del puerto de enlace del Hub, obtenida y verificada con el código al emparejar. Con ella la
-- entrega de eventos verifica el TLS del Hub. Sin fila, el Agent no está emparejado.
CREATE TABLE hub_pairing (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    hub_ca_pem TEXT NOT NULL,
    paired_at TEXT NOT NULL
);
