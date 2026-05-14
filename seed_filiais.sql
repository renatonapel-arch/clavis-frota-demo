-- Seed de filiais e centros de custo para demo local
-- Em staging/prod isso vem do sync incremental SIGE (TODO[ADR-sync-SIGE-filiais])

CREATE TABLE IF NOT EXISTS configuracao_filiais (
  filial    INTEGER PRIMARY KEY,
  sigla     TEXT NOT NULL,
  nome      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS configuracao_centros_custo (
  codigo    TEXT PRIMARY KEY,
  nome      TEXT NOT NULL,
  filial    INTEGER NOT NULL REFERENCES configuracao_filiais(filial)
);

INSERT OR IGNORE INTO configuracao_filiais (filial, sigla, nome) VALUES
  (100, 'MGA', 'Maringá — Matriz'),
  (700, 'MGA', 'Maringá — CD'),
  (900, 'MGA', 'Maringá — Loja'),
  (300, 'LEM', 'Londrina'),
  (302, 'LEM', 'Londrina — CD'),
  (200, 'PTA', 'Ponta Grossa'),
  (202, 'PTA', 'Ponta Grossa — CD');

INSERT OR IGNORE INTO configuracao_centros_custo (codigo, nome, filial) VALUES
  ('LOG-MGA', 'Logística Maringá',     100),
  ('ADM-MGA', 'Administrativo Maringá', 100),
  ('LOG-LEM', 'Logística Londrina',     300),
  ('LOG-PTA', 'Logística Ponta Grossa', 200);
