"""
Gera o GDRIVE_REFRESH_TOKEN (Opção B: OAuth com o seu usuário Google).
Rode UMA vez no seu computador:

    pip install google-auth-oauthlib
    python get_drive_refresh_token.py caminho/para/client_secret.json

Abre o navegador, você autoriza, e o script imprime os 3 valores para colar
nos Secrets do GitHub.
"""

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    if len(sys.argv) != 2:
        print("Uso: python get_drive_refresh_token.py client_secret.json")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    with open(sys.argv[1], encoding="utf-8") as f:
        info = json.load(f)
    client = info.get("installed") or info.get("web") or {}

    print("\nCole estes valores nos Secrets do repositório:\n")
    print(f"GDRIVE_CLIENT_ID      = {client.get('client_id')}")
    print(f"GDRIVE_CLIENT_SECRET  = {client.get('client_secret')}")
    print(f"GDRIVE_REFRESH_TOKEN  = {creds.refresh_token}")


if __name__ == "__main__":
    main()
