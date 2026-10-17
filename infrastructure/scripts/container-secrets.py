"""Generate local Compose secrets once. Never print their values."""
import os
from pathlib import Path
import secrets

directory = Path(__file__).resolve().parents[2] / '.runtime'
directory.mkdir(exist_ok=True,mode=0o700)
def create_secret(filename):
    try:
        descriptor = os.open(directory/filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor,'w') as output:
        output.write(secrets.token_urlsafe(48))
create_secret('postgres-password')
create_secret('gateway.key')
print('Файлы секретов Compose подготовлены в .runtime/')
