-- 1) Rode uma vez, em um SQL Editor ou notebook do Databricks.
--    Ajuste catálogo/schema/volume se quiser outros nomes (e o THOTH_VOLUME_PATH no app.yaml).
CREATE SCHEMA IF NOT EXISTS main.thoth;
CREATE VOLUME IF NOT EXISTS main.thoth.dados COMMENT 'Bases auxiliares do Thoth Education';

-- 2) Dê acesso ao service principal do app (Compute > Apps > seu app > aba Authorization mostra o nome/ID).
GRANT USE CATALOG ON CATALOG main TO `<service-principal-do-app>`;
GRANT USE SCHEMA ON SCHEMA main.thoth TO `<service-principal-do-app>`;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME main.thoth.dados TO `<service-principal-do-app>`;
