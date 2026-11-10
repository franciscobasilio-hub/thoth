# Thoth Education no Databricks Apps

## O que muda
As bases auxiliares passam a ser gravadas em um **Volume do Unity Catalog** (via API do servidor `app.py`),
e não mais só no navegador. Assim, uma base enviada no computador aparece no celular e em qualquer outro aparelho.

## Passo a passo
1. **Volume e permissões:** execute `setup.sql` (troque `<service-principal-do-app>` pelo service principal do app, depois do passo 3).
2. **Nome do volume:** se usar outro catálogo/schema/volume, ajuste `THOTH_VOLUME_PATH` em `app.yaml`.
3. **Criar o app:** Databricks > Compute > Apps > Create app (Custom) e envie estes arquivos
   (`app.py`, `app.yaml`, `requirements.txt`, `index.html`) por Git ou por upload ao workspace
   (`databricks apps deploy` ou pelo botão Deploy).
4. **Conferir:** abra o app, entre em *Bases Auxiliares*: o selo deve mostrar
   "✓ Conectado ao Databricks". Se aparecer "⚠ Sem banco conectado", o app não achou o volume
   (revise o caminho e as permissões do passo 1).
5. **Subir as bases** uma vez (em qualquer aparelho). Nos demais, abra o app (ou use o botão
   *Atualizar do Databricks*) para carregá-las.

## Teste local (opcional)
```
pip install -r requirements.txt
THOTH_LOCAL_DIR=./dados python app.py     # abre em http://localhost:8000
```

## O que fica salvo e por que sobrevive a novos deploys
Tudo é gravado no **Volume** (`THOTH_VOLUME_PATH`), que é independente do código do app.
Um novo deploy troca apenas `app.py`/`index.html`; o conteúdo do Volume não é tocado.

| O quê | Arquivo no Volume |
|---|---|
| Bases auxiliares (disciplinas, equivalências, matriz, ementas) | `kv/bases/*.json` e `kv/bases_chunks/*.json` |
| Usuários criados em Gestão de Acesso (senha só em hash) | `kv/config/USERS.json` |
| Perfis e permissões | `kv/config/PROFILES.json` |
| Links de redefinição de senha | `kv/config/RESETS.json` |

Para conferir: Catalog Explorer > `main` > `thoth` > Volumes > `dados` > pasta `kv`.

Regras para não perder dados:
- **Nunca** use `THOTH_LOCAL_DIR` em produção (o disco do app é apagado a cada deploy).
- Não apague nem recrie o Volume ao fazer deploy. O `setup.sql` só usa `IF NOT EXISTS`.
- Na primeira abertura com o Volume vazio, o sistema grava os usuários que já existem (os 3 padrão ou os que você criou no navegador). A partir daí, o Volume é a fonte oficial.
- Se um usuário for criado e o salvamento falhar, aparece o pop-up "Falha ao salvar no Databricks".
- Backup opcional: copie a pasta `kv` do Volume de tempos em tempos (Catalog Explorer ou `databricks fs cp -r`).

## Senhas (criptografia)
- As senhas **nunca** são gravadas em texto. Cada usuário guarda só um hash PBKDF2-SHA256 (210 mil iterações) com salt aleatório próprio:
  `{"email": "...", "profile": "...", "pw": {"s": "<salt>", "h": "<hash>", "i": 210000}}`.
- Hash não tem volta: nem o administrador nem o Databricks conseguem ver a senha. Quem esquecer usa "Esqueceu sua senha?"
  ou o administrador define uma senha temporária.
- Na primeira abertura após esta atualização, as senhas antigas (texto) existentes no Volume ou no navegador são convertidas
  em hash automaticamente e o texto é apagado.
- Os links de redefinição também ficam protegidos: o Volume guarda só o hash do token, e o link enviado por e-mail continua o mesmo.
- O hash exige HTTPS (o Databricks Apps já usa).

## Quem pode entrar
O controle de acesso é **só do sistema**: quem entra, com qual perfil e quais telas vê é definido em **Gestão de Acesso**.
Não é preciso criar nem ajustar acesso de cada pessoa no Databricks.

Configuração única no Databricks (uma vez, não por pessoa):
- Compute > Apps > seu app > Permissions: dê **Can use** ao grupo **All account users** (todos os usuários da conta).
  A partir daí qualquer pessoa da conta abre o app e o sistema decide se ela entra.
- As bases e os usuários são lidos e gravados pelo **service principal do app**, então ninguém precisa de permissão
  no Volume ou no catálogo. Só o service principal recebe os `GRANT` do `setup.sql`.

Limite do Databricks: o app só abre para quem faz login na conta/workspace do Databricks (SSO da empresa).
Pessoas de fora da conta não conseguem abrir o app, mesmo com acesso criado no sistema.

Não existem usuários pré-criados. No primeiro uso, com o Volume vazio, o login mostra o link
**"Primeiro acesso: criar administrador"**. Ele só aparece enquanto não houver nenhum acesso cadastrado e some depois que o
primeiro administrador é criado. Esse administrador cria os demais em Gestão de Acesso.
Sem acesso criado no sistema, o login mostra "Este e-mail não tem acesso".
Se o Volume já tinha os usuários padrão antigos (senha `123`), exclua-os ou redefina a senha em Gestão de Acesso.

## Observações
- Cada base é gravada em partes de ~3 MB em `<volume>/kv/bases_chunks/...` e o índice em `<volume>/kv/bases/...`.
- Todos os usuários do app compartilham as mesmas bases (acesso feito pelo service principal do app).
- A tela de login do sistema continua sendo a do próprio `index.html`.
