# Sync de criativos Meta Ads -> Google Drive

Roda todo dia às 07:17 (Brasília) no GitHub Actions. Lê todos os anúncios da conta, em qualquer status, e sobe para uma pasta do Google Drive só os criativos cujo nome de anúncio ainda não existe lá.

## Como funciona

- **Nome do arquivo = nome exato do anúncio.** Sem extensão por padrão (o Drive identifica o tipo pelo conteúdo). Para salvar com `.mp4`/`.jpg`, mude `APPEND_EXTENSION` para `"true"` no workflow.
- **Nunca repete nome.** A deduplicação é feita contra o que já está na pasta do Drive. Se a conta tiver dois anúncios com o mesmo nome, sobe o mais antigo (`created_time`) e o outro é contado como "nome repetido ignorado".
- **Append diário.** Só sobe o que é novo. O que falhar hoje é tentado de novo amanhã, sem duplicar.
- **Todos os status:** ativos, pausados, arquivados, reprovados, em revisão e excluídos.
- **Vídeo e imagem.** Se um anúncio tiver mais de uma mídia (carrossel, Advantage+ com várias variações, criativo por posicionamento), vira uma subpasta com o nome do anúncio e as mídias numeradas dentro (`01.mp4`, `02.jpg`...).
- **Busca da mídia em cascata:** vídeo/imagem do creative, depois post do Facebook (`effective_object_story_id`), depois post do Instagram (`effective_instagram_media_id`, cobre parte dos anúncios de parceria) e por fim `image_url`.
- **Relatório:** cada execução gera um resumo na aba Actions e um `failures.csv` (artefato) com anúncio, status e motivo de cada falha.
- **Primeira carga:** se a conta tiver milhares de anúncios, a primeira execução para em 330 min (limite do Actions é 6h) e continua de onde parou no dia seguinte. Para acelerar, rode manualmente algumas vezes em "Actions > Run workflow".

## Setup (uma vez)

### 1. Token do Meta

Use um **System User** do Business Manager com token que não expira:

1. Business Settings > Users > System users > Add (papel Admin).
2. Add assets > Ad accounts > selecione a conta > permissão de gerenciar.
3. Generate new token, marque `ads_read` e `ads_management`, validade **Never**.
4. Se você usa anúncios de parceria / posts de páginas, atribua também as **Páginas** e a **conta do Instagram** ao System User (sem isso a mídia de parte desses anúncios não é acessível).

### 2. Pasta do Drive

Crie a pasta e copie o ID da URL: `drive.google.com/drive/folders/<ESTE_ID>`.

Escolha **uma** forma de autenticação:

**Opção A: Service Account** (recomendada se a pasta estiver num Drive compartilhado da empresa)
1. Google Cloud Console > crie um projeto > ative a **Google Drive API**.
2. IAM > Service Accounts > crie > Keys > Add key > JSON.
3. Adicione o e-mail da service account como **Content manager** do Drive compartilhado.
4. Cole o JSON inteiro no secret `GDRIVE_SA_JSON`.

> Service account não tem cota própria de armazenamento: em "Meu Drive" o upload falha. Use Drive compartilhado ou a Opção B.

**Opção B: OAuth com seu usuário** (funciona em "Meu Drive")
1. Google Cloud Console > ative a Google Drive API > OAuth consent screen (External/Internal) e **publique o app** (em modo "Testing" o refresh token expira em 7 dias).
2. Credentials > Create credentials > OAuth client ID > **Desktop app** > baixe o JSON.
3. No seu computador: `pip install google-auth-oauthlib` e `python get_drive_refresh_token.py client_secret.json`.
4. Cole os 3 valores impressos nos secrets.

### 3. Secrets do GitHub

Settings > Secrets and variables > Actions > New repository secret:

| Secret | Valor |
|---|---|
| `META_ACCESS_TOKEN` | token do System User |
| `META_AD_ACCOUNT_ID` | `act_939465894739796` |
| `GDRIVE_FOLDER_ID` | ID da pasta |
| `GDRIVE_SA_JSON` | (Opção A) JSON da service account |
| `GDRIVE_CLIENT_ID` / `GDRIVE_CLIENT_SECRET` / `GDRIVE_REFRESH_TOKEN` | (Opção B) |

### 4. Primeira execução

Actions > "Sync criativos Meta Ads -> Google Drive" > **Run workflow**. Confira o resumo e o `failures.csv`.

## Pontos de atenção

- **Renomear um anúncio já enviado** gera um arquivo novo com o nome novo (o antigo continua no Drive).
- **Apagar um arquivo do Drive** faz ele ser reenviado na próxima execução. Para excluir de vez, deixe-o na pasta.
- **Anúncios excluídos há muito tempo** podem ter a mídia removida pelo Meta. Esses aparecem no `failures.csv`.
- **Anúncios de parceria com post do creator:** se o token não tiver acesso ao post original, a mídia não vem. A correção é dar ao System User acesso à conta do Instagram/Página usada no anúncio.
- **Agendamento:** o GitHub desliga crons em repositórios sem commit há 60 dias. O job `keepalive` faz um commit vazio a cada 50 dias para evitar isso.

## Rodar local

```bash
pip install -r requirements.txt
export META_ACCESS_TOKEN=... META_AD_ACCOUNT_ID=act_... GDRIVE_FOLDER_ID=... GDRIVE_SA_JSON="$(cat sa.json)"
python sync_meta_creatives.py
```
